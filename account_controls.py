"""Account labels and health checks shared by the Mini App and existing services."""
import asyncio

from postgres import connect
from utils.asyncio_compat import to_thread
from workflow import query

OWNED_OR_RENTED = """EXISTS(SELECT 1 FROM purchased_accounts p WHERE p.phone=? AND p.user_id=?)
    OR EXISTS(SELECT 1 FROM stored_accounts s WHERE s.phone=? AND s.user_id=?
        AND NOT EXISTS(SELECT 1 FROM purchased_accounts p WHERE p.phone=s.phone AND p.user_id<>s.user_id))
    OR EXISTS(SELECT 1 FROM rental_leases l WHERE l.phone=? AND l.renter_id=?
        AND l.status='active' AND l.ends_at>CURRENT_TIMESTAMP)"""


def save_labels_sync(user, phone, label, folder, tags):
    values = [str(v).strip() for v in (label, folder, tags)]
    if any(len(v) > maximum for v,maximum in zip(values, (60,40,160))):
        raise ValueError('الاسم أو المجموعة أو الوسوم أطول من المسموح.')
    with connect() as db:
        from account_access import is_manager
        administrative = '''(? AND (
            EXISTS(SELECT 1 FROM accounts a WHERE a.phone=? AND NOT a.is_sold)
            OR EXISTS(SELECT 1 FROM account_management_grants g WHERE g.phone=? AND
                g.owner_id=COALESCE((SELECT p.user_id FROM purchased_accounts p WHERE p.phone=g.phone),
                                   (SELECT s.user_id FROM stored_accounts s WHERE s.phone=g.phone)))))'''
        row = db.execute(f'''INSERT INTO account_labels(user_id,phone,label,folder,tags)
            SELECT ?,?,?,?,? WHERE ({OWNED_OR_RENTED}) OR {administrative}
            ON CONFLICT(user_id,phone) DO UPDATE SET label=excluded.label,folder=excluded.folder,tags=excluded.tags
            RETURNING phone''', (user,phone,*values,phone,user,phone,user,phone,user,is_manager(user),phone,phone)).fetchone()
        if not row:
            raise ValueError('الحساب غير متاح لك.')


async def save_labels(*args):
    return await to_thread(save_labels_sync, *args)


async def account_status(user, phone):
    import account_health
    accounts = await account_health.accounts_for(user)
    selected = [a for a in accounts.values() if a.phone == phone]
    if not selected:
        raise ValueError('الحساب غير متاح لك.')
    records = await account_health.records(a.key for a in selected)
    return [{'status': records.get(a.key,{}).get('status','unknown'),
             'checked_at': records.get(a.key,{}).get('checked_at')} for a in selected]


async def request_check(client, user, phone):
    import account_health
    from handlers.health_handlers import run_checks
    accounts = await account_health.accounts_for(user)
    keys = [key for key,a in accounts.items() if a.phone == phone]
    if not keys:
        raise ValueError('الحساب غير متاح لك.')
    previous = account_health._manual_tasks.get(user)
    if previous and not previous.done():
        raise ValueError('لديك فحص جارٍ؛ انتظر نتيجته في البوت.')
    task = asyncio.create_task(run_checks(client,user,keys),name=f'miniapp-health-{user}')
    account_health._manual_tasks[user] = task
    task.add_done_callback(lambda done: account_health._manual_tasks.pop(user,None)
                          if account_health._manual_tasks.get(user) is done else None)


async def rentals(user, page):
    if page < 0:
        raise ValueError('صفحة غير صالحة.')
    rows = await query('''SELECT lease_id,phone,ends_at,status,EXTRACT(EPOCH FROM (ends_at-CURRENT_TIMESTAMP))
        FROM rental_leases WHERE renter_id=? ORDER BY ends_at DESC,lease_id DESC LIMIT 21 OFFSET ?''',
        (user,page*20), fetchall=True) or []
    return {'items': [{'id': r[0],'phone': r[1],'ends_at': r[2],'status': r[3],
                       'remaining_seconds': max(0,int(r[4]))} for r in rows[:20]], 'more': len(rows)>20}
