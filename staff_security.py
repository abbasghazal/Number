"""Shared RBAC checks for callbacks, direct handlers and the HTTP API."""
import inspect

from config import ADMIN_ID, DEVELOPER_ID
from postgres import connect
from utils.asyncio_compat import to_thread
from workflow import query

ROLES = {
    'administrator': {'support', 'inventory', 'finance', 'operations', 'manage'},
    'support': {'support'}, 'inventory': {'inventory'},
    'finance': {'finance'}, 'operations': {'operations'},
}
ROLE_NAMES = {'administrator': 'مشرف شامل', 'support': 'الدعم', 'inventory': 'المخزون',
              'finance': 'المالية', 'operations': 'التشغيل'}


def permitted(db, user, permission):
    if user and user in {ADMIN_ID,DEVELOPER_ID}:
        return True
    if permission == 'support' and (user == DEVELOPER_ID or db.execute('SELECT 1 FROM admins WHERE user_id=?', (user,)).fetchone()):
        return True
    row = db.execute('SELECT r.role FROM staff_roles r JOIN admins a ON a.user_id=r.user_id WHERE r.user_id=?',
                     (user,)).fetchone()
    return bool(row and permission in ROLES.get(row[0], set()))


async def has_permission(user_id, permission):
    if user_id and user_id in {ADMIN_ID,DEVELOPER_ID}:
        return True
    if permission == 'support' and (user_id == DEVELOPER_ID or await query('SELECT 1 FROM admins WHERE user_id=?', (user_id,), fetchone=True)):
        return True
    row = await query('SELECT r.role FROM staff_roles r JOIN admins a ON a.user_id=r.user_id WHERE r.user_id=?',
                      (user_id,), fetchone=True)
    return bool(row and permission in ROLES.get(row[0], set()))


def handler_permission(module, name):
    if name in {'admin_panel', 'admin_bot_stats'}:
        return 'staff'
    if name in {'add_coins', 'del_coins', 'buy_sell_settings', 'balance_settings', 'set_referral_reward_handler',
                'confirm_withdraw', 'rental_settings_handler'}:
        return 'finance'
    if name in {'ban_user', 'unban_user', 'ban_settings', 'close_ticket', 'reply_to_user'}:
        return 'support'
    if name in {'admin_operations'}:
        return 'operations'
    if 'country_handlers' in module:
        return 'finance' if 'price' in name else 'inventory'
    if 'support_handlers' in module:
        return 'support'
    if 'payment_handlers' in module or 'balance_handlers' in module:
        return 'finance'
    if 'backup_handlers' in module:
        return 'operations'
    if name in {'num_settings', 'all_numbers_count'}:
        return 'inventory'
    return 'manage'


async def is_staff(user_id):
    if user_id and user_id in {ADMIN_ID,DEVELOPER_ID}:
        return True
    return bool(await query('SELECT 1 FROM admins WHERE user_id=?', (user_id,), fetchone=True))


def caller_permission():
    # Caller -> base.ensure_admin -> this helper. Every direct handler is checked.
    frame = inspect.currentframe()
    try:
        caller = frame.f_back.f_back
        return handler_permission(caller.f_globals.get('__name__', ''), caller.f_code.co_name)
    finally:
        del frame


def caller_name():
    frame = inspect.currentframe()
    try:
        return frame.f_back.f_back.f_code.co_name
    finally:
        del frame


async def require(event, permission):
    user = getattr(event, 'sender_id', None) or event.chat_id
    allowed = await is_staff(user) if permission == 'staff' else await has_permission(user, permission)
    if not allowed:
        await event.answer('لا تملك الصلاحية المطلوبة لهذه العملية.', alert=True)
    return allowed


def callback_permission(data):
    if data.startswith(('num:prices','num:price:')):
        return 'finance'
    if data.startswith(('num:reviews','num:review:')):
        return 'inventory'
    if data.startswith('feature:staff:'):
        return 'owner'
    if data in {'admin_panel', 'admin_bot_stats'}:
        return 'staff'
    if data.startswith(('reply_', 'close_ticket_', 'ops:tickets:', 'feature:tickets:staff')) or data in {'ban', 'unban', 'bbvjls'}:
        return 'support'
    if data.startswith(('ops:withdraw:', 'confirm_withdraw_', 'chg_', 'chs_', 'feature:coupons:admin',
                        'feature:coupons:new', 'feature:coupons:disable', 'feature:reports:', 'feature:cost:',
                        'feature:expense:', 'feature:loyalty:edit')) or data in {
        'ajkofgl', 'ajkcoingl', 'change_price', 'change_sell_price', 'add_coins', 'del_coins',
        'set_referral_reward', 'payment_settings', 'next_buy_price_countries', 'prev_buy_price_countries',
        'next_sell_price_countries', 'prev_sell_price_countries', 'rental_settings'} or data.startswith('payment_set_'):
        return 'finance'
    if data.startswith(('ops:backup:', 'ops:service:')) or data in {'daily_backup', 'admin_operations'}:
        return 'operations'
    if data.startswith(('delete_', 'del_country_')) or data in {'ajxjao', 'all_of_number', 'add_country', 'del_country', 'add', 'del_account'}:
        return 'inventory'
    if data.startswith('ops:reports:'):
        return 'finance'
    return 'manage'


async def filtered_panel(buttons, user_id):
    result = []
    for row in buttons:
        visible = []
        for button in row:
            raw = getattr(button, 'data', b'')
            data = raw.decode() if isinstance(raw, bytes) else str(raw)
            permission = callback_permission(data)
            if data in {'main', 'developer_menu'} or (user_id == ADMIN_ID if permission == 'owner' else await is_staff(user_id) if permission == 'staff'
                                                      else await has_permission(user_id, permission)):
                visible.append(button)
        if visible:
            result.append(visible)
    return result


def set_role_sync(actor, user, role):
    if actor != ADMIN_ID or user <= 0 or user == ADMIN_ID or role not in ROLES:
        raise ValueError('غير مسموح بتعديل هذه الصلاحية.')
    with connect() as db:
        old = db.execute('SELECT role FROM staff_roles WHERE user_id=?', (user,)).fetchone()
        db.execute('INSERT INTO admins(user_id,added_by) VALUES(?,?) ON CONFLICT(user_id) DO NOTHING', (user, actor))
        db.execute('''INSERT INTO staff_roles(user_id,role,updated_by) VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET role=excluded.role,updated_by=excluded.updated_by,
            updated_at=CURRENT_TIMESTAMP''', (user, role, actor))
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (actor, f'staff_role:{user}:{old[0] if old else "none"}->{role}'))


async def set_role(actor, user, role):
    return await to_thread(set_role_sync, actor, user, role)


def remove_role_sync(actor,user):
    if actor != ADMIN_ID or user <= 0 or user == ADMIN_ID:
        raise ValueError('غير مسموح بحذف هذا المشرف.')
    with connect() as db:
        db.execute('DELETE FROM admins WHERE user_id=?',(user,))
        db.execute('DELETE FROM staff_roles WHERE user_id=?',(user,))
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)',(actor,f'staff_removed:{user}'))


async def remove_role(actor,user):
    await to_thread(remove_role_sync,actor,user)
