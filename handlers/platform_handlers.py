"""Private, ownership-scoped menus for jobs, 2FA, rentals and withdrawals."""
import asyncio
import re

from keyboards import Button, cancel_operation_keyboard
from helpers import log_audit_event, mark_withdrawal_paid_atomically
from platform_services import owned_password, reject_withdrawal, renewal_quote, renew_lease
from utils.privacy import mask_phone
from workflow import query
from .base import active_conversations, ensure_admin, enforce_rate_limit

STATES = {'running': 'جارية', 'completed': 'مكتملة', 'failed': 'فشلت', 'cancelled': 'ملغاة',
          'interrupted': 'توقفت بانقطاع التشغيل', 'retrying': 'فشلت محاولة؛ إعادة المحاولة مجدولة',
          'pending': 'بانتظار المراجعة', 'paid': 'تم الصرف', 'rejected': 'مرفوضة مع استرداد المبلغ'}
KINDS = {'import': 'استيراد جلسات', 'health': 'فحص حسابات', 'posting': 'نشر', 'creation': 'إنشاء مجموعات',
         'shahm_collect': 'شهم · تجميع', 'shahm_transfer': 'شهم · تحويل', 'shahm_gift': 'شهم · هدية',
         'shahm_buttons': 'شهم · عرض الأزرار', 'shahm_press': 'شهم · ضغط زر',
         'backup': 'نسخ احتياطي', 'restore': 'اختبار استعادة'}


def back(data='main'):
    return [Button.inline('رجوع', data=data)]


async def ask(event, prompt, timeout=180):
    user = event.sender_id
    if getattr(event, 'chat_id', user) != user:
        await event.answer('استخدم المحادثة الخاصة لإرسال البيانات.', alert=True)
        return None
    if user in active_conversations:
        await event.answer('أكمل المحادثة الحالية أو ألغها أولًا.', alert=True)
        return None
    async with event.client.conversation(user) as conv:
        active_conversations[user] = conv
        try:
            if getattr(event, 'data', None) is not None:
                await event.answer()
            mark_input = getattr(event, 'mark_input_started', None)
            if mark_input:
                mark_input()
            message = await conv.send_message(prompt, buttons=cancel_operation_keyboard())
            while True:
                response = await conv.get_response(message, timeout=timeout)
                text = (response.raw_text or '').strip()
                command = text.split(maxsplit=1)[0].split('@', 1)[0].lower() if text else ''
                if text in ('إلغاء العملية', 'إلغاء', 'الغاء', 'رجوع') or command in ('/cancel', '/start'):
                    return None
                if response.file or not text:
                    message = await conv.send_message('أرسل الرد كنص، أو اضغط إلغاء العملية.', buttons=cancel_operation_keyboard())
                    continue
                return text
        except asyncio.TimeoutError:
            await conv.send_message('انتهى وقت الإدخال. افتح العملية مجددًا للمحاولة.')
        except asyncio.CancelledError:
            # conv.cancel() cancels the pending response, not the handler task.
            # Preserve real task cancellation during shutdown.
            if getattr(asyncio.current_task(), 'cancelling', lambda: 0)():
                raise
            return None
        finally:
            if active_conversations.get(user) is conv:
                active_conversations.pop(user, None)


async def jobs_menu(event, action='list', value='0'):
    if action in ('cleanup','purge'):
        from task_cleanup import menu
        return await menu(event,action,value)
    user = event.sender_id
    if action == 'cancel':
        row = await query('''UPDATE workflow_jobs SET cancel_requested=TRUE WHERE job_id=?
            AND user_id=? AND status='running' AND cancellable=TRUE RETURNING job_id''',
            (value, user), commit=True, fetchone=True)
        await event.answer('سيُلغى عند أقرب نقطة آمنة؛ النتائج المكتملة محفوظة.' if row else 'المهمة غير متاحة للإلغاء.', alert=True)
        action, value = 'view', value
    if action == 'view':
        row = await query('''SELECT kind,status,total,processed,success,failed,message,cancellable,
            cancel_requested,updated_at FROM workflow_jobs WHERE job_id=? AND user_id=?''', (value, user), fetchone=True)
        if not row:
            return await event.answer('المهمة غير موجودة ضمن حسابك.', alert=True)
        kind, status, total, processed, success, failed, message, can_cancel, requested, updated = row
        text = (f'المهمة #{value} · {KINDS.get(kind, kind)}\nالحالة: {STATES.get(status,status)}\n'
                f'المعالج: {processed} / {total if total is not None else "غير محدد"}\n'
                f'الناجح: {success} · الفاشل: {failed}\n{message or ""}\nآخر تحديث: {updated}')
        if kind == 'posting':
            text += '\nتقدم المعالجة بالدورات، والنجاح والفشل بمحاولات الإرسال.'
        if kind.startswith('shahm_'):
            text += '\nالتقدم بعدد الحسابات المعالجة. اكتمال الطلب يعني وصول رد؛ راجع رد البوت المستهدف لتأكيد النتيجة والرصيد.'
        if requested:
            text += '\nتم طلب الإلغاء. أثناء انتظار إدخال، استخدم زر إلغاء المحادثة أيضًا.'
        buttons = [[Button.inline('تحديث', data=f'ops:jobs:view:{value}')]]
        if can_cancel and status == 'running' and not requested:
            buttons.append([Button.inline('إلغاء المهمة', data=f'ops:jobs:cancel:{value}')])
        buttons.append(back('ops:jobs:list:0'))
        return await event.edit(text, buttons=buttons, parse_mode=None)
    page = max(0, min(10000, int(value)))
    rows = await query('''SELECT job_id,kind,status FROM workflow_jobs WHERE user_id=?
        ORDER BY created_at DESC,job_id DESC LIMIT 9 OFFSET ?''', (user, page*8), fetchall=True)
    buttons = [[Button.inline(f'{KINDS.get(kind,kind)} · {STATES.get(status,status)}', data=f'ops:jobs:view:{key}')]
               for key, kind, status in (rows or [])[:8]]
    if page:
        buttons.append([Button.inline('السابق', data=f'ops:jobs:list:{page-1}')])
    if len(rows or []) > 8:
        buttons.append([Button.inline('التالي', data=f'ops:jobs:list:{page+1}')])
    buttons += [[Button.inline('حذف جميع المهام المكتملة والملغاة',data='ops:jobs:cleanup:mine')],[Button.inline('تحديث', data=f'ops:jobs:list:{page}')], back()]
    from config import DEVELOPER_ID
    if user==DEVELOPER_ID:
        buttons.insert(-1,[Button.inline('تنظيف المهام المنتهية لجميع المستخدمين',data='ops:jobs:cleanup:all')])
    await event.edit('مركز المهام والعمليات\nتظهر محاولات التشغيل الجديدة هنا، وتبقى نتائجها بعد إعادة التشغيل.', buttons=buttons)


async def twofa_menu(event, action='list', value='0'):
    user = event.sender_id
    if action in {'reveal', 'confirm'}:
        if not re.fullmatch(r'\d{7,20}', value):
            raise ValueError('رقم غير صالح')
        password = await owned_password(user, value)
        if not password or password == 'لا يوجد':
            return await event.answer('لا توجد كلمة محفوظة لحساب تملكه.', alert=True)
        if action == 'reveal':
            return await event.edit('سيتم إظهار كلمة التحقق المحفوظة في هذه المحادثة الخاصة. قد تكون قد تغيرت خارج البوت.',
                buttons=[[Button.inline('إظهار الكلمة الآن', data=f'ops:twofa:confirm:{value}')], back('ops:twofa:list:0')])
        if not await enforce_rate_limit(event, 'reveal_twofa', 10):
            return
        await log_audit_event(user, f'twofa_revealed:{mask_phone(value)}')
        await event.respond(f'كلمة التحقق المحفوظة للحساب {mask_phone(value)}:\n{password}', parse_mode=None)
        return
    page = max(0, min(10000, int(value)))
    rows = await query('''WITH owned AS (
        SELECT phone FROM stored_accounts WHERE user_id=? UNION SELECT phone FROM purchased_accounts WHERE user_id=?
    ), items AS (
        SELECT phone FROM owned UNION SELECT phone FROM twofa_history WHERE user_id=?
        UNION SELECT phone FROM scheduled_twofa_changes WHERE owner_id=?
    ) SELECT i.phone,s.due_at,s.attempts,s.last_error,h.status,h.error,h.updated_at,
        EXISTS(SELECT 1 FROM owned o WHERE o.phone=i.phone)
        FROM items i LEFT JOIN scheduled_twofa_changes s ON s.phone=i.phone
            AND (s.owner_id=? OR (s.owner_id IS NULL AND EXISTS(SELECT 1 FROM owned o WHERE o.phone=s.phone)))
        LEFT JOIN twofa_history h ON h.phone=i.phone AND h.user_id=?
        ORDER BY i.phone LIMIT 9 OFFSET ?''', (user,user,user,user,user,user,page*8), fetchall=True)
    text = 'إدارة تغيير كلمات 2FA\nالمواعيد بتوقيت قاعدة البيانات.\n\n'
    buttons = []
    for phone, due, attempts, error, state, old_error, updated, owned in (rows or [])[:8]:
        status = ('فشلت بعد 3 محاولات' if attempts >= 3 else 'مجدولة / إعادة محاولة') if due else STATES.get(state, 'لا توجد مهمة تغيير مسجلة')
        text += f'{mask_phone(phone)} · {status}\n'
        if due:
            text += f'الموعد: {due} · المحاولات: {attempts}\n'
        reason = error if due else old_error
        if reason:
            text += f'السبب: {reason}\n'
        if updated and not due:
            text += f'آخر تحديث: {updated}\n'
        if owned:
            buttons.append([Button.inline(f'الكلمة المحفوظة · {mask_phone(phone)}', data=f'ops:twofa:reveal:{phone}')])
    if page:
        buttons.append([Button.inline('السابق', data=f'ops:twofa:list:{page-1}')])
    if len(rows or []) > 8:
        buttons.append([Button.inline('التالي', data=f'ops:twofa:list:{page+1}')])
    buttons += [[Button.inline('تحديث', data=f'ops:twofa:list:{page}')], back('accounts_view')]
    await event.edit(text, buttons=buttons, parse_mode=None)


async def withdrawals_menu(event, action='list', value='0'):
    if not await ensure_admin(event, 'finance'):
        return
    if action == 'reject':
        request_id = int(value)
        reason = await ask(event, f'أرسل سبب رفض طلب السحب #{request_id}. سيُعاد المبلغ تلقائيًا إلى رصيد صاحبه.')
        if reason is None:
            return
        result = await reject_withdrawal(request_id, event.sender_id, reason)
        return await event.respond('تم الرفض والاسترداد وتسجيل حركة مستقلة.' if result else 'تمت معالجة الطلب سابقًا؛ لم يتغير الرصيد.',
                                   buttons=[back('ops:withdraw:list:0')])
    if action == 'pay':
        request_id = int(value)
        owner = await mark_withdrawal_paid_atomically(request_id, event.sender_id)
        if owner:
            from workflow import queue_notice
            await queue_notice(owner, f'withdrawal-paid:{request_id}', f'تم تأكيد صرف طلب السحب #{request_id}.')
        return await event.edit('تم تسجيل الصرف.' if owner else 'تمت معالجة الطلب سابقًا.', buttons=[back('ops:withdraw:list:0')])
    if action == 'view':
        row = await query('''SELECT user_id,amount,wallet,status,rejection_reason FROM withdrawal_requests WHERE request_id=?''',
                          (int(value),), fetchone=True)
        if not row:
            return await event.answer('الطلب غير موجود.', alert=True)
        owner, amount, wallet, state, reason = row
        buttons = []
        if state == 'pending':
            buttons = [[Button.inline('تأكيد أن المبلغ صُرف فعليًا', data=f'ops:withdraw:pay:{value}')],
                       [Button.inline('رفض وإرجاع المبلغ', data=f'ops:withdraw:reject:{value}')]]
        buttons.append(back('ops:withdraw:list:0'))
        return await event.edit(f'طلب #{value}\nالمستخدم: {owner}\nالمبلغ: {amount}$\nالمحفظة: {wallet}\n'
                                f'الحالة: {STATES.get(state,state)}\n{reason or ""}', parse_mode=None, buttons=buttons)
    page = max(0, min(10000, int(value)))
    rows = await query('''SELECT request_id,amount,status FROM withdrawal_requests
        ORDER BY (status='pending') DESC,request_id DESC LIMIT 9 OFFSET ?''', (page*8,), fetchall=True)
    buttons = [[Button.inline(f'#{key} · {amount}$ · {STATES.get(state,state)}', data=f'ops:withdraw:view:{key}')]
               for key, amount, state in (rows or [])[:8]]
    if page:
        buttons.append([Button.inline('السابق', data=f'ops:withdraw:list:{page-1}')])
    if len(rows or []) > 8:
        buttons.append([Button.inline('التالي', data=f'ops:withdraw:list:{page+1}')])
    buttons.append(back('admin_panel'))
    await event.edit('مراجعة طلبات السحب والاسترداد', buttons=buttons)


async def rentals_menu(event, action='list', value='0'):
    user = event.sender_id
    if action == 'confirm':
        ok, message = await renew_lease(user, value)
        return await event.edit(message, buttons=[back('ops:rent:list:0')])
    if action == 'quote':
        text = await ask(event, 'أرسل عدد الأيام الإضافية. ستظهر التكلفة قبل التأكيد.')
        if text is None:
            return
        key, amount, end = await renewal_quote(user, int(value), int(text.strip()))
        return await event.respond(f'تجديد الإيجار #{value}\nالأيام الإضافية: {int(text)}\nالتكلفة: {amount}$\n'
                                   f'تُضاف المدة إلى: {end}\nالعرض صالح 5 دقائق؛ لا خصم قبل التأكيد.',
                                   buttons=[[Button.inline('تأكيد التجديد والخصم', data=f'ops:rent:confirm:{key}')], back('ops:rent:list:0')])
    page = max(0, min(10000, int(value)))
    rows = await query('''SELECT lease_id,phone,ends_at,status,EXTRACT(EPOCH FROM (ends_at-CURRENT_TIMESTAMP))
        FROM rental_leases WHERE renter_id=? ORDER BY ends_at DESC,lease_id DESC LIMIT 9 OFFSET ?''',
                       (user,page*8), fetchall=True)
    text, buttons = 'إيجاراتي والتجديد\n\n', []
    for key, phone, end, state, seconds in (rows or [])[:8]:
        remaining = max(0, int(seconds))
        text += f'#{key} · {mask_phone(phone)}\nينتهي: {end}\n'
        text += f'المتبقي: {remaining//86400} يوم و{remaining//3600%24} ساعة و{remaining//60%60} دقيقة\n\n'
        if state == 'active' and seconds > 0:
            buttons.append([Button.inline(f'تجديد #{key}', data=f'ops:rent:quote:{key}')])
    if page:
        buttons.append([Button.inline('السابق', data=f'ops:rent:list:{page-1}')])
    if len(rows or []) > 8:
        buttons.append([Button.inline('التالي', data=f'ops:rent:list:{page+1}')])
    buttons += [[Button.inline('تحديث', data=f'ops:rent:list:{page}')], back('accounts_view')]
    await event.edit(text, parse_mode=None, buttons=buttons)


async def platform_menu(event, data):
    if event.sender_id != event.chat_id:
        return await event.answer('استخدم المحادثة الخاصة.', alert=True)
    try:
        _, section, action, value = data.split(':', 3)
        handlers = {'jobs': jobs_menu, 'twofa': twofa_menu, 'withdraw': withdrawals_menu, 'rent': rentals_menu}
        if section in {'notifications', 'bulk', 'schedule', 'reports', 'service', 'tickets'}:
            from .operations_center import operations_center
            return await operations_center(event, section, action, value)
        if section == 'accounts':
            from .organization_handlers import organization_menu
            return await organization_menu(event, action, value)
        if section == 'backup':
            from .backup_handlers import backup_menu
            return await backup_menu(event, action, value)
        handler = handlers.get(section)
        if handler is None:
            raise ValueError('قسم غير صالح')
        await handler(event, action, value)
    except (ValueError, ArithmeticError):
        await event.answer('الطلب غير صالح أو تغيرت البيانات؛ حدّث القائمة وتحقق من القيم.', alert=True)
