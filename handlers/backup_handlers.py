import os

from keyboards import Button
from workflow import query
from .base import ensure_owner
from staff_security import require
from .platform_handlers import back


async def backup_menu(event, action, value):
    if not await require(event, 'operations'):
        return
    if action in {'new', 'restore'} and not await ensure_owner(event):
        return
    if action == 'new':
        from operations import create_and_send_database_backup
        await event.answer('بدأ إنشاء النسخة. يمكنك متابعة مركز المهام.')
        await create_and_send_database_backup(event.client,event.sender_id)
        return await event.respond('تم إنشاء النسخة وإرسالها.',buttons=[back('ops:backup:list:0')])
    if action == 'restore':
        from backup_verification import verify_backup
        await event.answer('بدأ اختبار الاستعادة في قاعدة مؤقتة؛ تابع مركز المهام.')
        result = await verify_backup(event.sender_id,int(value))
        return await event.respond(result,parse_mode=None,buttons=[back('ops:backup:list:0')])
    rows = await query('''SELECT backup_id,status,size_bytes,delivered,error,restore_status,restore_details,created_at,restored_at
        FROM backup_runs ORDER BY backup_id DESC LIMIT 8''',fetchall=True)
    latest = await query("SELECT backup_id,size_bytes,created_at FROM backup_runs WHERE status='success' ORDER BY backup_id DESC LIMIT 1",fetchone=True)
    text = 'مراقبة النسخ الاحتياطي والاستعادة\n'
    if latest:
        text += f'آخر نسخة ناجحة: #{latest[0]} · {latest[1] or 0:,} بايت · {latest[2]}\n'
    else:
        text += 'لا توجد نسخة ناجحة مسجلة بعد.\n'
    text += ('اختبار الاستعادة مُعدّ على الخادم.\n' if os.getenv('RESTORE_TEST_ADMIN_URL') else
             'لتفعيل الاختبار اضبط RESTORE_TEST_ADMIN_URL لاتصال صيانة في خادم اختبار بصلاحية إنشاء قواعد مؤقتة.\n')
    buttons = [[Button.inline('إنشاء نسخة الآن',data='ops:backup:new:0')]]
    labels = {'success':'نجحت','failed':'فشلت','running':'جارية','untested':'لم تُختبر','passed':'نجح الاختبار','interrupted':'انقطع التشغيل'}
    for key,state,size,delivered,error,restore,details,created,tested in rows or []:
        text += f'\n#{key} · {labels.get(state,state)} · {size or 0:,} بايت\n{created}\nالإرسال: {"تم" if delivered else "لم يتم"}\nالاستعادة: {labels.get(restore,restore)}\n'
        if error:
            text += error+'\n'
        if details:
            text += details[:220]+'\n'
        if state == 'success' and restore != 'running':
            buttons.append([Button.inline(f'اختبار استعادة #{key}',data=f'ops:backup:restore:{key}')])
    buttons += [[Button.inline('تحديث',data='ops:backup:list:0')],back('admin_panel')]
    await event.edit(text,buttons=buttons,parse_mode=None)
