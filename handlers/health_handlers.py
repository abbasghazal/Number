"""Health menus resolve ownership on every callback, including stale buttons."""
import asyncio
import contextlib
import datetime as dt
import logging

import account_health as health
import workflow
from keyboards import Button
from utils.privacy import mask_phone
from .common import ProgressMessage, edit_or_respond

logger = logging.getLogger(__name__)
PAGE_SIZE = 8


def timestamp(value):
    return value.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if value else '—'


def describe(account, record):
    details = record.get('details', {})
    twofa = {True: 'مفعّل', False: 'غير مفعّل', None: 'غير متحقق'}[details.get('twofa')]
    partial = ' (إحصاء جزئي)' if details.get('counts_partial') else ''
    text = (
        f"🩺 {mask_phone(account.phone)}\n"
        f"الحالة: {health.LABELS[record.get('status', 'unknown')]}\n"
        f"آخر محاولة: {timestamp(record.get('checked_at'))}\n"
        f"آخر نتيجة مؤكدة: {timestamp(record.get('confirmed_at'))}\n"
    )
    if record.get('attempt') in {'temporary', 'flood_wait'}:
        text += f"المحاولة الأخيرة: {health.LABELS[record['attempt']]}؛ النتيجة السابقة محفوظة.\n"
    if details.get('installation_removed'):
        text += 'أُزيل تسجيل الجلسة المنتهية. يمكنك تنصيب الحساب أو عرضه للبيع بجلسة جديدة صالحة.\n'
    text += (
        f"الاسم: {details.get('name') or '—'}\n"
        f"المعرف: {details.get('username') or '—'}\n"
        f"التحقق بخطوتين: {twofa}\n"
        f"المجموعات: {details.get('groups') if details.get('groups') is not None else '—'}{partial}\n"
        f"القنوات: {details.get('channels') if details.get('channels') is not None else '—'}{partial}\n"
        'قيود السبام: غير متحققة؛ صلاحية الدخول لا تضمن صلاحية كل خدمة.\n'
        'التحقق بخطوتين لا يمنع استخدام الحساب.'
    )
    return text


async def health_menu(event, data='health:page:0'):
    if event.chat_id != event.sender_id:
        return await event.answer('افتح هذه القائمة في المحادثة الخاصة.', alert=True)
    user_id = event.sender_id
    accounts = await health.accounts_for(user_id)
    try:
        _, action, value = data.split(':', 2)
    except ValueError:
        return await event.answer('زر غير صالح.', alert=True)
    if action == 'page':
        try:
            page = max(0, int(value))
        except ValueError:
            return await event.answer('صفحة غير صالحة.', alert=True)
        items = sorted(accounts.values(), key=lambda a: (a.phone, a.key))
        page = min(page, max(0, (len(items) - 1) // PAGE_SIZE))
        visible = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        state = await health.records(a.key for a in visible)
        buttons = [[Button.inline(
            f"{mask_phone(a.phone)} · {health.LABELS[state.get(a.key, {}).get('status', 'unknown')]}",
            data=f'health:view:{a.key[:20]}',
        )] for a in visible]
        navigation = []
        if page:
            navigation.append(Button.inline('السابق', data=f'health:page:{page-1}'))
        if (page + 1) * PAGE_SIZE < len(items):
            navigation.append(Button.inline('التالي', data=f'health:page:{page+1}'))
        if navigation:
            buttons.append(navigation)
        if items:
            buttons.append([Button.inline('فحص جميع حساباتي', data='health:scan:all')])
        buttons.append([Button.inline('رجوع', data='accounts_view')])
        return await edit_or_respond(event,
            f'🩺 صحة الحسابات — {len(items)} حساب\n'
            'فحص دوري كل 6 ساعات مع تنبيه عند تغير الحالة.\n'
            'يُستبعد العطل المؤكد من النشر وخدمات الحسابات والتأجير.\n'
            'عند ثبوت انتهاء جلسة حساب منصّب يُزال تسجيلها القديم لتتمكن من إضافته بجلسة جديدة.\n'
            'اختر حسابًا لعرض التفاصيل أو إعادة الفحص.', buttons=buttons)
    matches = [a for a in accounts.values() if a.key.startswith(value)]
    account = matches[0] if len(matches) == 1 else None
    if action == 'view' and account:
        record = (await health.records([account.key])).get(account.key, {})
        # Explicitly disable parsing for Telegram-controlled profile strings.
        return await event.edit(describe(account, record), parse_mode=None, buttons=[
            [Button.inline('إعادة الفحص', data=f'health:scan:{account.key[:20]}')],
            [Button.inline('رجوع', data='health:page:0')],
        ])
    if action != 'scan' or (value != 'all' and account is None):
        return await event.answer('الحساب لم يعد متاحًا لك.', alert=True)
    task = health._manual_tasks.get(user_id)
    if task and not task.done():
        return await event.answer('لديك فحص جارٍ بالفعل.', alert=True)
    keys = list(accounts) if value == 'all' else [account.key]
    if not keys:
        return await event.answer('لا توجد حسابات متاحة للفحص.', alert=True)
    await event.answer('بدأ الفحص. ستصلك النتيجة هنا.')
    task = asyncio.create_task(run_checks(event.client, user_id, keys), name=f'health-check-{user_id}')
    health._manual_tasks[user_id] = task
    task.add_done_callback(lambda done: health._manual_tasks.pop(user_id, None)
                           if health._manual_tasks.get(user_id) is done else None)


@workflow.tracked('health')
async def run_checks(client, user_id, keys, schedule_id=None):
    counts = {}
    completed = 0
    failures = 0
    status = await client.send_message(
        user_id, f'⏳ بدأ فحص {len(keys)} حساب. سأعرض التقدم أثناء الفحص.', parse_mode=None
    )
    reporter = ProgressMessage(status, 'جارٍ فحص صحة الحسابات', len(keys))
    await workflow.progress(total=len(keys))
    try:
        for key in keys:
            if schedule_id is not None:
                row = await workflow.query('SELECT cancel_requested FROM scheduled_operations WHERE schedule_id=? AND user_id=?',
                                           (schedule_id, user_id), fetchone=True)
                if not row or row[0]:
                    raise asyncio.CancelledError()
            await workflow.progress(processed=completed, success=completed-failures, failed=failures)
            # A deleted account or expired lease must not be checked from a
            # session cached at the time the user pressed the button.
            account = (await health.accounts_for(user_id)).get(key)
            if account is None:
                continue
            record = await health.check_account(account, manual=True)
            await health.deliver_notices(client, account, record)
            label = health.LABELS[record.get('attempt', 'unknown')]
            counts[label] = counts.get(label, 0) + 1
            completed += 1
            failures += record.get('attempt') != 'healthy'
            await reporter.update(completed, completed - failures, failures)
        await workflow.progress(processed=completed, success=completed-failures, failed=failures)
        await reporter.update(completed, completed - failures, failures, force=True)
        summary = '\n'.join(f'• {label}: {count}' for label, count in counts.items())
        await client.send_message(user_id,
            f'🩺 اكتمل طلب الفحص: {completed}/{len(keys)} حساب\n{summary}\n'
            'قد تُستخدم النتيجة الأخيرة عند تكرار الفحص خلال دقيقة أو أثناء انتظار تيليجرام.',
            parse_mode=None, buttons=[[Button.inline('عرض النتائج', data='health:page:0')]])
    except Exception:
        await workflow.note_failure('تعذر إكمال الفحص؛ النتائج المكتملة محفوظة')
        logger.exception('Manual account health check failed')
        if schedule_id is not None:
            raise
        with contextlib.suppress(Exception):
            await client.send_message(user_id, 'تعذر إكمال الفحص. النتائج المكتملة محفوظة؛ حاول لاحقًا.')
