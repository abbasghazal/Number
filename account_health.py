"""Telegram health evidence, revoked installation cleanup and scoped notices.

No login, messages to SpamBot, session logout, or account modification happens
on Telegram. A network failure never deletes a saved account.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import weakref
from dataclasses import dataclass, field

from telethon import errors, functions, types
from session_guard import TelegramClient
from telethon.sessions import StringSession

from config import ADMIN_ID, API_HASH, API_ID
from utils.privacy import mask_phone

logger = logging.getLogger(__name__)
BLOCKED = frozenset({'revoked', 'banned', 'deleted', 'restricted', 'invalid', 'mismatch'})
LABELS = {
    'unknown': 'لم يُفحص بعد', 'healthy': 'متصل وصالح للدخول',
    'revoked': 'الجلسة منتهية — يحتاج إعادة تسجيل الدخول',
    'banned': 'حساب معطل أو محظور', 'deleted': 'حساب محذوف',
    'restricted': 'تيليجرام يبلّغ عن تقييد للحساب',
    'invalid': 'جلسة غير صالحة أو تعذر فك تشفيرها',
    'mismatch': 'رقم الجلسة لا يطابق الرقم المسجل',
    'temporary': 'تعذر الاتصال مؤقتًا', 'flood_wait': 'انتظار طلبه تيليجرام',
}
CHECK_INTERVAL = 6 * 60 * 60
MANUAL_INTERVAL = 60
_locks = weakref.WeakValueDictionary()
_slots = None
_slots_loop = None
_manual_tasks = {}


async def query(*args, **kwargs):
    # Lazy import keeps helpers free to use the service eligibility functions.
    from helpers import execute_query
    return await execute_query(*args, **kwargs)


def session_key(session):
    value = str(session or '')
    try:
        # The auth key survives DC/address or StringSession serialization changes.
        key = StringSession(value).auth_key
        if key:
            return hashlib.sha256(key.key).hexdigest()
    except (ValueError, TypeError):
        pass
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class Account:
    phone: str
    session: str = field(repr=False)
    key: str
    recipients: set = field(default_factory=set)
    inventory_session: str | None = field(default=None, repr=False)
    installed: bool = False


async def accounts_for(user_id=None):
    """Resolve current owners, never former sellers or expired renters."""
    from helpers import decrypt_session
    if user_id is None:
        rows = await query('''
            SELECT s.phone, s.session, s.user_id, 'owned' FROM stored_accounts s
            WHERE NOT EXISTS(SELECT 1 FROM purchased_accounts p WHERE p.phone=s.phone AND p.user_id<>s.user_id)
            UNION ALL SELECT phone, session, user_id, 'owned' FROM purchased_accounts
            UNION ALL SELECT phone, session, ?, 'inventory' FROM accounts WHERE is_sold=0
            UNION ALL SELECT phone, session, seller_id, 'pending' FROM pending_sales
            UNION ALL SELECT a.phone, a.session, l.renter_id, 'rented'
              FROM rental_leases l JOIN accounts a ON a.phone=l.phone
              WHERE l.status='active' AND l.ends_at>CURRENT_TIMESTAMP
        ''', (ADMIN_ID,), fetchall=True)
    else:
        from account_access import is_manager
        manager = is_manager(user_id)
        rows = await query('''
            SELECT s.phone,s.session,s.user_id,'owned' FROM stored_accounts s
            WHERE (s.user_id=? OR (? AND EXISTS(SELECT 1 FROM account_management_grants g
                WHERE g.phone=s.phone AND g.owner_id=s.user_id)))
              AND NOT EXISTS(SELECT 1 FROM purchased_accounts p WHERE p.phone=s.phone AND p.user_id<>s.user_id)
            UNION ALL SELECT p.phone,p.session,p.user_id,'owned' FROM purchased_accounts p
            WHERE p.user_id=? OR (? AND EXISTS(SELECT 1 FROM account_management_grants g
                WHERE g.phone=p.phone AND g.owner_id=p.user_id))
            UNION ALL SELECT a.phone,a.session,?,'inventory' FROM accounts a
            WHERE a.is_sold=0 AND (? OR ?)
            UNION ALL SELECT a.phone,a.session,l.renter_id,'rented'
              FROM rental_leases l JOIN accounts a ON a.phone=l.phone
              WHERE l.renter_id=? AND l.status='active' AND l.ends_at>CURRENT_TIMESTAMP
        ''', (user_id,manager,user_id,manager,ADMIN_ID,manager,int(user_id)==ADMIN_ID,user_id), fetchall=True)
    result = {}
    for phone, encrypted, owner, source in rows or []:
        session = decrypt_session(encrypted)
        # Distinguish unreadable ciphertexts without exposing them in the UI.
        key = (session_key(session) if session else
               hashlib.sha256(f'unreadable:{phone}:{encrypted}'.encode()).hexdigest())
        account = result.setdefault(key, Account(str(phone), session or '', key))
        if owner:
            account.recipients.add(int(owner))
        if source == 'inventory':
            account.inventory_session = encrypted
        elif source in {'owned', 'pending'}:
            account.installed = True
    return result


async def records(keys):
    keys = list(keys)
    if not keys:
        return {}
    rows = await query('''SELECT session_key, status, attempt, details,
        checked_at, confirmed_at, next_check_at FROM account_health
        WHERE session_key=ANY(?)''', (keys,), fetchall=True)
    return {r[0]: dict(zip(
        ('status', 'attempt', 'details', 'checked_at', 'confirmed_at', 'next_check_at'),
        (r[1], r[2], json.loads(r[3]), r[4], r[5], r[6]),
    )) for r in rows or []}


async def filter_usable_accounts(accounts):
    """Unexamined accounts remain usable; only confirmed faults exclude them."""
    accounts = list(accounts or [])
    state = await records(session_key(a[1]) for a in accounts)
    return [a for a in accounts if a[1] and state.get(session_key(a[1]), {}).get('status') not in BLOCKED]


async def session_is_usable(session):
    if not session:
        return False
    state = await records([session_key(session)])
    return state.get(session_key(session), {}).get('status') not in BLOCKED


@dataclass
class Result:
    status: str
    details: dict = field(default_factory=dict)
    retry_seconds: int = CHECK_INTERVAL


def classify_error(exc):
    name = type(exc).__name__
    if isinstance(exc, errors.FloodWaitError):
        return Result('flood_wait', retry_seconds=max(60, int(exc.seconds) + 5))
    if name in {'UserDeactivatedBanError', 'PhoneNumberBannedError'}:
        return Result('banned')
    if name == 'UserDeactivatedError':
        return Result('deleted')
    if name in {'AuthKeyInvalidError', 'AuthKeyUnregisteredError', 'AuthKeyDuplicatedError',
                'SessionRevokedError', 'SessionExpiredError', 'UnauthorizedError'}:
        return Result('revoked')
    # Do not infer an account ban from a chat permission error, transport error,
    # AuthKeyNotFound, an unfamiliar RPC error, or a failed optional detail fetch.
    return Result('temporary', retry_seconds=15 * 60)


async def probe(account):
    client = None
    try:
        try:
            session = StringSession(account.session)
            if not session.auth_key:
                return Result('invalid')
        except (ValueError, TypeError):
            return Result('invalid')
        client = TelegramClient(session, API_ID, API_HASH,
                                connection_retries=0, request_retries=0,
                                flood_sleep_threshold=0, timeout=10)
        await asyncio.wait_for(client.connect(), 15)
        users = await asyncio.wait_for(
            client(functions.users.GetUsersRequest([types.InputUserSelf()])), 15,
        )
        me = users[0] if users else None
        if me is None or isinstance(me, types.UserEmpty):
            return Result('revoked')
        if getattr(me, 'deleted', False):
            return Result('deleted')
        phone = ''.join(c for c in account.phone if c.isdigit())
        if str(getattr(me, 'phone', '') or '') != phone:
            return Result('mismatch')
        details = {
            'name': ' '.join(filter(None, [me.first_name, me.last_name]))[:128],
            'username': me.username, 'twofa': None, 'groups': None,
            'channels': None, 'spam': 'unknown',
        }
        status = 'restricted' if getattr(me, 'restricted', False) else 'healthy'
        try:
            password = await asyncio.wait_for(client(functions.account.GetPasswordRequest()), 10)
            details['twofa'] = bool(password.has_password)
            # Bounded enumeration: report partial counts rather than pretending
            # a timeout or limit is an exact total. Message content is not saved.
            groups = channels = total = 0

            async def count_dialogs():
                nonlocal groups, channels, total
                async for dialog in client.iter_dialogs(limit=1000):
                    total += 1
                    if dialog.is_group:
                        groups += 1
                    elif dialog.is_channel:
                        channels += 1
            try:
                await asyncio.wait_for(count_dialogs(), 15)
                details['counts_partial'] = total >= 1000
            finally:
                details.update(groups=groups, channels=channels)
        except Exception as exc:
            details['counts_partial'] = True
            details['optional_details_unavailable'] = True
            retry = classify_error(exc)
            # An explicit revocation during a detail fetch is still evidence.
            if retry.status in BLOCKED:
                return retry
            if retry.status == 'flood_wait':
                details['retry_seconds'] = retry.retry_seconds
        return Result(status, details, max(CHECK_INTERVAL, details.get('retry_seconds', 0)))
    except Exception as exc:
        return classify_error(exc)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 5)


def effective_status(previous, result):
    return previous if result.status in {'temporary', 'flood_wait'} else result.status


async def check_account(account, *, manual=False):
    global _slots, _slots_loop
    loop = asyncio.get_running_loop()
    if _slots_loop is not loop:
        _slots, _slots_loop = asyncio.Semaphore(2), loop
    lock = _locks.setdefault(account.key, asyncio.Lock())
    async with lock:
        previous = (await records([account.key])).get(account.key, {})
        if previous.get('status') == 'revoked':
            from session_lifecycle import retire
            if await retire(account.session,phone=account.phone,reason='revoked'):
                return {**previous,'details':{**previous.get('details',{}),'installation_removed':True}}
        now = dt.datetime.now(dt.timezone.utc)
        next_at = previous.get('next_check_at')
        if next_at and next_at > now:
            checked = previous['checked_at']
            if (not manual or previous['attempt'] == 'flood_wait'
                    or (now - checked).total_seconds() < MANUAL_INTERVAL
                    or previous.get('details', {}).get('retry_seconds')):
                return previous
        async with _slots:
            result = await probe(account)
        status = effective_status(previous.get('status', 'unknown'), result)
        confirmed = result.status not in {'temporary', 'flood_wait'}
        details = result.details if confirmed else previous.get('details', {})
        retired = False
        if result.status == 'revoked':
            from session_lifecycle import retire
            retired = await retire(account.session,phone=account.phone,reason='revoked')
            if retired:
                details = {**details,'installation_removed':True}
        await query('''INSERT INTO account_health
            (session_key, phone, inventory_session, status, attempt, details,
             checked_at, confirmed_at, next_check_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP,
                    CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                    CURRENT_TIMESTAMP + (? * INTERVAL '1 second'))
            ON CONFLICT(session_key) DO UPDATE SET phone=excluded.phone,
              inventory_session=excluded.inventory_session, status=excluded.status,
              attempt=excluded.attempt, details=excluded.details,
              checked_at=excluded.checked_at,
              confirmed_at=COALESCE(excluded.confirmed_at, account_health.confirmed_at),
              next_check_at=excluded.next_check_at''',
            (account.key, account.phone, None if retired else account.inventory_session, status,
             result.status, json.dumps(details, ensure_ascii=False), confirmed, result.retry_seconds),
            commit=True)
        return (await records([account.key]))[account.key]


async def deliver_notices(client, account, record):
    lock = _locks.setdefault(account.key, asyncio.Lock())
    async with lock:
        # A newer scan may have completed while this notification waited.
        record = (await records([account.key])).get(account.key, record)
        current = (await accounts_for()).get(account.key)
        if current is not None:
            await _deliver_notices(client, current, record)


async def _deliver_notices(client, account, record):
    # First healthy check is a quiet baseline; first fault and later changes notify.
    for user_id in account.recipients:
        row = await query('''SELECT status FROM account_health_notices
            WHERE session_key=? AND user_id=?''', (account.key, user_id), fetchone=True)
        status = record['status']
        if status == 'unknown' or (row and row[0] == status):
            continue
        if row or status in BLOCKED:
            try:
                await asyncio.wait_for(client.send_message(user_id,
                    f"🩺 تحديث صحة الحساب {mask_phone(account.phone)}\n{LABELS[status]}\n"
                    'راجع «عرض الحسابات ← صحة الحسابات» للتفاصيل.', parse_mode=None), 30)
                await asyncio.sleep(0.25)
            except Exception:
                # No acknowledgment: retry on the next monitor pass.
                logger.warning('Account health notification delivery deferred')
                continue
        await query('''INSERT INTO account_health_notices(session_key, user_id, status)
            VALUES (?, ?, ?) ON CONFLICT(session_key, user_id)
            DO UPDATE SET status=excluded.status''', (account.key, user_id, status), commit=True)


async def monitor(client):
    while True:
        from service_health import tick
        tick('account_health')
        try:
            accounts = await accounts_for()
            state = await records(accounts)
            # Apply the cleanup policy to confirmed revocations from earlier versions,
            # even when their next network probe is still hours away.
            for account in [a for a in accounts.values() if a.installed and state.get(a.key,{}).get('status')=='revoked'][:50]:
                from session_lifecycle import retire
                await retire(account.session,phone=account.phone,reason='revoked')
            accounts = await accounts_for()
            state = await records(accounts)
            now = dt.datetime.now(dt.timezone.utc)
            due = [a for a in accounts.values()
                   if state.get(a.key, {}).get('next_check_at', now) <= now]
            due.sort(key=lambda a: state.get(a.key, {}).get('next_check_at', now))
            for account in due[:50]:
                tick('account_health')
                record = await check_account(account)
                await deliver_notices(client, account, record)
                await asyncio.sleep(1)
            # Re-resolve ownership after scans, including expired rental leases.
            current = await accounts_for()
            state = await records(current)
            for key, account in current.items():
                tick('account_health')
                if key in state:
                    await deliver_notices(client, account, state[key])
        except Exception:
            tick('account_health', True)
            logger.exception('Account health monitor pass failed')
        await asyncio.sleep(60)


async def stop_manual_checks():
    tasks = list(_manual_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _manual_tasks.clear()
