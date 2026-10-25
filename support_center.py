"""Ticket history and ownership-checked conversations with transactional notices."""
from config import ADMIN_ID, DEVELOPER_ID
from postgres import connect
from staff_security import permitted
from utils.asyncio_compat import to_thread
from workflow import enqueue, query


def clean_message(message):
    message = str(message or '').strip()
    if not message or len(message) > 3000:
        raise ValueError('الرسالة مطلوبة وبحد أقصى 3000 حرف.')
    return message


def create_sync(user, message):
    message = clean_message(message)
    with connect() as db:
        ticket = db.execute('INSERT INTO support_tickets(user_id,message) VALUES(?,?) RETURNING ticket_id',
                            (user, message)).fetchone()[0]
        db.execute('INSERT INTO ticket_messages(ticket_id,user_id,message) VALUES(?,?,?)', (ticket, user, message))
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (user, f'support_ticket_created:{ticket}'))
        recipients = {ADMIN_ID, DEVELOPER_ID} | {r[0] for r in db.execute('SELECT user_id FROM admins').fetchall()}
        for recipient in recipients - {0}:
            enqueue(db, recipient, f'ticket:staff:{ticket}:{recipient}', f'🛟 تذكرة دعم #{ticket}\nالعميل: {user}\n\n{message}')
        return ticket


async def create(user, message):
    return await to_thread(create_sync, user, message)


def access(db, actor, ticket_id, *, lock=False):
    row = db.execute('SELECT user_id,status,assigned_to,priority FROM support_tickets WHERE ticket_id=?' +
                     (' FOR UPDATE' if lock else ''), (ticket_id,)).fetchone()
    staff = permitted(db, actor, 'support')
    if not row or (row[0] != actor and not staff):
        raise ValueError('التذكرة غير موجودة ضمن صلاحياتك.')
    return row, staff


def detail_sync(actor, ticket, page):
    if page < 0:
        raise ValueError('صفحة غير صالحة.')
    with connect() as db:
        row, staff = access(db, actor, ticket)
        messages = db.execute('''SELECT user_id,message,is_staff,created_at FROM ticket_messages
            WHERE ticket_id=? ORDER BY message_id DESC LIMIT 2 OFFSET ?''', (ticket, page)).fetchall()
        return {'id': ticket, 'owner': row[0], 'status': row[1], 'assigned_to': row[2], 'priority': row[3],
                'staff': staff, 'messages': messages[:1], 'more': len(messages) > 1}


async def detail(actor, ticket, page=0):
    return await to_thread(detail_sync, actor, ticket, page)


async def listing(actor, page=0, *, staff=False):
    if page < 0:
        raise ValueError('صفحة غير صالحة.')
    from staff_security import has_permission
    if staff and not await has_permission(actor, 'support'):
        raise ValueError('صلاحية الدعم مطلوبة.')
    condition = '' if staff else 'WHERE user_id=?'
    args = (21, page*20) if staff else (actor, 21, page*20)
    return await query(f'''SELECT ticket_id,user_id,status,assigned_to,priority,created_at FROM support_tickets
        {condition} ORDER BY CASE WHEN status='closed' THEN 1 ELSE 0 END,
        CASE WHEN priority='high' THEN 0 ELSE 1 END,ticket_id DESC LIMIT ? OFFSET ?''', args, fetchall=True) or []


def reply_sync(actor, ticket, message):
    message = clean_message(message)
    with connect() as db:
        row, staff = access(db, actor, ticket, lock=True)
        if row[1] == 'closed':
            raise ValueError('أعد فتح التذكرة قبل إضافة رسالة.')
        is_staff = staff and actor != row[0]
        mid = db.execute('''INSERT INTO ticket_messages(ticket_id,user_id,message,is_staff)
            VALUES(?,?,?,?) RETURNING message_id''', (ticket, actor, message, is_staff)).fetchone()[0]
        recipients = {row[0]} if is_staff else ({ADMIN_ID, DEVELOPER_ID} | {r[0] for r in db.execute('SELECT user_id FROM admins').fetchall()}) - {0}
        for recipient in recipients:
            enqueue(db, recipient, f'ticket:reply:{mid}:{recipient}', f'رسالة في التذكرة #{ticket}:\n{message}')
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (actor, f'ticket_reply:{ticket}:{mid}'))
        return mid


async def reply(actor, ticket, message):
    return await to_thread(reply_sync, actor, ticket, message)


def change_sync(actor, ticket, action, value=None):
    with connect() as db:
        row, staff = access(db, actor, ticket, lock=True)
        if action in {'close', 'reopen'}:
            status = 'closed' if action == 'close' else 'open'
            db.execute('''UPDATE support_tickets SET status=?,closed_by=?,closed_at=
                CASE WHEN ?='closed' THEN CURRENT_TIMESTAMP ELSE NULL END WHERE ticket_id=?''',
                (status, actor if status == 'closed' else None, status, ticket))
        elif action == 'assign' and staff:
            assigned = int(value)
            if not permitted(db, assigned, 'support'):
                raise ValueError('المسؤول المختار لا يملك صلاحية الدعم.')
            db.execute("UPDATE support_tickets SET assigned_to=?,status='in_progress',closed_at=NULL,closed_by=NULL WHERE ticket_id=?",
                       (assigned, ticket))
        elif action == 'priority' and staff and value in {'normal', 'high'}:
            db.execute('UPDATE support_tickets SET priority=? WHERE ticket_id=?', (value, ticket))
        else:
            raise ValueError('الإجراء غير مسموح.')
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (actor, f'ticket_{action}:{ticket}:{value}'))
        enqueue(db, row[0], f'ticket:change:{ticket}:{__import__("secrets").token_hex(8)}',
                f'تم تحديث التذكرة #{ticket}: ' + {'close': 'مغلقة', 'reopen': 'مفتوحة',
                                                  'assign': 'تم تعيين مسؤول', 'priority': 'تم تعديل الأولوية'}[action])


async def change(*args):
    return await to_thread(change_sync, *args)
