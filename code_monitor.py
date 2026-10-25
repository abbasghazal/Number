"""Per-user multi-channel code monitors that only ever use an owner's accounts."""

import asyncio
import contextlib
import logging
import re

from telethon import errors, functions, types

import account_access
import automation
from account_health import session_is_usable
from managed_twofa import state_key
from postgres import connect
from session_guard import SessionBusyError
from shahm_engine import BotSession, ShahmError, normalized_text, profile_for, parse_telegram_link
from utils.asyncio_compat import to_thread
from workflow import query, queue_notice

LOG = logging.getLogger(__name__)
_workers = {}
_next_poll = 0


def parse_code(text, allowed):
    text = text or ''
    normalized = normalized_text(text)
    if 'نقاط' not in normalized or 'كود' not in normalized:
        return None
    match = re.search(r'(?:الكود|كود)\s*[:：]\s*([A-Za-z0-9_-]{1,64})(?![A-Za-z0-9_-])', text)
    if not match:
        return None
    mentions = {'@' + item.lower() for item in re.findall(r'@([A-Za-z][A-Za-z0-9_]{4,31})', text)}
    mentions.update('@' + item.lower() for item in re.findall(r'https?://t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?:\b|/)', text))
    candidates = mentions & {item.lower() for item in allowed}
    return (candidates.pop(), match[1]) if len(candidates) == 1 else None


async def _ensure_config(owner):
    await query("INSERT INTO user_code_monitor_config(owner_id) VALUES(?) ON CONFLICT(owner_id) DO NOTHING", (owner,), commit=True)


async def configuration(owner):
    await _ensure_config(owner)
    return await query("SELECT enabled,updated_at FROM user_code_monitor_config WHERE owner_id=?", (owner,), fetchone=True)


async def sources(owner):
    await _ensure_config(owner)
    return await query(
        "SELECT source_id,access_hash,source,monitor_phone,last_message,enabled FROM user_code_monitor_sources WHERE owner_id=? ORDER BY source",
        (owner,), fetchall=True,
    ) or []


async def owned_account(owner, phone, fingerprint=None):
    """Resolve only an account owned by this exact user, never inventory/grants."""
    account = await account_access.resolve(owner, phone, owned_only=True)
    if not account or len(account) < 3 or account[2] != owner:
        raise PermissionError()
    if fingerprint and state_key(phone, account[1]) != fingerprint:
        raise PermissionError()
    if not await session_is_usable(account[1]):
        raise PermissionError()
    if not await account_access.authorized(owner, account, owned_only=True):
        raise PermissionError()
    return account


async def checkpoint(owner, account):
    row = await query("SELECT enabled FROM user_code_monitor_config WHERE owner_id=?", (owner,), fetchone=True)
    if not row or not row[0]:
        raise PermissionError()
    await owned_account(owner, account[0], state_key(account[0], account[1]))


async def pause(seconds, owner, account):
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        await asyncio.sleep(min(5, end - asyncio.get_running_loop().time()))
        await checkpoint(owner, account)


async def add_source(owner, raw, phone):
    await _ensure_config(owner)
    account = await owned_account(owner, phone)
    raw = (raw or '').strip()
    peer = None
    if re.fullmatch(r'-100[0-9]{5,15}', raw):
        peer = int(raw)
    else:
        parsed = parse_telegram_link(raw)
        if not parsed or parsed[0] != 'public':
            raise ValueError('أرسل @معرف قناة عامة أو رابط t.me عام أو معرفًا رقميًا -100…')
        peer = parsed[1]
    async with automation.connection(account) as client:
        me = await client.get_me()
        if not me or me.bot or str(me.phone).lstrip('+') != phone:
            raise ValueError("جلسة المراقبة لا تطابق الحساب المملوك المختار.")
        if isinstance(peer, int):
            entity = None
            for dialog in await client.get_dialogs(limit=None):
                candidate = getattr(dialog, 'entity', None)
                if isinstance(candidate, types.Channel) and -1000000000000 - candidate.id == peer:
                    entity = candidate
                    break
            if entity is None:
                raise ValueError("الحساب المختار لا يستطيع الوصول إلى هذا المعرف الرقمي. استخدم @المعرف العام إن وجد.")
        else:
            entity = await client.get_entity(peer)
        if not isinstance(entity, types.Channel):
            raise ValueError("مصدر المراقبة ليس قناة.")
        try:
            await client(functions.channels.JoinChannelRequest(entity))
        except errors.UserAlreadyParticipantError:
            pass
        messages = await client.get_messages(entity, limit=1)
        last = messages[0].id if messages else 0
    source = '@' + entity.username if getattr(entity, 'username', None) else str(-1000000000000 - entity.id)
    await query(
        """INSERT INTO user_code_monitor_sources(owner_id,source_id,access_hash,source,monitor_phone,last_message,enabled)
           VALUES(?,?,?,?,?,?,TRUE) ON CONFLICT(owner_id,source_id) DO UPDATE SET access_hash=excluded.access_hash,source=excluded.source,
           monitor_phone=excluded.monitor_phone,last_message=GREATEST(user_code_monitor_sources.last_message,excluded.last_message),
           enabled=TRUE,updated_at=CURRENT_TIMESTAMP""",
        (owner, entity.id, getattr(entity, 'access_hash', None), source, phone, last), commit=True,
    )
    return source


async def remove_source(owner, source_id):
    row = await query("DELETE FROM user_code_monitor_sources WHERE owner_id=? AND source_id=? RETURNING source", (owner, int(source_id)), commit=True, fetchone=True)
    if not row:
        raise ValueError('مصدر المراقبة غير موجود.')
    return row[0]


async def configure(owner, phone, enabled):
    await _ensure_config(owner)
    if not enabled:
        await query("UPDATE user_code_monitor_config SET enabled=FALSE,updated_at=CURRENT_TIMESTAMP WHERE owner_id=?", (owner,), commit=True)
        return
    existing = await sources(owner)
    if not existing and phone:
        legacy = await query("SELECT source FROM user_code_monitor_config WHERE owner_id=?", (owner,), fetchone=True)
        if legacy and legacy[0]:
            await add_source(owner, legacy[0], phone)
            existing = await sources(owner)
    if not existing:
        raise ValueError('أضف قناة مراقبة واحدة على الأقل قبل التشغيل.')
    await query("UPDATE user_code_monitor_config SET enabled=TRUE,updated_at=CURRENT_TIMESTAMP WHERE owner_id=?", (owner,), commit=True)


def enqueue_code(owner, source, message, bot, code, accounts):
    with connect() as db:
        key = db.execute(
            """INSERT INTO user_monitored_codes(owner_id,source_id,message_id,bot,code) VALUES(?,?,?,?,?)
               ON CONFLICT(owner_id,bot,code) DO NOTHING RETURNING code_id""",
            (owner, source, message, bot.lower(), code),
        ).fetchone()
        if not key:
            return None
        for account in accounts:
            if len(account) < 3 or account[2] != owner:
                continue
            db.execute(
                "INSERT INTO user_monitored_code_accounts(code_id,phone,session_key) VALUES(?,?,?)",
                (key[0], account[0], state_key(account[0], account[1])),
            )
        return key[0]


async def poll_source(owner, source_id, access_hash, source, phone, last):
    account = await owned_account(owner, phone)
    allowed = [item[0] for item in await query(
        "SELECT username FROM user_code_monitor_bots WHERE owner_id=? AND enabled", (owner,), fetchall=True
    ) or []]
    if not allowed:
        return
    async with automation.connection(account) as client:
        me = await client.get_me()
        if not me or me.bot or str(me.phone).lstrip('+') != phone:
            raise PermissionError()
        entity = await client.get_entity(types.InputChannel(source_id, access_hash)) if access_hash else await client.get_entity(source)
        if not isinstance(entity, types.Channel) or entity.id != source_id:
            raise ValueError('monitor_source_changed')
        messages = await client.get_messages(entity, min_id=last, limit=50, reverse=True)
        accounts = await account_access.accounts(owner, owned_only=True)
        for message in sorted(messages, key=lambda item: item.id):
            if message.id <= last or getattr(message, 'out', False):
                continue
            parsed = parse_code(message.raw_text, allowed)
            if parsed:
                await to_thread(enqueue_code, owner, source_id, message.id, *parsed, accounts)
            await query(
                "UPDATE user_code_monitor_sources SET last_message=GREATEST(last_message,?),updated_at=CURRENT_TIMESTAMP WHERE owner_id=? AND source_id=?",
                (message.id, owner, source_id), commit=True,
            )


async def poll(owner):
    cfg = await configuration(owner)
    if not cfg or not cfg[0]:
        return
    for source_id, access_hash, source, phone, last, enabled in await sources(owner):
        if not enabled:
            continue
        try:
            await poll_source(owner, source_id, access_hash, source, phone, last)
        except errors.FloodWaitError as exc:
            await automation.save_wait(phone, exc.seconds + 1)
        except (PermissionError, SessionBusyError):
            continue
        except Exception as exc:
            LOG.warning('Channel code source deferred (%s)', type(exc).__name__)


async def redeem(owner, code_id, phone, fingerprint):
    row = await query(
        """UPDATE user_monitored_code_accounts SET status='preparing',updated_at=CURRENT_TIMESTAMP
           WHERE code_id=? AND phone=? AND status='pending' AND next_attempt<=CURRENT_TIMESTAMP RETURNING phone""",
        (code_id, phone), commit=True, fetchone=True,
    )
    if not row:
        return
    sending = False
    status, result, delay = 'failed', 'تعذر تنفيذ الكود.', None
    try:
        account = await owned_account(owner, phone, fingerprint)
        await checkpoint(owner, account)
        data = await query(
            """SELECT c.bot,c.code,b.button_text FROM user_monitored_codes c
               JOIN user_code_monitor_bots b ON b.owner_id=c.owner_id AND b.username=c.bot AND b.enabled
               WHERE c.code_id=? AND c.owner_id=?""",
            (code_id, owner), fetchone=True,
        )
        if not data:
            raise PermissionError()
        async with automation.connection(account) as client:
            me = await client.get_me()
            if not me or me.bot or str(me.phone).lstrip('+') != phone:
                raise PermissionError()
            session = BotSession(
                client, profile_for(data[0]), lambda: checkpoint(owner, account),
                lambda seconds: pause(seconds, owner, account),
            )
            await session.prepare()

            async def before_send():
                nonlocal sending
                await checkpoint(owner, account)
                allowed = await query(
                    "SELECT enabled FROM user_code_monitor_bots WHERE owner_id=? AND username=?", (owner, data[0]), fetchone=True
                )
                if not allowed or not allowed[0]:
                    raise PermissionError()
                await query("UPDATE user_monitored_code_accounts SET status='sending',updated_at=CURRENT_TIMESTAMP WHERE code_id=? AND phone=?", (code_id, phone), commit=True)
                sending = True

            status, result = await session.redeem_code(data[1], before_send, data[2])
    except errors.FloodWaitError as exc:
        await automation.save_wait(phone, exc.seconds + 1)
        status, result, delay = ('unknown', 'انقطع الرد بعد بدء إرسال الكود.', None) if sending else ('pending', 'انتظار تيليجرام.', exc.seconds + 1)
    except SessionBusyError:
        status, result, delay = ('unknown', 'تعذر تأكيد الرد بعد الإرسال.', None) if sending else ('pending', 'الحساب مشغول بخدمة أخرى.', 60)
    except asyncio.CancelledError:
        status, result, delay = ('unknown', 'توقف التشغيل أثناء إرسال الكود.', None) if sending else ('pending', 'توقف قبل إرسال الكود.', 60)
        raise
    except (PermissionError, ShahmError) as exc:
        status, result = ('unknown' if sending else 'failed'), (str(exc) if isinstance(exc, ShahmError) else 'انتهى الإذن أو توقفت المراقبة.')
    except Exception as exc:
        LOG.warning('Code attempt deferred (%s)', type(exc).__name__)
        status, result, delay = ('unknown', 'تعذر تأكيد الرد بعد الإرسال.', None) if sending else ('pending', 'تعذر الاتصال قبل الإرسال.', 300)
    finally:
        await query(
            """UPDATE user_monitored_code_accounts SET status=?,result=?,next_attempt=CURRENT_TIMESTAMP+?*INTERVAL '1 second',
               updated_at=CURRENT_TIMESTAMP WHERE code_id=? AND phone=?""",
            (status, result[:1200], delay or 0, code_id, phone), commit=True,
        )
        if status != 'pending':
            await queue_notice(owner, f'monitored-code:{owner}:{code_id}:{phone}', f'كود #{code_id} · +{phone}\nالحالة: {status}\n{result[:1200]}')



async def tick():
    global _next_poll
    if asyncio.get_running_loop().time() >= _next_poll:
        owners = await query("SELECT owner_id FROM user_code_monitor_config WHERE enabled", fetchall=True) or []
        for (owner,) in owners:
            await poll(owner)
        _next_poll = asyncio.get_running_loop().time() + 30
    rows = await query(
        """SELECT c.owner_id,a.code_id,a.phone,a.session_key FROM user_monitored_code_accounts a
           JOIN user_monitored_codes c ON c.code_id=a.code_id
           JOIN user_code_monitor_config cfg ON cfg.owner_id=c.owner_id AND cfg.enabled
           WHERE a.status='pending' AND a.next_attempt<=CURRENT_TIMESTAMP ORDER BY a.code_id,a.phone LIMIT 30""",
        fetchall=True,
    ) or []
    for owner, code_id, phone, fingerprint in rows:
        key = (owner, phone)
        if key in _workers:
            continue
        task = asyncio.create_task(redeem(owner, code_id, phone, fingerprint), name=f'code-{owner}-{code_id}-{phone}')
        _workers[key] = task

        def finished(done, key=key):
            _workers.pop(key, None)
            if not done.cancelled() and done.exception():
                LOG.error('Code worker failed (%s)', type(done.exception()).__name__)

        task.add_done_callback(finished)


async def recover():
    await query("UPDATE user_monitored_code_accounts SET status='pending' WHERE status='preparing'", commit=True)
    await query("UPDATE user_monitored_code_accounts SET status='unknown',result='انقطع التشغيل أثناء الإرسال؛ لم يُكرر الكود.' WHERE status='sending'", commit=True)


async def stop(owner=None):
    tasks = [task for (task_owner, _), task in list(_workers.items()) if owner is None or task_owner == owner]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
