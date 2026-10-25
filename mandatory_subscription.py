"""Paid user-owned mandatory-channel subscriptions."""
import asyncio
import logging
from decimal import Decimal, InvalidOperation

from helpers import execute_query, get_force_channels, is_user_in_channel
from postgres import connect
from utils.asyncio_compat import to_thread


logger = logging.getLogger(__name__)


TIME_PACKAGES = {
    "1h": ("ساعة واحدة", 1, Decimal("5")),
    "2h": ("ساعتان", 2, Decimal("10")),
    "5h": ("خمس ساعات", 5, Decimal("20")),
    "1d": ("يوم كامل", 24, Decimal("50")),
}
MAX_PAID_CHANNELS = 15
REOPEN_PAID_CHANNELS_AT = 10
_LOCK_SETTING = "paid_force_channels_admission_locked"
_delivery_tasks = {}


def _price(value):
    try:
        price = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("أدخل سعراً موجباً صحيحاً.") from None
    if not price.is_finite() or price <= 0:
        raise ValueError("أدخل سعراً موجباً صحيحاً.")
    return price


async def time_prices():
    prices = {}
    for key, (_label, _hours, default) in TIME_PACKAGES.items():
        row = await execute_query("SELECT value FROM settings WHERE key=?", (f"force_time_price:{key}",), fetchone=True)
        try:
            prices[key] = _price(row[0]) if row and row[0] is not None else default
        except ValueError:
            prices[key] = default
    return prices


async def count_prices():
    return await execute_query(
        "SELECT join_count,price FROM force_count_prices ORDER BY join_count",
        fetchall=True,
    ) or []


async def set_time_price(package, value):
    if package not in TIME_PACKAGES:
        raise ValueError("باقة الوقت غير صالحة.")
    price = _price(value)
    await execute_query(
        """INSERT INTO settings(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,last_updated=CURRENT_TIMESTAMP""",
        (f"force_time_price:{package}", str(price)),
        commit=True,
    )
    return price


async def set_count_price(join_count, value):
    try:
        join_count = int(join_count)
    except (ValueError, TypeError):
        raise ValueError("عدد المشتركين غير صالح.") from None
    if join_count <= 0 or join_count > 1_000_000:
        raise ValueError("عدد المشتركين يجب أن يكون بين 1 و1000000.")
    price = _price(value)
    await execute_query(
        """INSERT INTO force_count_prices(join_count,price) VALUES(?,?)
           ON CONFLICT(join_count) DO UPDATE SET price=excluded.price""",
        (join_count, price),
        commit=True,
    )
    return join_count, price


async def delete_count_price(join_count):
    await execute_query("DELETE FROM force_count_prices WHERE join_count=?", (int(join_count),), commit=True)


async def cleanup_expired():
    await execute_query(
        """UPDATE paid_force_channels SET active=FALSE
           WHERE active=TRUE AND (
               (mode='time' AND expires_at<=CURRENT_TIMESTAMP)
               OR (mode='count' AND joins_used>=join_limit)
           )""",
        commit=True,
    )


async def active_user_channels(owner_id=None):
    await cleanup_expired()
    sql = """SELECT subscription_id,channel_id,channel_name,mode,price,expires_at,join_limit,joins_used
             FROM paid_force_channels WHERE active=TRUE"""
    args = ()
    if owner_id is not None:
        sql += " AND owner_id=?"
        args = (owner_id,)
    return await execute_query(sql + " ORDER BY created_at DESC", args, fetchall=True) or []


async def required_paid_channels():
    await cleanup_expired()
    return await execute_query(
        "SELECT subscription_id,channel_id,mode FROM paid_force_channels WHERE active=TRUE ORDER BY subscription_id",
        fetchall=True,
    ) or []


def _purchase_sync(owner_id, channel_id, channel_name, mode, price, *, hours=None, join_limit=None):
    """Debit the owner and create a subscription in one database transaction."""
    with connect() as db:
        db.execute(
            """UPDATE paid_force_channels SET active=FALSE
               WHERE active=TRUE AND ((mode='time' AND expires_at<=CURRENT_TIMESTAMP)
               OR (mode='count' AND joins_used>=join_limit))"""
        )
        db.execute(
            "INSERT INTO settings(key,value) VALUES (?, '0') ON CONFLICT(key) DO NOTHING",
            (_LOCK_SETTING,),
        )
        lock_row = db.execute("SELECT value FROM settings WHERE key=? FOR UPDATE", (_LOCK_SETTING,)).fetchone()
        active_count = db.execute("SELECT COUNT(*) FROM paid_force_channels WHERE active=TRUE").fetchone()[0]
        locked = str(lock_row[0] or "0") == "1"
        if active_count >= MAX_PAID_CHANNELS:
            locked = True
        elif active_count <= REOPEN_PAID_CHANNELS_AT:
            locked = False
        db.execute("UPDATE settings SET value=?,last_updated=CURRENT_TIMESTAMP WHERE key=?", ("1" if locked else "0", _LOCK_SETTING))
        if locked:
            db.rollback()
            return False, "اكتمل الحد الأقصى لقنوات المستخدمين. ستتاح الإضافة مجدداً عندما ينخفض العدد إلى 10 قنوات أو أقل.", None

        if db.execute("SELECT 1 FROM force_channels WHERE channel_id=?", (channel_id,)).fetchone():
            db.rollback()
            return False, "هذه قناة إجبارية أضافها المطور بالفعل.", None
        if db.execute("SELECT 1 FROM paid_force_channels WHERE channel_id=? AND active=TRUE", (channel_id,)).fetchone():
            db.rollback()
            return False, "هذه القناة مشتركة بالفعل ضمن الاشتراك الإجباري.", None

        balance = db.execute(
            "UPDATE users SET coins=coins-? WHERE user_id=? AND coins>=? RETURNING coins",
            (price, owner_id, price),
        ).fetchone()
        if not balance:
            db.rollback()
            return False, "رصيدك غير كافٍ لهذه الباقة.", None

        expires_at = None
        if mode == "time":
            row = db.execute("SELECT CURRENT_TIMESTAMP + (? * INTERVAL '1 hour')", (int(hours),)).fetchone()
            expires_at = row[0]
        subscription = db.execute(
            """INSERT INTO paid_force_channels(channel_id,channel_name,owner_id,mode,price,expires_at,join_limit)
               VALUES(?,?,?,?,?,?,?) RETURNING subscription_id""",
            (channel_id, channel_name, owner_id, mode, price, expires_at, join_limit),
        ).fetchone()[0]
        db.execute(
            """INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference)
               VALUES(?,?,?,?,?)""",
            (owner_id, -price, balance[0], "mandatory_channel_purchase", str(subscription)),
        )
        if active_count + 1 >= MAX_PAID_CHANNELS:
            db.execute("UPDATE settings SET value='1',last_updated=CURRENT_TIMESTAMP WHERE key=?", (_LOCK_SETTING,))
        db.commit()
        return True, balance[0], subscription


async def purchase(owner_id, channel_id, channel_name, package, *, count_mode=False):
    channel_id = str(channel_id or "").strip().lstrip("@").lower()
    if not channel_id or "/" in channel_id or " " in channel_id:
        raise ValueError("معرّف القناة غير صالح.")
    if count_mode:
        row = await execute_query("SELECT price FROM force_count_prices WHERE join_count=?", (int(package),), fetchone=True)
        if not row:
            raise ValueError("باقة العدد لم تعد متاحة.")
        price = _price(row[0])
        return await to_thread(_purchase_sync, owner_id, channel_id, channel_name, "count", price, join_limit=int(package))

    if package not in TIME_PACKAGES:
        raise ValueError("باقة الوقت غير صالحة.")
    price = (await time_prices())[package]
    return await to_thread(
        _purchase_sync, owner_id, channel_id, channel_name, "time", price,
        hours=TIME_PACKAGES[package][1],
    )


async def _delivered_account_keys(subscription_id):
    rows = await execute_query(
        "SELECT account_key FROM paid_force_channel_accounts WHERE subscription_id=?",
        (subscription_id,), fetchall=True,
    ) or []
    return {row[0] for row in rows}


async def _platform_join_accounts():
    """Return every distinct, locally-held session eligible for a count order.

    The count service is a platform feature, so its pool deliberately covers
    the platform inventory, stored sessions, pending listings and active
    account auctions.  Duplicate copies of one Telegram session are joined
    only once.
    """
    rows = await execute_query(
        """SELECT phone,session,'inventory' FROM accounts
           WHERE is_sold=FALSE AND session IS NOT NULL
           UNION ALL SELECT phone,session,'stored' FROM stored_accounts WHERE session IS NOT NULL
           UNION ALL SELECT phone,session,'pending_sale' FROM pending_sales WHERE session IS NOT NULL
           UNION ALL SELECT phone,session,'auction' FROM auctions
             WHERE status='active' AND session IS NOT NULL
           UNION ALL SELECT phone,session,'purchased' FROM purchased_accounts WHERE session IS NOT NULL""",
        fetchall=True,
    ) or []
    from account_health import session_key
    from helpers import decrypt_session
    result = []
    seen = set()
    for phone, encrypted_session, source in rows:
        session = decrypt_session(encrypted_session)
        if not session:
            continue
        key = session_key(session)
        if key in seen:
            continue
        seen.add(key)
        result.append((str(phone), session, source, key))
    return result


async def _join_as_new_member(session, channel_id):
    """Join one public channel and report only a genuinely new membership."""
    from config import API_HASH, API_ID
    from session_guard import TelegramClient
    from telethon.errors import UserAlreadyParticipantError
    from telethon.sessions import StringSession
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.types import Channel

    account_client = None
    try:
        account_client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await asyncio.wait_for(account_client.connect(), timeout=20)
        entity = await asyncio.wait_for(account_client.get_entity(channel_id.lstrip("@")), timeout=20)
        if not isinstance(entity, Channel):
            return False
        try:
            await asyncio.wait_for(account_client(JoinChannelRequest(entity)), timeout=30)
            return True
        except UserAlreadyParticipantError:
            # Existing memberships are not sold again as a new addition.
            return False
    finally:
        if account_client and account_client.is_connected():
            await account_client.disconnect()


def _record_account_delivery_sync(subscription_id, account_key, phone):
    """Count one newly joined bot-controlled account, never above the package limit."""
    with connect() as db:
        subscription = db.execute(
            """SELECT owner_id,channel_id,joins_used,join_limit FROM paid_force_channels
               WHERE subscription_id=? AND active=TRUE AND mode='count' FOR UPDATE""",
            (subscription_id,),
        ).fetchone()
        if not subscription or int(subscription[2]) >= int(subscription[3] or 0):
            db.rollback()
            return None
        added = db.execute(
            """INSERT INTO paid_force_channel_accounts(subscription_id,account_key,phone)
               VALUES(?,?,?) ON CONFLICT(subscription_id,account_key) DO NOTHING
               RETURNING account_key""",
            (subscription_id, account_key, phone),
        ).fetchone()
        if not added:
            db.commit()
            return False, int(subscription[2]), int(subscription[3]), None, None
        used = int(subscription[2]) + 1
        completed = used >= int(subscription[3])
        if completed:
            db.execute(
                """UPDATE paid_force_channels SET joins_used=?,active=FALSE,completed_at=CURRENT_TIMESTAMP
                   WHERE subscription_id=?""",
                (used, subscription_id),
            )
        else:
            db.execute("UPDATE paid_force_channels SET joins_used=? WHERE subscription_id=?", (used, subscription_id))
        db.commit()
        return True, used, int(subscription[3]), subscription[0] if completed else None, subscription[1] if completed else None


async def _deliver_count_package(client, subscription_id):
    """Join a count package with only the platform's own service inventory."""
    try:
        row = await execute_query(
            """SELECT channel_id,joins_used,join_limit FROM paid_force_channels
               WHERE subscription_id=? AND active=TRUE AND mode='count'""",
            (subscription_id,), fetchone=True,
        )
        if not row:
            return
        channel_id, used, limit = row
        if int(used) >= int(limit or 0):
            return

        from account_health import session_is_usable
        accounts = await _platform_join_accounts()
        delivered = await _delivered_account_keys(subscription_id)

        for phone, session, _source, account_key in accounts:
            if len(delivered) >= int(limit):
                break
            if account_key in delivered or not await session_is_usable(session):
                continue
            try:
                added_now = await _join_as_new_member(session, channel_id)
            except Exception as exc:
                logger.info("Mandatory count join skipped for %s: %s", phone, type(exc).__name__)
                continue
            if not added_now:
                continue

            result = await to_thread(_record_account_delivery_sync, subscription_id, account_key, phone)
            if not result:
                break
            inserted, used, limit, completed_owner, completed_channel = result
            if inserted:
                delivered.add(account_key)
            if completed_owner:
                try:
                    await client.send_message(
                        completed_owner,
                        f"✅ اكتمل اشتراك العدد لقناتك @{completed_channel}.\n"
                        f"اكتمل العدد المطلوب: {used}/{limit} مشترك.",
                    )
                except Exception:
                    logger.exception("Could not notify completed mandatory subscription %s", subscription_id)
                break
            # A small gap protects the service inventory from join-rate limits.
            await asyncio.sleep(1)
    except Exception:
        logger.exception("Mandatory count delivery failed for subscription %s", subscription_id)
    finally:
        _delivery_tasks.pop(subscription_id, None)


def start_count_delivery(client, subscription_id):
    """Start or resume one count package without duplicating its worker."""
    current = _delivery_tasks.get(subscription_id)
    if current and not current.done():
        return current
    task = asyncio.create_task(
        _deliver_count_package(client, int(subscription_id)),
        name=f"mandatory-count-{subscription_id}",
    )
    _delivery_tasks[int(subscription_id)] = task
    return task


async def resume_count_deliveries(client):
    """Resume incomplete count packages after startup and when new inventory appears."""
    await cleanup_expired()
    rows = await execute_query(
        "SELECT subscription_id FROM paid_force_channels WHERE active=TRUE AND mode='count' AND joins_used<join_limit",
        fetchall=True,
    ) or []
    for (subscription_id,) in rows:
        start_count_delivery(client, subscription_id)
    return len(rows)


async def verify_required_subscriptions(client, user_id):
    """Check every mandatory channel and count each paid count-mode join once."""
    for channel in await get_force_channels():
        if not await is_user_in_channel(client, user_id, channel):
            return False, channel
    for subscription_id, channel, mode in await required_paid_channels():
        if not await is_user_in_channel(client, user_id, channel):
            return False, channel
    return True, None


async def remove_owned_subscription(owner_id, subscription_id):
    row = await execute_query(
        """UPDATE paid_force_channels SET active=FALSE
           WHERE subscription_id=? AND owner_id=? AND active=TRUE RETURNING channel_id""",
        (int(subscription_id), owner_id), fetchone=True, commit=True,
    )
    return row[0] if row else None
