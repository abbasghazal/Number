"""Claim schedules atomically and keep repeating jobs cancellable across restarts."""
import asyncio
import json
import logging

import workflow
from postgres import connect
from utils.asyncio_compat import to_thread

logger = logging.getLogger(__name__)
_tasks = {}
_stopping = False


async def create(user, minutes, recurrence, folder=''):
    if not 5 <= minutes <= 10080 or recurrence not in {0, 1440, 10080} or len(folder) > 64:
        raise ValueError('الوقت أو التكرار أو المجموعة غير صالح.')
    return await workflow.query('''INSERT INTO scheduled_operations(user_id,kind,payload,due_at,interval_minutes)
        VALUES(?,'health_scan',?,CURRENT_TIMESTAMP+(? * INTERVAL '1 minute'),?) RETURNING schedule_id''',
        (user, json.dumps({'folder': folder}, ensure_ascii=False), minutes, recurrence or None), commit=True, fetchone=True)


async def cancel(user, schedule):
    row = await workflow.query('''UPDATE scheduled_operations SET cancel_requested=TRUE,
        status=CASE WHEN status='running' THEN status ELSE 'cancelled' END
        WHERE schedule_id=? AND user_id=? AND status IN ('running','pending') RETURNING schedule_id''',
        (schedule, user), commit=True, fetchone=True)
    task = _tasks.get(schedule)
    if row and task:
        task.cancel()


async def recover():
    global _stopping
    _stopping = False
    await workflow.query("""UPDATE scheduled_operations SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'pending' END,
        started_at=NULL,error='انقطع التنفيذ؛ أعيدت جدولة المحاولة'
        WHERE status='running'""", commit=True)


def claim_sync():
    slots = max(0, 2-len(_tasks))
    if not slots:
        return []
    with connect() as db:
        return db.execute('''UPDATE scheduled_operations s SET status='running',started_at=CURRENT_TIMESTAMP,error=NULL
            WHERE schedule_id IN (SELECT schedule_id FROM scheduled_operations
                WHERE status='pending' AND NOT cancel_requested AND due_at<=CURRENT_TIMESTAMP
                ORDER BY due_at FOR UPDATE SKIP LOCKED LIMIT ?)
            RETURNING s.schedule_id,s.user_id,s.kind,s.payload''', (slots,)).fetchall()


async def run_one(client, schedule, user, kind, payload):
    status, error = 'completed', None
    try:
        from handlers.base import user_has_license_access
        from helpers import is_banned
        if await is_banned(user) or not (await user_has_license_access(user))[0]:
            raise ValueError('توقف الوصول إلى الخدمة.')
        if kind != 'health_scan':
            raise ValueError('نوع المهمة غير مدعوم.')
        from account_health import accounts_for
        from handlers.health_handlers import run_checks
        folder = json.loads(payload or '{}').get('folder', '')
        accounts = await accounts_for(user)
        if folder:
            labels = await workflow.query('SELECT phone FROM account_labels WHERE user_id=? AND folder=?',
                                           (user, folder), fetchall=True) or []
            phones = {r[0] for r in labels}
            accounts = {k: a for k, a in accounts.items() if a.phone in phones}
        await run_checks(client, user, list(accounts), schedule_id=schedule)
    except asyncio.CancelledError:
        status, error = ('interrupted' if _stopping else 'cancelled'), 'أُوقفت المهمة؛ النتائج المكتملة محفوظة.'
    except Exception:
        logger.exception('Scheduled health check failed')
        status, error = 'failed', 'تعذر إكمال الفحص المجدول.'
    finally:
        await workflow.query('''UPDATE scheduled_operations SET
            status=CASE WHEN cancel_requested THEN 'cancelled' WHEN ?='interrupted' THEN 'pending' WHEN interval_minutes IS NOT NULL AND ?='completed'
                THEN 'pending' ELSE ? END,
            due_at=CASE WHEN interval_minutes IS NOT NULL AND ?='completed' THEN
                CURRENT_TIMESTAMP+(interval_minutes * INTERVAL '1 minute') ELSE due_at END,
            completed_at=CURRENT_TIMESTAMP,run_count=run_count+1,started_at=NULL,error=?
            WHERE schedule_id=? AND status='running' ''', (status, status, status, status, error, schedule), commit=True)
        await workflow.queue_notice(user, f'schedule:{schedule}:{__import__("secrets").token_hex(8)}',
                                    f'المهمة المجدولة #{schedule}: ' + (error or 'اكتملت بنجاح.'))


async def dispatch(client):
    if _stopping:
        return
    for schedule, user, kind, payload in await to_thread(claim_sync):
        task = asyncio.create_task(run_one(client, schedule, user, kind, payload), name=f'schedule-{schedule}')
        _tasks[schedule] = task
        task.add_done_callback(lambda done, sid=schedule: _tasks.pop(sid, None))


async def stop():
    global _stopping
    _stopping = True
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
