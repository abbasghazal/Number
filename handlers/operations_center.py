"""Safe operational dashboards: no session strings or 2FA values are exposed."""
import asyncio

from keyboards import Button
from workflow import query
from .base import ensure_admin
from .platform_handlers import ask, back


async def operations_center(event, section, action, value):
    user = event.sender_id
    if section in {'schedule', 'reports', 'tickets'}:
        from .feature_handlers import feature_menu
        translated = 'view' if section == 'reports' else 'staff' if section == 'tickets' else action
        return await feature_menu(event, f'feature:{section}:{translated}:{value}')
    if section == 'notifications':
        if action == 'toggle':
            field = value if value in {'health', 'rental', 'twofa', 'system'} else None
            if not field:
                raise ValueError('invalid preference')
            await query('INSERT INTO user_notification_preferences(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING',
                        (user,), commit=True)
            await query(f'UPDATE user_notification_preferences SET {field}=NOT {field} WHERE user_id=?', (user,), commit=True)
        if action == 'read':
            await query('UPDATE user_notification_center SET is_read=TRUE WHERE user_id=?', (user,), commit=True)
        prefs = await query('SELECT health,rental,twofa,system FROM user_notification_preferences WHERE user_id=?', (user,), fetchone=True) or (True, True, True, True)
        notices = await query('SELECT category,message,created_at FROM user_notification_center WHERE user_id=? ORDER BY notice_id DESC LIMIT 12', (user,), fetchall=True) or []
        text = '🔔 مركز الإشعارات\n\n' + ('\n'.join(f'• {created}: {message}' for _, message, created in notices) or 'لا توجد إشعارات حتى الآن.')
        labels = [('health','صحة الحسابات'), ('rental','الإيجارات'), ('twofa','2FA'), ('system','النظام')]
        buttons = [[Button.inline(f'{label}: {"✅" if prefs[i] else "❌"}', data=f'ops:notifications:toggle:{key}')]
                   for i, (key, label) in enumerate(labels)]
        buttons += [[Button.inline('تعليم الكل كمقروء', data='ops:notifications:read:0')], [back('accounts_view')[0]]]
        return await event.edit(text, parse_mode=None, buttons=buttons)

    if section == 'bulk':
        if action == 'start' and value == 'health':
            confirmation = await ask(event, 'سيُعاد فحص كل حساباتك المتاحة. اكتب نعم للتأكيد.')
            if confirmation != 'نعم':
                return await event.respond('تم إلغاء العملية.')
            from account_health import accounts_for
            from .health_handlers import run_checks
            keys = list(await accounts_for(user))
            if not keys:
                return await event.respond('لا توجد حسابات متاحة للفحص.')
            await event.respond(f'⏳ بدأ فحص جماعي آمن لـ {len(keys)} حساب.')
            asyncio.create_task(run_checks(event.client, user, keys), name=f'bulk-health-{user}')
            return
        return await event.edit('🛡️ عمليات جماعية آمنة\n\nكل عملية تتطلب تأكيداً وتعرض تقدماً ويمكن إيقافها من مركز المهام.',
                                buttons=[[Button.inline('فحص جماعي للحسابات', data='ops:bulk:start:health')], [back('accounts_view')[0]]])

    if not await ensure_admin(event, 'operations' if section == 'service' else 'manage'):
        return
    if section == 'service':
        from service_health import snapshot
        health = await snapshot(fresh=True)
        from operations import get_operational_stats
        stats = await get_operational_stats()
        jobs = await query("SELECT COUNT(*) FROM workflow_jobs WHERE status='running'", fetchone=True)
        failed = await query("SELECT kind,message,updated_at FROM workflow_jobs WHERE status='failed' ORDER BY updated_at DESC LIMIT 5", fetchall=True) or []
        worker_text = '\n'.join(f'• {name}: {"سليم" if item["ok"] else "متأخر أو متوقف"} · آخر تقدم منذ {item["age_seconds"]} ثانية' for name,item in health['workers'].items())
        return await event.edit(f'🩺 حالة الخدمة\n\nقاعدة البيانات: {"متصلة" if health["database"] else "غير متاحة"}\nتيليجرام: {"متصل" if health["telegram"] else "منقطع"}\nحجمها: {stats["database_size"]/(1024*1024):.2f} MB\nمهام جارية: {jobs[0] if jobs else 0}\nنشر نشط: {stats["posting_tasks"]}\nسحوبات معلقة: {stats["withdrawals"]}\n\n{worker_text}\n\nآخر الأخطاء:\n' + ('\n'.join(f'• {k}: {m or "—"} ({d})' for k,m,d in failed) or 'لا توجد أخطاء مسجلة.'), parse_mode=None, buttons=[[Button.inline('تحديث',data='ops:service:list:0')],[back('admin_panel')[0]]])
    raise ValueError('unknown center section')


async def run_scheduled_operations(client):
    from recurring_jobs import dispatch
    return await dispatch(client)
