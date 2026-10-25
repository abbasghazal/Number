"""Reconcile the requested shop bio across every persisted account source."""
import asyncio
import contextlib
import hashlib
import logging

from telethon import errors, functions
from telethon.sessions import StringSession

from account_health import session_key
from config import API_ID, API_HASH
from session_guard import TelegramClient, SessionBusyError
from workflow import query

logger = logging.getLogger(__name__)
OFFICIAL_BIO = 'المتجر الرسمي لبيع الارقام الافتراضية : @applyshahm'
SOURCES = ('stored_accounts', 'purchased_accounts', 'accounts', 'pending_sales',
           'pending_purchases', 'auctions', 'sold_accounts', 'active_accounts',
           'scheduled_twofa_changes')


def migrate(c):
    c.execute('''CREATE TABLE IF NOT EXISTS account_bio_state (
        session_key TEXT PRIMARY KEY,phone TEXT NOT NULL,status TEXT NOT NULL,
        next_run_at TIMESTAMP NOT NULL,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')


async def apply_bio(phone, session):
    client = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH,
                                exclusive_session=True, flood_sleep_threshold=0,
                                request_retries=0, connection_retries=1)
        await asyncio.wait_for(client.connect(), 20)
        if not await asyncio.wait_for(client.is_user_authorized(), 20):
            return 'unauthorized', 21600
        me = await asyncio.wait_for(client.get_me(), 20)
        if not me or me.bot or str(me.phone or '').lstrip('+') != phone:
            return 'mismatch', 21600
        profile = await asyncio.wait_for(client(functions.users.GetFullUserRequest('me')), 20)
        if getattr(profile.full_user, 'about', '') != OFFICIAL_BIO:
            await asyncio.wait_for(client(functions.account.UpdateProfileRequest(about=OFFICIAL_BIO)), 20)
        return 'applied', 21600
    except SessionBusyError:
        return 'busy', 60
    except errors.FloodWaitError as exc:
        return 'flood_wait', max(60, exc.seconds + 1)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Never include session strings or request data in logs or state.
        logger.info('Account bio update deferred (%s)', type(exc).__name__)
        return 'retry', 300
    finally:
        if client:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 15)


async def reconcile(limit=20):
    from helpers import decrypt_session
    import service_health
    # Read current account rows each pass: removed accounts are never retried
    # from a stale queue. Imports, sales and transactional purchases all feed
    # these same tables, including accounts saved before this feature existed.
    rows = await query(' UNION '.join(f'SELECT phone,session FROM {table}' for table in SOURCES), fetchall=True)
    state = dict(await query('''SELECT session_key,next_run_at>CURRENT_TIMESTAMP
        FROM account_bio_state ORDER BY updated_at,session_key''', fetchall=True) or [])
    rank = {key: index for index, key in enumerate(state)}
    candidates = {}
    processed = 0
    for phone, encrypted in rows or []:
        session = decrypt_session(encrypted)
        if not session:
            continue
        phone = str(phone).lstrip('+')
        key = hashlib.sha256(f'{phone}:{session_key(session)}'.encode()).hexdigest()
        if state.get(key):
            continue
        candidates[key] = (phone, session)
    # New and least-recently-attempted accounts go first, so a batch of busy
    # sessions cannot starve the rest of the inventory on every minute's pass.
    for key in sorted(candidates, key=lambda key: (key in rank, rank.get(key, -1))):
        phone, session = candidates[key]
        status, delay = await apply_bio(phone, session)
        await query('''INSERT INTO account_bio_state(session_key,phone,status,next_run_at)
            VALUES(?,?,?,CURRENT_TIMESTAMP+(? * INTERVAL '1 second'))
            ON CONFLICT(session_key) DO UPDATE SET status=excluded.status,
            next_run_at=excluded.next_run_at,updated_at=CURRENT_TIMESTAMP''',
            (key, phone, status, delay), commit=True)
        processed += 1
        service_health.tick('account_bio')
        if processed >= limit:
            break
        await asyncio.sleep(2)
    return processed


async def monitor():
    import service_health
    while True:
        try:
            await reconcile()
            service_health.tick('account_bio')
        except Exception:
            service_health.tick('account_bio', True)
            logger.exception('Account bio reconciliation failed')
        await asyncio.sleep(60)
