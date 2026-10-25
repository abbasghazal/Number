import re
import contextlib
import asyncio
from utils.asyncio_compat import to_thread
import random
import datetime
import logging
import secrets
import string
import math
from urllib.parse import urlparse
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import sys
from decimal import Decimal, ROUND_CEILING
from datetime import timedelta
from utils.privacy import mask_phone
import workflow
from account_health import filter_usable_accounts, session_is_usable
from config import API_ID, API_HASH, ADMIN_ID, GROUP_NAMES, SESSION_ENCRYPTION_KEY, TONAPI_BASE_URL, TONAPI_KEY, manual_creation_tasks, auto_creation_tasks
from postgres import DatabaseError, connect
from messages import msgs
from telethon.sessions import StringSession
from telethon import events, errors, password as telethon_password
from session_guard import TelegramClient, SessionBusyError
from telethon.tl.functions.account import GetAuthorizationsRequest, GetPasswordRequest, ResetAuthorizationRequest, UpdatePasswordSettingsRequest, UpdateProfileRequest
from telethon.tl.functions.channels import JoinChannelRequest, CreateChannelRequest, GetParticipantRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, SendMessageRequest, EditChatDefaultBannedRightsRequest, GetMessagesViewsRequest, GetDiscussionMessageRequest, ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.types import ChatBannedRights, InputPeerChannel
from telethon.tl.types.account import PasswordInputSettings
from telethon.errors import FloodWaitError, ChannelPrivateError, UserNotParticipantError, UserAlreadyParticipantError
from telethon.tl import functions, types
from cryptography.fernet import Fernet, InvalidToken

# إعداد نظام تسجيل الأخطاء
logger = logging.getLogger(__name__)
bot = None
user_posting_accounts = {}
_auto_engagement_in_progress = set()
_running_posting_task_ids = set()
_posting_worker_tasks = set()
_posting_worker_owners = {}


def _tonapi_username_auction_check_sync(username):
    """Return True only when TonAPI reports the public t.me domain in its auction flow."""
    if not TONAPI_KEY:
        return None, "TONAPI_KEY غير مضبوط"
    domain = f"{username.lower()}.t.me"
    url = f"{TONAPI_BASE_URL}/v2/dns/{quote(domain, safe='.')}/bids"
    request = Request(url, headers={"Authorization": f"Bearer {TONAPI_KEY}", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=8) as response:
            # A successful domain-bids response means TonAPI recognizes an
            # active auction record for this collectible t.me domain. It can
            # legitimately contain zero bids, so do not use list length.
            if 200 <= response.status < 300:
                return True, None
            return None, f"TonAPI أعاد الحالة {response.status}"
    except HTTPError as exc:
        if exc.code == 404:
            return False, None
        if exc.code in (401, 403):
            return None, "مفتاح TonAPI غير صالح أو لا يملك صلاحية الفحص"
        return None, f"TonAPI أعاد الحالة {exc.code}"
    except (URLError, TimeoutError, OSError):
        return None, "تعذر الاتصال بـ TonAPI"


async def is_username_listed_on_tonapi(username):
    """Return True only for a confirmed TonAPI auction; None means the service was unavailable."""
    return await to_thread(_tonapi_username_auction_check_sync, username)
_posting_flood_wait_until = {}


def start_posting_worker(coroutine, *, name=None, user_id=None, task_id=None):
    """Keep a strong reference so posting workers can shut down cleanly."""
    task = asyncio.create_task(coroutine, name=name)
    _posting_worker_tasks.add(task)
    _posting_worker_owners[task] = (user_id, task_id)

    def forget_worker(done_task):
        _posting_worker_tasks.discard(done_task)
        _posting_worker_owners.pop(done_task, None)

    task.add_done_callback(forget_worker)
    return task


async def stop_user_posting_workers(user_id, task_ids=None):
    """Stop only posting workers belonging to one user and wait for cleanup."""
    wanted_ids = set(task_ids) if task_ids is not None else None
    tasks = [
        task for task, (owner_id, task_id) in list(_posting_worker_owners.items())
        if owner_id == user_id and (wanted_ids is None or task_id in wanted_ids)
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return len(tasks)


async def stop_posting_workers():
    """Cancel workers and wait for their Telethon clients to disconnect."""
    tasks = list(_posting_worker_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

_SESSION_PREFIX = "enc:"
TWOFA_CHANGE_NOTICE = "🔐 سيعيّن النظام كلمة تحقق بخطوتين ثابتة، ويحفظها مشفرة. لا تظهر إلا عند عرضها صراحة من إدارة 2FA. يُؤجل التغيير إذا طلب Telegram الانتظار."

def _get_fernet():
    if not SESSION_ENCRYPTION_KEY:
        return None
    try:
        return Fernet(SESSION_ENCRYPTION_KEY.encode())
    except ValueError:
        logger.error("Invalid SESSION_ENCRYPTION_KEY; generate it with Fernet.generate_key()")
        return None

def encrypt_session(session):
    if not session or str(session).startswith(_SESSION_PREFIX):
        return session
    fernet = _get_fernet()
    if not fernet:
        raise RuntimeError("SESSION_ENCRYPTION_KEY is required to store sessions")
    token = fernet.encrypt(str(session).encode("utf-8")).decode("utf-8")
    return _SESSION_PREFIX + token

def decrypt_session(session):
    if not session or not str(session).startswith(_SESSION_PREFIX):
        return session
    fernet = _get_fernet()
    if not fernet:
        raise RuntimeError("SESSION_ENCRYPTION_KEY is required to read encrypted sessions")
    try:
        return fernet.decrypt(str(session)[len(_SESSION_PREFIX):].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt stored session")
        return None

def _replace_row_value(row, index, value):
    if row is None:
        return None
    row = list(row)
    if len(row) > index:
        row[index] = value
    return tuple(row)

def _decrypt_session_row(row, index=1, secret_indexes=()):
    if row is None:
        return None
    row = _replace_row_value(row, index, decrypt_session(row[index]))
    for secret_index in secret_indexes:
        row = _replace_row_value(row, secret_index, decrypt_session(row[secret_index]))
    return row

def _decrypt_session_rows(rows, index=1, secret_indexes=()):
    if not rows:
        return rows
    return [_decrypt_session_row(row, index, secret_indexes) for row in rows]

def _execute_query_sync(query, args=(), fetchone=False, fetchall=False, commit=False):
    with connect() as db:
        cursor = db.cursor()
        cursor.execute(query, args)
        result = cursor.fetchone() if fetchone else (cursor.fetchall() if fetchall else None)
        if commit:
            db.commit()
        return result


async def execute_query(query, args=(), fetchone=False, fetchall=False, commit=False):
    """Run blocking psycopg work off the Telegram event loop with retry."""
    for attempt in range(3):
        try:
            return await to_thread(_execute_query_sync, query, args, fetchone, fetchall, commit)
        except DatabaseError:
            if commit or attempt == 2:
                logger.exception("Database operation failed after retries")
                raise
            await asyncio.sleep(0.1 * (2 ** attempt))

async def get_setting(key):
    result = await execute_query(
        "SELECT value FROM settings WHERE key=?",
        (key,),
        fetchone=True
    )
    return result[0] if result else None

async def set_setting(key, value):
    await execute_query(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, last_updated=CURRENT_TIMESTAMP
        """,
        (key, value),
        commit=True
    )

async def get_user(user_id):
    result = await execute_query(
        "SELECT user_id, username, first_name, last_name, coins, referral_points, referral_balance, join_date, last_active "
        "FROM users WHERE user_id=?",
        (user_id,),
        fetchone=True
    )
    if result:
        return {
            'user_id': result[0],
            'username': result[1],
            'first_name': result[2],
            'last_name': result[3],
            'coins': result[4],
            'referral_points': result[5],
            'referral_balance': result[6],
            'join_date': result[7],
            'last_active': result[8]
        }
    return None

async def create_user(user_id):
    await execute_query(
        "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
        (user_id,),
        commit=True
    )

async def touch_user_activity(user_id):
    await execute_query(
        "UPDATE users SET last_active=CURRENT_TIMESTAMP WHERE user_id=?",
        (user_id,), commit=True,
    )


async def record_referral_click(referred_id, referrer_id):
    """Record only the first valid inviter; this does not award any points."""
    if referred_id == referrer_id:
        return False
    if not await get_user(referrer_id):
        return False
    await execute_query(
        "INSERT INTO referrals (referred_id, referrer_id) VALUES (?, ?) ON CONFLICT(referred_id) DO NOTHING",
        (referred_id, referrer_id), commit=True,
    )
    return True


async def award_referral_after_subscription(referred_id):
    """Award an inviter exactly once after the referred account passed channel checks."""
    try:
        reward = _decimal_amount(await get_setting("referral_reward_amount") or 0)
        result = await to_thread(_award_referral_sync, referred_id, reward)
        if not result:
            return None
        referrer_id, balance_after = result
        await log_audit_event(referrer_id, f"referral_reward:{referred_id}:{reward}")
        return referrer_id, reward
    except (ValueError, DatabaseError):
        logger.exception("Failed to award referral")
        return None


def _award_referral_sync(referred_id, reward):
    with connect() as db:
            referral = db.execute(
                "SELECT referrer_id FROM referrals WHERE referred_id=? AND rewarded_at IS NULL FOR UPDATE",
                (referred_id,),
            ).fetchone()
            if not referral:
                return None
            referrer_id = referral[0]
            if db.execute(
                "UPDATE referrals SET rewarded_at=CURRENT_TIMESTAMP WHERE referred_id=? AND rewarded_at IS NULL",
                (referred_id,),
            ).rowcount != 1:
                return None
            if db.execute(
                "UPDATE users SET coins=coins+?, referral_balance=referral_balance+? WHERE user_id=?",
                (reward, reward, referrer_id),
            ).rowcount != 1:
                db.rollback()
                return None
            balance_after = db.execute("SELECT coins FROM users WHERE user_id=?", (referrer_id,)).fetchone()[0]
            db.execute(
                "INSERT INTO balance_ledger (user_id, amount, balance_after, operation, reference) VALUES (?, ?, ?, ?, ?)",
                (referrer_id, reward, balance_after, "referral_reward", str(referred_id)),
            )
            db.commit()
            return referrer_id, balance_after


async def get_referral_stats(user_id):
    row = await execute_query(
        "SELECT COUNT(*), COUNT(rewarded_at) FROM referrals WHERE referrer_id=?",
        (user_id,), fetchone=True,
    )
    return {"invited": row[0] if row else 0, "rewarded": row[1] if row else 0}


async def get_referral_leaderboard(limit=3):
    """Return only anonymized referral totals for the public leaderboard."""
    rows = await execute_query(
        """
        SELECT referrer_id, COUNT(*) AS referral_count
        FROM referrals
        GROUP BY referrer_id
        HAVING COUNT(*) > 0
        ORDER BY referral_count DESC, referrer_id ASC
        LIMIT ?
        """,
        (limit,), fetchall=True,
    )
    return [(row[0], row[1]) for row in (rows or [])]


def withdrawable_balance(user_data):
    """Referral rewards are spendable only when purchasing, never transferable."""
    if not user_data:
        return Decimal("0.00")
    return max(Decimal("0.00"), Decimal(str(user_data.get("coins") or 0)) - Decimal(str(user_data.get("referral_balance") or 0)))

def parse_db_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None

def format_db_datetime(value):
    if isinstance(value, str):
        return value
    return value.strftime("%Y-%m-%d %H:%M:%S")

def format_license_key(raw_key):
    cleaned = "".join(ch for ch in str(raw_key).upper() if ch.isalnum())
    if len(cleaned) == 20:
        return "-".join(cleaned[i:i + 5] for i in range(0, 20, 5))
    return str(raw_key).strip().upper()

async def generate_license_key():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        parts = ["".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4)]
        license_key = "-".join(parts)
        exists = await execute_query(
            "SELECT 1 FROM licenses WHERE license_key=?",
            (license_key,),
            fetchone=True
        )
        if not exists:
            return license_key

async def create_license(duration_days, created_by):
    license_key = await generate_license_key()
    await execute_query(
        """
        INSERT INTO licenses (license_key, duration_days, created_by)
        VALUES (?, ?, ?)
        """,
        (license_key, int(duration_days), created_by),
        commit=True
    )
    return license_key

async def activate_license(user_id, license_key):
    license_key = format_license_key(license_key)
    license_data = await execute_query(
        """
        SELECT license_key, duration_days, used_by, is_active, is_revoked
        FROM licenses WHERE license_key=?
        """,
        (license_key,),
        fetchone=True
    )
    if not license_data:
        return False, "المفتاح غير موجود."
    if int(license_data[4]) == 1:
        return False, "هذا المفتاح ملغي من قبل المطور."
    if license_data[2]:
        return False, "هذا المفتاح مستخدم من قبل."
    if int(license_data[3]) != 1:
        return False, "هذا المفتاح غير نشط."

    now = datetime.datetime.now()
    expires_at = now + datetime.timedelta(days=int(license_data[1]))
    try:
        claimed = await to_thread(_activate_license_sync, user_id, license_key, now, expires_at)
    except DatabaseError:
        logger.exception("Failed to activate license atomically")
        return False, "تعذر تفعيل المفتاح، حاول لاحقاً."
    if not claimed:
        return False, "تم استخدام المفتاح بالفعل"
    return True, expires_at


def _activate_license_sync(user_id, license_key, now, expires_at):
    with connect() as db:
        cursor = db.execute(
            """UPDATE licenses SET used_by=?,used_at=?,expires_at=?,is_active=0
               WHERE license_key=? AND used_by IS NULL AND is_active=1 AND is_revoked=0""",
            (user_id, format_db_datetime(now), format_db_datetime(expires_at), license_key),
        )
        if cursor.rowcount != 1:
            db.rollback()
            return False
        db.execute(
            """INSERT INTO user_licenses(user_id,license_key,activated_at,expires_at,is_valid,
                reminded_3d,reminded_1d,expired_notified) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET license_key=excluded.license_key,
                activated_at=excluded.activated_at,expires_at=excluded.expires_at,is_valid=1,
                reminded_3d=0,reminded_1d=0,expired_notified=0""",
            (user_id, license_key, format_db_datetime(now), format_db_datetime(expires_at), True, False, False, False),
        )
        db.commit()
        return True

async def get_user_license(user_id):
    return await execute_query(
        """
        SELECT ul.user_id, ul.license_key, ul.activated_at, ul.expires_at, ul.is_valid,
               l.duration_days, l.is_revoked
        FROM user_licenses ul
        LEFT JOIN licenses l ON l.license_key=ul.license_key
        WHERE ul.user_id=?
        """,
        (user_id,),
        fetchone=True
    )

async def get_license_status(user_id):
    license_data = await get_user_license(user_id)
    if not license_data:
        return {"allowed": False, "reason": "missing", "license": None}
    expires_at = parse_db_datetime(license_data[3])
    if int(license_data[4]) != 1 or int(license_data[6] or 0) == 1:
        return {"allowed": False, "reason": "invalid", "license": license_data, "expires_at": expires_at}
    if not expires_at or expires_at <= datetime.datetime.now():
        await execute_query(
            "UPDATE user_licenses SET is_valid=0 WHERE user_id=?",
            (user_id,),
            commit=True
        )
        return {"allowed": False, "reason": "expired", "license": license_data, "expires_at": expires_at}
    return {"allowed": True, "reason": "active", "license": license_data, "expires_at": expires_at}

async def list_licenses(limit=50):
    return await execute_query(
        """
        SELECT license_key, duration_days, created_by, created_at, used_by, used_at,
               expires_at, is_active, is_revoked
        FROM licenses
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
        fetchall=True
    )

async def revoke_license(license_key):
    license_key = format_license_key(license_key)
    license_data = await execute_query(
        "SELECT used_by FROM licenses WHERE license_key=?",
        (license_key,),
        fetchone=True
    )
    if not license_data:
        return False
    await execute_query(
        "UPDATE licenses SET is_revoked=1, is_active=0 WHERE license_key=?",
        (license_key,),
        commit=True
    )
    if license_data[0]:
        await execute_query(
            "UPDATE user_licenses SET is_valid=0 WHERE user_id=? AND license_key=?",
            (license_data[0], license_key),
            commit=True
        )
    return True

async def delete_expired_licenses():
    now = format_db_datetime(datetime.datetime.now())
    deleted = await execute_query(
        """
        SELECT COUNT(*) FROM licenses
        WHERE expires_at IS NOT NULL AND expires_at < ?
        """,
        (now,),
        fetchone=True
    )
    await execute_query(
        """
        DELETE FROM user_licenses
        WHERE license_key IN (
            SELECT license_key FROM licenses
            WHERE expires_at IS NOT NULL AND expires_at < ?
        )
        """,
        (now,),
        commit=True
    )
    await execute_query(
        """
        DELETE FROM licenses
        WHERE expires_at IS NOT NULL AND expires_at < ?
        """,
        (now,),
        commit=True
    )
    return deleted[0] if deleted else 0

async def get_license_stats():
    now = format_db_datetime(datetime.datetime.now())
    active_users = await execute_query(
        "SELECT COUNT(*) FROM user_licenses WHERE is_valid=1 AND expires_at>?",
        (now,),
        fetchone=True
    )
    used_keys = await execute_query("SELECT COUNT(*) FROM licenses WHERE used_by IS NOT NULL", fetchone=True)
    unused_keys = await execute_query("SELECT COUNT(*) FROM licenses WHERE used_by IS NULL AND is_revoked=0", fetchone=True)
    revoked_keys = await execute_query("SELECT COUNT(*) FROM licenses WHERE is_revoked=1", fetchone=True)
    expired_keys = await execute_query(
        "SELECT COUNT(*) FROM licenses WHERE expires_at IS NOT NULL AND expires_at<=?",
        (now,),
        fetchone=True
    )
    return {
        "active_users": active_users[0] if active_users else 0,
        "used_keys": used_keys[0] if used_keys else 0,
        "unused_keys": unused_keys[0] if unused_keys else 0,
        "revoked_keys": revoked_keys[0] if revoked_keys else 0,
        "expired_keys": expired_keys[0] if expired_keys else 0,
    }

async def get_license_notification_targets():
    return await execute_query(
        """
        SELECT user_id, license_key, expires_at, reminded_3d, reminded_1d, expired_notified
        FROM user_licenses
        WHERE is_valid=1 OR expired_notified=0
        """,
        (),
        fetchall=True
    )

async def mark_license_reminder(user_id, field):
    if field not in {"reminded_3d", "reminded_1d", "expired_notified"}:
        return
    await execute_query(
        f"UPDATE user_licenses SET {field}=1 WHERE user_id=?",
        (user_id,),
        commit=True
    )

DEFAULT_REACTIONS = ['❤️', '🔥', '👍', '🎉', '🤩']

async def extract_entity_from_url(url):
    """استخراج معرف القناة ورقم الرسالة من الرابط"""
    try:
        parts = url.split('/')
        if len(parts) >= 5:
            if parts[3] == 'c':
                # روابط القنوات الخاصة
                channel_id = int(parts[4])
                if channel_id < 0:
                    channel_id = -1000000000000 - channel_id
                else:
                    channel_id = int(f"-100{channel_id}")
                msg_id = int(parts[5])
                return channel_id, msg_id
            else:
                # روابط القنوات العامة
                channel_username = parts[3]
                msg_id = int(parts[4])
                return channel_username, msg_id
        return None, None
    except Exception as e:
        logger.error(f"Error extracting entity: {str(e)}")
        return None, None

async def resolve_message_target_from_url(client, url):
    entity, msg_id = await extract_entity_from_url(url)
    if not entity or not msg_id:
        return None, None

    if isinstance(entity, str):
        entity = await resolve_posting_target(client, entity)
    return entity, msg_id

async def get_purchased_account_info(phone):
    row = await execute_query(
        """
        SELECT phone, session, user_id, seller_id, calling_code, name, price, purchased_at, twofa
        FROM purchased_accounts WHERE phone=?
        """,
        (phone,),
        fetchone=True
    )
    return _decrypt_session_row(row, secret_indexes=(8,))

async def get_stored_account_info(phone):
    row = await execute_query(
        "SELECT phone, session, user_id, twofa, storage_date FROM stored_accounts WHERE phone=?",
        (phone,),
        fetchone=True
    )
    return _decrypt_session_row(row, secret_indexes=(3,))

async def react_to_message(session_str, url, emoji):
    """التفاعل مع منشور بإيموجي محدد"""
    if not await session_is_usable(session_str):
        return False
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        entity, msg_id = await resolve_message_target_from_url(client, url)
        if not entity or not msg_id:
            return False
        
        await client(functions.messages.SendReactionRequest(
            peer=entity,
            msg_id=msg_id,
            reaction=[types.ReactionEmoji(emoticon=emoji)],
            big=True
        ))
        return True
    except Exception as e:
        logger.error(f"Error reacting: {str(e)}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def vote_in_poll(session_str, url, option_number):
    """التصويت في استفتاء"""
    if not await session_is_usable(session_str):
        return False
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        entity, msg_id = await resolve_message_target_from_url(client, url)
        if not entity or not msg_id:
            return False
        
        await client(functions.messages.SendVoteRequest(
            peer=entity,
            msg_id=msg_id,
            options=[str(option_number - 1)]  # الخيارات تبدأ من 0
        ))
        return True
    except Exception as e:
        logger.error(f"Error voting: {str(e)}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def increment_views_count(session_str, url):
    """زيادة عدد مشاهدات المنشور"""
    if not await session_is_usable(session_str):
        return False
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        entity, msg_id = await resolve_message_target_from_url(client, url)
        if not entity or not msg_id:
            return False
        
        await client(GetMessagesViewsRequest(
            peer=entity,
            id=[msg_id],
            increment=True
        ))
        return True
    except Exception as e:
        logger.error(f"Error incrementing views: {str(e)}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def mass_react(user_id, url, emoji, count, accounts=None):
    """التفاعل الجماعي باستخدام عدة حسابات"""
    accounts = accounts if accounts is not None else await get_stored_accounts(user_id)
    if not accounts or not 1 <= count <= len(accounts):
        return 0
    
    success = 0
    for account in accounts[:count]:
        if not await service_account_authorized(user_id, account):
            continue
        try:
            if await react_to_message(account[1], url, emoji):
                success += 1
            await asyncio.sleep(1)  # تأخير بين كل عملية
        except Exception as e:
            logger.error(f"Error in mass react: {str(e)}")
    return success

async def mass_vote(user_id, url, option, count, accounts=None):
    """التصويت الجماعي باستخدام عدة حسابات"""
    accounts = accounts if accounts is not None else await get_stored_accounts(user_id)
    if not accounts or not 1 <= count <= len(accounts):
        return 0
    
    success = 0
    for account in accounts[:count]:
        if not await service_account_authorized(user_id, account):
            continue
        try:
            if await vote_in_poll(account[1], url, option):
                success += 1
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error in mass vote: {str(e)}")
    return success

async def increment_views(user_id, url, count, accounts=None):
    """زيادة المشاهدات باستخدام عدة حسابات"""
    accounts = accounts if accounts is not None else await get_stored_accounts(user_id)
    if not accounts or not 1 <= count <= len(accounts):
        return 0
    
    success = 0
    for account in accounts[:count]:
        if not await service_account_authorized(user_id, account):
            continue
        try:
            if await increment_views_count(account[1], url):
                success += 1
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error in increment views: {str(e)}")
    return success


async def session_requires_twofa(session_str):
    """Return whether a valid session has an existing 2FA password, safely."""
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return None
        password_info = await client(GetPasswordRequest())
        return bool(password_info.has_password)
    except Exception:
        logger.exception("Could not read 2FA state for submitted session")
        return None
    finally:
        if client and client.is_connected():
            await client.disconnect()


def generate_account_twofa_password() -> str:
    """Return the configured fixed 2FA password for managed accounts."""
    return "DevShaHm"


async def delete_sensitive_message(message) -> None:
    """Best-effort deletion of a password-bearing Telegram message."""
    with contextlib.suppress(Exception):
        await message.delete()


async def set_managed_twofa(session_str, new_password, current_password=None, *, expected_phone=None):
    """Set the configured 2FA password after authorization by the holder.

    ``current_password`` (or the known recovery candidates) is used only in
    memory to build Telegram's SRP proof and is never returned or logged.
    """
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH,
                                exclusive_session=True, flood_sleep_threshold=0,
                                request_retries=0, connection_retries=1)
        await asyncio.wait_for(client.connect(), 20)
        if not await asyncio.wait_for(client.is_user_authorized(), 20):
            return False, "الجلسة غير مخوّلة."
        if expected_phone is not None:
            me = await asyncio.wait_for(client.get_me(), 20)
            if not me or getattr(me, 'bot', False) or str(getattr(me, 'phone', '') or '').lstrip('+') != str(expected_phone).lstrip('+'):
                return False, "رقم الجلسة لا يطابق الرقم المسجل."
        password_info = await asyncio.wait_for(client(GetPasswordRequest()), 20)
        if password_info.has_password:
            # Recover a crash after Telegram accepted the new password but
            # before the database transaction committed.
            try:
                await asyncio.wait_for(client(functions.account.GetPasswordSettingsRequest(
                    password=telethon_password.compute_check(password_info, new_password),
                )), 20)
                return True, None
            except errors.PasswordHashInvalidError:
                password_info = await asyncio.wait_for(client(GetPasswordRequest()), 20)
            if not current_password:
                return False, "كلمة التحقق بخطوتين الحالية مطلوبة."
        # An old pending rotation may have reached Telegram before a crash.
        # Keep both its original and proposed password as recovery candidates.
        candidates = current_password if isinstance(current_password, (list, tuple)) else [current_password]
        candidates = list(dict.fromkeys(value for value in candidates if value and value != new_password))
        if password_info.has_password and not candidates:
            return False, "كلمة التحقق بخطوتين الحالية مطلوبة."
        candidates = candidates or [None]
        if not new_password or len(new_password) < 5:
            return False, "كلمة التحقق يجب أن تحتوي على خمسة أحرف على الأقل."
        # Telegram requires 32 fresh random bytes appended to the new salt
        # before hashing. Send the same extended algorithm with the hash.
        for index, candidate in enumerate(candidates):
            check = telethon_password.compute_check(password_info, candidate) if password_info.has_password else types.InputCheckPasswordEmpty()
            password_info.new_algo.salt1 += secrets.token_bytes(32)
            new_hash = telethon_password.compute_digest(password_info.new_algo, new_password)
            try:
                await asyncio.wait_for(client(UpdatePasswordSettingsRequest(
                    password=check,
                    new_settings=PasswordInputSettings(
                        new_algo=password_info.new_algo, new_password_hash=new_hash, hint="",
                    ),
                )), 20)
                return True, None
            except errors.PasswordHashInvalidError:
                if index + 1 == len(candidates):
                    raise
                password_info = await asyncio.wait_for(client(GetPasswordRequest()), 20)
    except (FloodWaitError, errors.PasswordTooFreshError, errors.SessionTooFreshError) as exc:
        return False, f"Telegram فرض فترة انتظار لتغيير كلمة المرور. retry_after={max(60, exc.seconds + 1)}"
    except errors.PasswordHashInvalidError:
        return False, "كلمة التحقق بخطوتين الحالية غير صحيحة."
    except SessionBusyError:
        return False, "الحساب مشغول بخدمة أخرى. retry_after=60"
    except Exception as exc:
        logger.info("Managed 2FA change deferred (%s)", type(exc).__name__)
        return False, "تعذر الاتصال لإتمام تغيير كلمة التحقق بخطوتين. retry_after=300"
    finally:
        if client:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 15)


async def schedule_managed_twofa_change(phone, session_str, current_password=None, owner_id=None):
    """Persist an encrypted request ready to execute after successful login."""
    await execute_query(
        """INSERT INTO scheduled_twofa_changes (phone, session, current_password, new_password, due_at, owner_id)
           VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
           ON CONFLICT(phone) DO NOTHING""",
        (phone, encrypt_session(session_str), encrypt_session(current_password), encrypt_session(generate_account_twofa_password()), owner_id),
        commit=True,
    )


def _process_twofa_rotation_sync(phone):
    from platform_services import record_twofa_outcome
    # Keep the request locked until all local representations commit. After
    # a crash the proposed password is verified before attempting a change.
    with connect() as db:
        row = db.execute(
            "SELECT session, current_password, legacy_password, new_password FROM scheduled_twofa_changes "
            "WHERE phone=? AND due_at<=CURRENT_TIMESTAMP AND attempts<3 FOR UPDATE SKIP LOCKED",
            (phone,),
        ).fetchone()
        if not row:
            return None
        owner = db.execute('SELECT owner_id FROM scheduled_twofa_changes WHERE phone=?', (phone,)).fetchone()
        current_owner = db.execute('SELECT user_id FROM stored_accounts WHERE phone=?', (phone,)).fetchone()
        if owner and owner[0] and current_owner and owner[0] != current_owner[0]:
            db.execute("UPDATE scheduled_twofa_changes SET attempts=3,last_error='تغيرت ملكية الحساب؛ أُوقف التغيير' WHERE phone=?", (phone,))
            db.commit()
            return False
        session, current, legacy, proposed = (decrypt_session(value) for value in row)
        if not session:
            db.execute("UPDATE scheduled_twofa_changes SET attempts=3,last_error='تعذر فك تشفير بيانات المهمة' WHERE phone=?", (phone,))
            record_twofa_outcome(db, phone, 'failed', 'تعذر فك تشفير بيانات المهمة.')
            db.commit()
            return False
        if not proposed:
            proposed = generate_account_twofa_password()
            db.execute("UPDATE scheduled_twofa_changes SET new_password=?,due_at=CURRENT_TIMESTAMP+INTERVAL '5 minutes' WHERE phone=?",
                       (encrypt_session(proposed), phone))
            db.commit()
        candidates = [value for value in (current, legacy, proposed) if value and value not in ('لا يوجد', 'no password')]
        changed, reason = asyncio.run(set_managed_twofa(session, proposed, candidates, expected_phone=phone))
        if not changed:
            from managed_twofa import retry_delay
            delay = retry_delay(reason)
            temporary = 'retry_after=' in (reason or '') or 'مشغول' in (reason or '')
            attempts = db.execute(
                "UPDATE scheduled_twofa_changes SET attempts=attempts+?,last_error=?, due_at=CURRENT_TIMESTAMP + ? * INTERVAL '1 second' WHERE phone=? RETURNING attempts",
                (0 if temporary else 1, reason, delay, phone),
            ).fetchone()[0]
            record_twofa_outcome(db, phone, 'failed' if attempts >= 3 else 'retrying', reason)
            db.commit()
            # Retain failed requests for recovery rather than silently
            # destroying a password Telegram may already have accepted.
            return False
        encrypted = encrypt_session(proposed)
        for table in ("accounts", "pending_sales", "pending_purchases", "stored_accounts", "purchased_accounts", "auctions"):
            db.execute(f"UPDATE {table} SET twofa=? WHERE phone=?", (encrypted, phone))
        db.execute("""INSERT INTO managed_twofa_credentials(phone,password) VALUES(?,?)
            ON CONFLICT(phone) DO UPDATE SET password=excluded.password,updated_at=CURRENT_TIMESTAMP""",
            (phone, encrypted))
        record_twofa_outcome(db, phone, 'completed')
        from managed_twofa import save_state
        save_state(db, phone, session, 'completed')
        db.execute("DELETE FROM scheduled_twofa_changes WHERE phone=?", (phone,))
        db.commit()
        return True


async def process_due_twofa_changes():
    rows = await execute_query(
        "SELECT phone FROM scheduled_twofa_changes WHERE due_at<=CURRENT_TIMESTAMP AND attempts<3 ORDER BY due_at LIMIT 25",
        fetchall=True,
    ) or []
    completed = failed = 0
    for (phone,) in rows:
        try:
            result = await to_thread(_process_twofa_rotation_sync, phone)
        except Exception:
            logger.exception("Could not commit scheduled 2FA rotation")
            result = False
        completed += result is True
        failed += result is False
    return completed, failed


def extract_story_target_from_url(url):
    """Parse a public Telegram story URL without accepting arbitrary peers.

    Telegram commonly shares stories as ``t.me/name/s/123``; clients may also
    provide ``t.me/name/123``.  The caller deliberately labels this action as
    a story operation, so the latter is accepted for client compatibility.
    """
    value = (url or "").strip()
    if not value.startswith(("https://", "http://")):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        return None, None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 3 and parts[1] == "s":
        username, story_id = parts[0], parts[2]
    elif len(parts) == 2:
        username, story_id = parts
    else:
        return None, None
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None, None
    try:
        story_id = int(story_id)
    except ValueError:
        return None, None
    return (username, story_id) if story_id > 0 else (None, None)


async def view_and_react_to_story(session_str, story_url, emoji):
    """View and react to one story from a single authorized account.

    The session is never included in errors or logs. A FloodWait is respected
    by failing this account rather than retrying aggressively.
    """
    if not await session_is_usable(session_str):
        return False, False
    username, story_id = extract_story_target_from_url(story_url)
    if not username or not emoji or len(emoji.strip()) > 32:
        return False, False
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return False, False
        peer = await client.get_input_entity(username)
        await client(functions.stories.IncrementStoryViewsRequest(peer=peer, id=[story_id]))
        await client(functions.stories.SendReactionRequest(
            peer=peer,
            story_id=story_id,
            reaction=types.ReactionEmoji(emoticon=emoji.strip()),
            add_to_recent=False,
        ))
        return True, True
    except FloodWaitError as exc:
        logger.warning("Story action deferred by Telegram FloodWait (%ss)", exc.seconds)
        return False, False
    except Exception:
        logger.exception("Story view/reaction failed")
        return False, False
    finally:
        if client and client.is_connected():
            await client.disconnect()


async def mass_story_view_and_react(accounts, story_url, emoji, count, *, user_id=None):
    """Run a bounded, sequential story campaign and return view/react totals."""
    if count < 1 or count > len(accounts):
        return 0, 0
    views = reactions = 0
    for account in accounts[:count]:
        if user_id is not None and not await service_account_authorized(user_id, account):
            continue
        viewed, reacted = await view_and_react_to_story(account[1], story_url, emoji)
        views += int(viewed)
        reactions += int(reacted)
        await asyncio.sleep(1)
    return views, reactions

async def update_user_coins(user_id, coins):
    await to_thread(_set_user_balance_sync, user_id, coins)


def _set_user_balance_sync(user_id, coins):
    balance = Decimal(str(coins)).quantize(Decimal('0.01'))
    if not balance.is_finite() or balance < 0:
        raise ValueError('invalid balance')
    with connect() as db:
        previous = db.execute('SELECT coins FROM users WHERE user_id=? FOR UPDATE', (user_id,)).fetchone()
        if previous is None or previous[0] == balance:
            return
        db.execute('UPDATE users SET coins=? WHERE user_id=?', (balance, user_id))
        db.execute('''INSERT INTO balance_ledger(user_id, amount, balance_after, operation)
                      VALUES (?, ?, ?, ?)''',
                   (user_id, balance - previous[0], balance, 'balance_set'))
        db.commit()


def _decimal_amount(value):
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be positive")
    return amount


def _decimal_delta(value):
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    return amount


def _adjust_balance_sync(user_id, amount, operation, reference, withdrawable_only=False):
    amount = _decimal_delta(amount)
    with connect() as db:
        if amount < 0 and withdrawable_only:
            update = db.execute(
                "UPDATE users SET coins=coins+? WHERE user_id=? AND coins-referral_balance+?>=0 RETURNING coins",
                (amount, user_id, amount),
            )
        else:
            update = db.execute(
                "UPDATE users SET coins=coins+? WHERE user_id=? AND coins+?>=0 RETURNING coins",
                (amount, user_id, amount),
            )
        row = update.fetchone()
        if not row:
            db.rollback()
            return False, None
        db.execute(
            "INSERT INTO balance_ledger (user_id, amount, balance_after, operation, reference) VALUES (?, ?, ?, ?, ?)",
            (user_id, amount, row[0], operation, str(reference or "")[:200]),
        )
        db.commit()
        return True, row[0]


async def adjust_balance_atomically(user_id, amount, operation, reference=None, *, withdrawable_only=False):
    """Apply a credit/debit and ledger entry in one transaction."""
    try:
        return await to_thread(
            _adjust_balance_sync, user_id, amount, operation, reference, withdrawable_only
        )
    except (ValueError, DatabaseError):
        logger.exception("Atomic balance adjustment failed")
        return False, None


def _create_withdrawal_sync(user_id, amount, wallet):
    amount = _decimal_amount(amount)
    with connect() as db:
        row = db.execute(
            "UPDATE users SET coins=coins-? WHERE user_id=? AND coins-referral_balance>=? RETURNING coins",
            (amount, user_id, amount),
        ).fetchone()
        if not row:
            db.rollback()
            return None
        request = db.execute(
            "INSERT INTO withdrawal_requests (user_id, amount, wallet) VALUES (?, ?, ?) RETURNING request_id",
            (user_id, amount, wallet[:500]),
        ).fetchone()
        db.execute(
            "INSERT INTO balance_ledger (user_id, amount, balance_after, operation, reference) VALUES (?, ?, ?, ?, ?)",
            (user_id, -amount, row[0], "withdrawal_requested", str(request[0])),
        )
        db.commit()
        return request[0]


async def create_withdrawal_atomically(user_id, amount, wallet):
    try:
        return await to_thread(_create_withdrawal_sync, user_id, amount, wallet)
    except (ValueError, DatabaseError):
        logger.exception("Atomic withdrawal creation failed")
        return None


def _mark_withdrawal_paid_sync(request_id, admin_id):
    with connect() as db:
        row = db.execute(
            "UPDATE withdrawal_requests SET status='paid', processed_by=?, processed_at=CURRENT_TIMESTAMP "
            "WHERE request_id=? AND status='pending' RETURNING user_id",
            (admin_id, request_id),
        ).fetchone()
        if not row:
            db.rollback()
            return None
        db.commit()
        return row[0]


async def mark_withdrawal_paid_atomically(request_id, admin_id):
    try:
        return await to_thread(_mark_withdrawal_paid_sync, request_id, admin_id)
    except DatabaseError:
        logger.exception("Could not mark withdrawal as paid")
        return None


async def transfer_coins_atomically(sender_id, recipient_id, amount, *, fee=Decimal('0')):
    """Debit and credit together so a crash cannot create a partial transfer."""
    try:
        amount = _decimal_amount(amount)
        fee = _decimal_delta(fee)
        if fee < 0:
            return False
        if sender_id == recipient_id:
            return False
        return await to_thread(_transfer_coins_sync, sender_id, recipient_id, amount, fee)
    except (ValueError, DatabaseError):
        logger.exception("Failed to transfer coins atomically")
        return False


def _transfer_coins_sync(sender_id, recipient_id, amount, fee=Decimal('0')):
    total = amount + fee
    with connect() as db:
            if db.execute(
                "UPDATE users SET coins=coins-? WHERE user_id=? AND coins-referral_balance>=?",
                (total, sender_id, total),
            ).rowcount != 1:
                return False
            if db.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (amount, recipient_id)).rowcount != 1:
                db.rollback()
                return False
            sender_balance = db.execute("SELECT coins FROM users WHERE user_id=?", (sender_id,)).fetchone()[0]
            recipient_balance = db.execute("SELECT coins FROM users WHERE user_id=?", (recipient_id,)).fetchone()[0]
            db.execute("INSERT INTO balance_ledger (user_id, amount, balance_after, operation, reference, fee) VALUES (?, ?, ?, ?, ?, ?)",
                       (sender_id, -total, sender_balance, "transfer_sent", str(recipient_id), fee))
            db.execute("INSERT INTO balance_ledger (user_id, amount, balance_after, operation, reference) VALUES (?, ?, ?, ?, ?)",
                       (recipient_id, amount, recipient_balance, "transfer_received", str(sender_id)))
            db.commit()
            return True


async def log_audit_event(user_id, action):
    """Best-effort audit trail for privileged access and security events."""
    await execute_query(
        "INSERT INTO audit_log (user_id, action) VALUES (?, ?)",
        (user_id, str(action)[:200]),
        commit=True,
    )


def minimum_auction_bid(current_bid):
    """أقل عرض مقبول، مقرب للأعلى إلى منزلتين عشريتين كما يظهر للمستخدم."""
    current = Decimal(str(current_bid or 0))
    increment = max(Decimal("0.10"), current * Decimal("0.05"))
    return (current + increment).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


async def get_auction_platform_fee(amount):
    """يعيد عمولة المنصة وصافي البائع حسب النسبة المهيأة في الإعدادات."""
    try:
        percent = Decimal(str(await get_setting("auction_platform_fee_percent") or "0"))
    except Exception:
        percent = Decimal("0")
    percent = min(max(percent, Decimal("0")), Decimal("100"))
    total = Decimal(str(amount))
    fee = (total * percent / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return fee, total - fee, percent


async def place_auction_bid_atomically(auction_id, user_id, bid_amount):
    """يسجل العرض بعد قفل المزاد، كي لا تتعارض العروض المتزامنة."""
    try:
        return await to_thread(_place_auction_bid_sync, auction_id, user_id, _decimal_amount(bid_amount))
    except (ValueError, DatabaseError):
        logger.exception("Failed to place auction bid")
        return False, "database_error", None


def _place_auction_bid_sync(auction_id, user_id, bid_amount):
    with connect() as db:
        from typed_auctions import verify
        verify(db,auction_id)
        auction = db.execute(
            "SELECT current_bid, status, seller_id FROM auctions WHERE auction_id=? FOR UPDATE",
            (auction_id,),
        ).fetchone()
        if not auction or auction[1] != "active":
            db.rollback()
            return False, None, None
        if int(auction[2]) == int(user_id):
            db.rollback()
            return False, "self_bid", None
        current_bid = Decimal(str(auction[0]))
        required_bid = minimum_auction_bid(current_bid)
        if bid_amount < required_bid:
            db.rollback()
            return False, current_bid, required_bid
        db.execute(
            "UPDATE auctions SET current_bid=?, current_bidder=? WHERE auction_id=?",
            (bid_amount, user_id, auction_id),
        )
        db.execute(
            "INSERT INTO auction_bids (auction_id, user_id, bid_amount) VALUES (?, ?, ?)",
            (auction_id, user_id, bid_amount),
        )
        db.commit()
        return True, current_bid, required_bid


async def settle_purchase_atomically(user_id, purchase, *, store_account=False, expected_quote=None):
    """Move an inventory account and debit its buyer in one PostgreSQL transaction."""
    from commerce import CouponUnavailable
    phone, calling_code, name, price, session, twofa = (
        purchase[2], purchase[3], purchase[4], purchase[5], purchase[6], purchase[7]
    )
    try:
        return await to_thread(
            _settle_purchase_sync, user_id, phone, calling_code, name, price, session, twofa, store_account, expected_quote
        )
    except CouponUnavailable:
        return False, 'coupon_invalid'
    except ValueError:
        return False, 'quote_invalid'
    except DatabaseError:
        logger.exception("Failed to settle purchase")
        return False, "database_error"


def _settle_purchase_sync(user_id, phone, calling_code, name, price, session, twofa, store_account, expected_quote=None):
    price = _decimal_amount(price)
    with connect() as db:
        from commerce import consume_quote, award_spend
        db.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE', (user_id,))
        pending = db.execute('SELECT quote_id FROM pending_purchases WHERE user_id=? AND phone=? FOR UPDATE',
                             (user_id, phone)).fetchone()
        if expected_quote and (not pending or pending[0] != expected_quote):
            raise ValueError('تغير عرض الشراء؛ افتح العرض الحالي.')
        inventory_session = db.execute('SELECT session FROM accounts WHERE phone=? FOR UPDATE', (phone,)).fetchone()
        if not inventory_session or decrypt_session(inventory_session[0]) != session:
            db.rollback()
            return False, 'inventory_changed'
        if db.execute("SELECT 1 FROM rental_leases WHERE phone=? AND status='active' AND ends_at>CURRENT_TIMESTAMP", (phone,)).fetchone():
            return False, 'rental_active'
        if pending and pending[0]:
            price = consume_quote(db, user_id, pending[0], 'purchase', phone)
        else:
            raise ValueError('اطلب عرض سعر جديدًا.')
        deferred_hold = db.execute("""SELECT amount,funds_reserved FROM deferred_purchases
            WHERE user_id=? AND phone=? AND quote_id=? AND status='pending' FOR UPDATE""",
            (user_id, phone, pending[0])).fetchone()
        funds_reserved = bool(deferred_hold and deferred_hold[1])
        if funds_reserved and Decimal(str(deferred_hold[0])) != Decimal(str(price)):
            raise ValueError('تغير سعر الحجز؛ أُلغي التسليم وأُعيد المبلغ.')
        reservation = db.execute(
            "DELETE FROM account_reservations WHERE phone=? AND user_id=? AND star_order_id IS NULL AND reserved_until>CURRENT_TIMESTAMP RETURNING phone",
            (phone, user_id),
        ).fetchone()
        if not reservation:
            db.rollback()
            return False, "reservation_expired"
        if not funds_reserved and db.execute(
            "UPDATE users SET coins=coins-?, referral_balance=GREATEST(0, referral_balance-?) "
            "WHERE user_id=? AND coins>=?",
            (price, price, user_id, price),
        ).rowcount != 1:
            db.rollback()
            return False, "insufficient_funds"
        inventory = db.execute('''DELETE FROM accounts WHERE phone=? AND is_active AND NOT is_sold
            AND NOT EXISTS(SELECT 1 FROM account_health h WHERE h.phone=accounts.phone
                AND h.inventory_session=accounts.session AND h.status IN
                ('revoked','banned','deleted','restricted','invalid','mismatch'))
            RETURNING acquisition_cost''', (phone,)).fetchone()
        if inventory is None:
            db.rollback()
            return False, "unavailable"
        encrypted_session = encrypt_session(session)
        encrypted_twofa = encrypt_session(twofa)
        db.execute(
            """INSERT INTO purchased_accounts (phone, session, user_id, seller_id, calling_code, name, price, twofa)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET session=excluded.session, user_id=excluded.user_id,
               seller_id=excluded.seller_id, calling_code=excluded.calling_code, name=excluded.name,
               price=excluded.price, twofa=excluded.twofa""",
            (phone, encrypted_session, user_id, None, calling_code, name, price, encrypted_twofa),
        )
        if store_account:
            db.execute(
                """INSERT INTO stored_accounts (phone, session, user_id, twofa) VALUES (?, ?, ?, ?)
                   ON CONFLICT(phone) DO UPDATE SET session=excluded.session, user_id=excluded.user_id,
                   twofa=excluded.twofa""",
                (phone, encrypted_session, user_id, encrypted_twofa),
            )
        balance = db.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
        if not funds_reserved:
            db.execute(
                """INSERT INTO balance_ledger (user_id,amount,balance_after,operation,reference,number_snapshot)
                SELECT ?,?,?,?,?,number_snapshot FROM commerce_quotes WHERE quote_id=?""",
                (user_id,-price,balance,"number_purchase",phone,pending[0]),
            )
        db.execute("DELETE FROM pending_purchases WHERE user_id=? AND phone=?", (user_id, phone))
        db.execute('''INSERT INTO commerce_sales(user_id,phone,revenue,cost,number_snapshot)
            SELECT ?,?,?,?,number_snapshot FROM commerce_quotes WHERE quote_id=?''',
                   (user_id, phone, price, inventory[0], pending[0]))
        deferred = db.execute("UPDATE deferred_purchases SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE user_id=? AND phone=? AND status='pending' RETURNING quote_id", (user_id, phone)).fetchone()
        if deferred:
            from workflow import enqueue
            enqueue(db, user_id, f'deferred:delivered:{deferred[0]}',
                    f'✅ انتهت جميع الإيجارات. أُضيف الرقم +{phone} إلى حساباتك وخُصم {price}$ مرة واحدة.')
        db.commit()
        return True, None


async def settle_auction_atomically(auction_id, buyer_id, auction):
    """Deliver the auction account and settle funds in one database transaction."""
    try:
        return await to_thread(_settle_auction_sync, auction_id, buyer_id)
    except (ValueError, DatabaseError):
        logger.exception("Failed to settle auction")
        return False, None, None


async def reserve_username_auction_atomically(auction_id, seller_id):
    """Move the winning cash balance into escrow before the seller transfers the username."""
    try:
        return await to_thread(_reserve_username_auction_sync, auction_id, seller_id)
    except (ValueError, DatabaseError):
        logger.exception("Failed to reserve username auction funds")
        return False, "database_error", None, None, None


def _reserve_username_auction_sync(auction_id, seller_id):
    with connect() as db:
        auction = db.execute(
            """SELECT seller_id,current_bid,current_bidder FROM username_auctions
               WHERE auction_id=? AND status='active' FOR UPDATE""", (auction_id,),
        ).fetchone()
        if not auction or int(auction[0]) != int(seller_id) or not auction[2]:
            db.rollback()
            return False, "unavailable", None, None, None
        owner, amount, buyer_id = auction
        if int(owner) == int(buyer_id):
            db.rollback()
            return False, "self_bid", buyer_id, None, None
        amount = _decimal_amount(amount)
        setting = db.execute("SELECT value FROM settings WHERE key='auction_platform_fee_percent'").fetchone()
        try:
            percent = Decimal(str(setting[0] if setting else "0"))
            if not percent.is_finite():
                percent = Decimal("0")
        except Exception:
            percent = Decimal("0")
        percent = min(max(percent, Decimal("0")), Decimal("100"))
        fee = (amount * percent / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        seller_amount = amount - fee
        balance = db.execute(
            "UPDATE users SET coins=coins-? WHERE user_id=? AND coins-referral_balance>=? RETURNING coins",
            (amount, buyer_id, amount),
        ).fetchone()
        if not balance:
            db.rollback()
            return False, "insufficient_funds", buyer_id, None, None
        if db.execute(
            "UPDATE username_auctions SET status='awaiting_transfer' WHERE auction_id=? AND status='active' AND current_bidder=?",
            (auction_id, buyer_id),
        ).rowcount != 1:
            db.rollback()
            return False, "unavailable", buyer_id, None, None
        db.execute("""INSERT INTO username_auction_escrows
            (auction_id,buyer_id,amount,fee,seller_amount,expires_at)
            VALUES(?,?,?,?,?,CURRENT_TIMESTAMP+INTERVAL '24 hours')""",
            (auction_id, buyer_id, amount, fee, seller_amount))
        db.execute("""INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,fee)
            VALUES(?,?,?,?,?,0)""",
            (buyer_id, -amount, balance[0], "username_auction_escrow", str(auction_id)))
        db.commit()
        return True, None, buyer_id, amount, fee


async def settle_username_auction_atomically(auction_id, buyer_id):
    """Release escrow only after Telegram ownership checks have passed."""
    try:
        return await to_thread(_settle_username_auction_sync, auction_id, buyer_id)
    except (ValueError, DatabaseError):
        logger.exception("Failed to settle username auction")
        return False, None, None


def _settle_username_auction_sync(auction_id, buyer_id):
    with connect() as db:
        auction = db.execute(
            """SELECT seller_id,current_bid,current_bidder FROM username_auctions
               WHERE auction_id=? AND status='awaiting_transfer' FOR UPDATE""", (auction_id,),
        ).fetchone()
        escrow = db.execute(
            """SELECT buyer_id,amount,fee,seller_amount
               FROM username_auction_escrows WHERE auction_id=? AND status IN ('held','review') FOR UPDATE""",
            (auction_id,),
        ).fetchone()
        if (not auction or not escrow or int(auction[2] or 0) != int(buyer_id)
                or int(escrow[0]) != int(buyer_id) or int(auction[0]) == int(buyer_id)
                or Decimal(str(auction[1])) != Decimal(str(escrow[1]))):
            db.rollback()
            return False, None, None
        seller_id = auction[0]
        _buyer_id, _amount, fee, seller_amount = escrow
        if db.execute(
            "UPDATE username_auction_escrows SET status='completed',updated_at=CURRENT_TIMESTAMP WHERE auction_id=? AND status IN ('held','review')",
            (auction_id,),
        ).rowcount != 1:
            db.rollback()
            return False, None, None
        if db.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (seller_amount, seller_id)).rowcount != 1:
            db.rollback()
            return False, None, None
        db.execute("INSERT INTO username_auction_fees (auction_id, amount) VALUES (?, ?)", (auction_id, fee))
        seller_balance = db.execute("SELECT coins FROM users WHERE user_id=?", (seller_id,)).fetchone()[0]
        db.execute(
            "INSERT INTO balance_ledger (user_id, amount, balance_after, operation, reference, fee) VALUES (?, ?, ?, ?, ?, ?)",
            (seller_id, seller_amount, seller_balance, "username_auction_sale", str(auction_id), fee),
        )
        db.execute("UPDATE username_auctions SET status='completed' WHERE auction_id=?", (auction_id,))
        db.commit()
        return True, fee, seller_amount


def _expire_username_auction_escrows_sync(limit=100):
    with connect() as db:
        rows = db.execute("""SELECT e.auction_id,e.buyer_id,a.seller_id FROM username_auction_escrows e
            JOIN username_auctions a ON a.auction_id=e.auction_id
            WHERE e.status='held' AND e.expires_at<=CURRENT_TIMESTAMP AND a.status='awaiting_transfer'
            ORDER BY e.expires_at LIMIT ? FOR UPDATE OF e SKIP LOCKED""", (limit,)).fetchall()
        expired = 0
        for auction_id, buyer_id, seller_id in rows:
            row = db.execute("""UPDATE username_auction_escrows SET status='review',updated_at=CURRENT_TIMESTAMP
                WHERE auction_id=? AND status='held' RETURNING amount""", (auction_id,)).fetchone()
            if not row:
                continue
            enqueue(db, buyer_id, f'username_auction:expired:{auction_id}',
                    'انتهت مهلة نقل اليوزر. بقي المبلغ محجوزاً للمراجعة؛ لن يُعاد تلقائياً لأن نقل الملكية يتم خارج البوت. إذا اكتمل النقل، استخدم زر التحقق في رسالة المزاد أو تواصل مع الدعم.')
            enqueue(db, seller_id, f'username_auction:expired_seller:{auction_id}',
                    'انتهت مهلة نقل اليوزر. بقي مبلغ المزاد محجوزاً للمراجعة؛ لن يُصرف أو يُعاد تلقائياً لأن نقل الملكية يتم خارج البوت. أكمل النقل ثم اطلب من الفائز التحقق، أو تواصل مع الدعم.')
            expired += 1
        if expired:
            db.commit()
        return expired


async def expire_username_auction_escrows():
    return await to_thread(_expire_username_auction_escrows_sync)


def _settle_auction_sync(auction_id, buyer_id):
    with connect() as db:
        from typed_auctions import verify
        auction = db.execute(
            """SELECT phone,session,seller_id,current_bid,twofa FROM auctions
               WHERE auction_id=? AND status='active' AND current_bidder=? FOR UPDATE""",
            (auction_id, buyer_id),
        ).fetchone()
        if not auction:
            return False, None, None
        phone, session, seller_id, amount, twofa = auction
        if int(seller_id) == int(buyer_id):
            db.rollback()
            return False, None, None
        amount = _decimal_amount(amount)
        setting = db.execute("SELECT value FROM settings WHERE key='auction_platform_fee_percent'").fetchone()
        try:
            percent = Decimal(str(setting[0] if setting else "0"))
            if not percent.is_finite():
                percent = Decimal("0")
        except Exception:
            percent = Decimal("0")
        percent = min(max(percent, Decimal("0")), Decimal("100"))
        fee = (amount * percent / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        seller_amount = amount - fee
        saved=verify(db,auction_id)
        if db.execute(
            "UPDATE auctions SET status='processing' WHERE auction_id=? AND status='active' AND current_bidder=?",
            (auction_id, buyer_id),
        ).rowcount != 1:
            return False, None, None
        if db.execute(
            "UPDATE users SET coins=coins-? WHERE user_id=? AND coins-referral_balance>=?",
            (amount, buyer_id, amount),
        ).rowcount != 1:
            db.rollback()
            return False, None, None
        if db.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (seller_amount, seller_id)).rowcount != 1:
            db.rollback()
            return False, None, None
        db.execute(
            "INSERT INTO platform_auction_fees (auction_id, amount) VALUES (?, ?)",
            (auction_id, fee),
        )
        db.execute(
            """INSERT INTO stored_accounts (phone, session, user_id, twofa) VALUES (?, ?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET session=excluded.session, user_id=excluded.user_id, twofa=excluded.twofa""",
            (phone, encrypt_session(decrypt_session(session)), buyer_id, encrypt_session(decrypt_session(twofa))),
        )
        buyer_balance = db.execute("SELECT coins FROM users WHERE user_id=?", (buyer_id,)).fetchone()[0]
        seller_balance = db.execute("SELECT coins FROM users WHERE user_id=?", (seller_id,)).fetchone()[0]
        db.execute(
            "INSERT INTO balance_ledger (user_id, amount, balance_after, operation, reference,number_snapshot) VALUES (?, ?, ?, ?, ?,?)",
            (buyer_id, -amount, buyer_balance, "auction_purchase", str(auction_id),saved),
        )
        db.execute(
            "INSERT INTO balance_ledger (user_id, amount, balance_after, operation, reference, fee,number_snapshot) VALUES (?, ?, ?, ?, ?, ?,?)",
            (seller_id, seller_amount, seller_balance, "auction_sale", str(auction_id), fee,saved),
        )
        db.execute("UPDATE auctions SET status='sold' WHERE auction_id=?", (auction_id,))
        db.execute('DELETE FROM purchased_accounts WHERE phone=? AND user_id=?',(phone,seller_id))
        db.execute('INSERT INTO commerce_sales(user_id,phone,revenue,number_snapshot) VALUES(?,?,?,?)',
                   (buyer_id,phone,amount,saved))
        db.commit()
        return True, fee, seller_amount


async def recover_stale_operations():
    """Return unfinished purchase workflows to a safe retryable state after restart."""
    await execute_query(
        "UPDATE pending_purchases SET status='pending', processing=0 WHERE status='processing'",
        commit=True,
    )
    await execute_query("UPDATE auctions SET status='active' WHERE status='processing'", commit=True)
    await execute_query("UPDATE username_auctions SET status='awaiting_transfer' WHERE status='processing'", commit=True)

async def is_user_in_channel(client, user_id, channel):
    try:
        result = await client(GetParticipantRequest(channel=channel, participant=user_id))
        participant = getattr(result, "participant", None)
        return bool(participant) and participant.__class__.__name__ not in {"ChannelParticipantLeft", "ChannelParticipantBanned"} \
            and not getattr(participant, "left", False)
    except (ChannelPrivateError, UserNotParticipantError):
        return False
    except Exception as e:
        logger.error(f"Error checking channel membership: {str(e)}")
        return False

async def logout_all_sessions(session_str, *, actor_id=None, phone=None):
    """Revoke other authorizations, preserving the authorization used by the bot."""
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH,
                                actor_id=actor_id, account_phone=phone, sensitive_action=True,
                                exclusive_session=True, flood_sleep_threshold=0,
                                request_retries=0, connection_retries=1)
        await asyncio.wait_for(client.connect(), 20)
        if not await asyncio.wait_for(client.is_user_authorized(), 20):
            return False
        if phone is not None:
            me = await asyncio.wait_for(client.get_me(), 20)
            if not me or str(getattr(me, 'phone', '') or '').lstrip('+') != str(phone).lstrip('+'):
                return False
        auths = await asyncio.wait_for(client(GetAuthorizationsRequest()), 20)
        success = True
        
        for auth in auths.authorizations:
            if auth.current:
                continue
            try:
                if not await asyncio.wait_for(client(ResetAuthorizationRequest(hash=auth.hash)), 20):
                    success = False
            except Exception as exc:
                success = False
                logger.warning("Other-session termination failed (%s)", type(exc).__name__)
        
        return success
    except Exception as exc:
        logger.warning("Other-session termination failed (%s)", type(exc).__name__)
        return False
    finally:
        if client:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 15)

async def ensure_mandatory_channel_membership(session, mandatory_channel=None):
    """Ensure one stored Telegram session belongs to the mandatory channel.

    Returns ``already``, ``joined``, ``disabled``, or ``failed`` so scheduled
    jobs can report their results without treating an existing membership as an
    error.
    """
    mandatory_channel = mandatory_channel or await get_setting("mandatory_channel")
    if not mandatory_channel:
        return "disabled"

    client = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        channel_entity = await client.get_input_entity(mandatory_channel)
        me = await client.get_me()
        try:
            await client(GetParticipantRequest(channel=channel_entity, participant=me.id))
            return "already"
        except UserNotParticipantError:
            await client(JoinChannelRequest(channel=channel_entity))
            await asyncio.sleep(2)
            return "joined"
        except UserAlreadyParticipantError:
            return "already"
    except Exception as e:
        logger.error("Error ensuring mandatory-channel membership: %s", e)
        return "failed"
    finally:
        if client and client.is_connected():
            await client.disconnect()


async def join_mandatory_channel(session):
    """Backward-compatible helper used when an account is added or purchased."""
    result = await ensure_mandatory_channel_membership(session)
    return result != "failed"


async def ensure_all_accounts_in_mandatory_channel():
    """Check every saved account and join accounts missing the mandatory channel."""
    mandatory_channel = await get_setting("mandatory_channel")
    if not mandatory_channel:
        logger.info("Mandatory-channel check skipped: no channel is configured")
        return {"checked": 0, "joined": 0, "failed": 0, "disabled": True}

    rows = await execute_query(
        """
        SELECT session FROM accounts WHERE session IS NOT NULL
        UNION ALL SELECT session FROM stored_accounts WHERE session IS NOT NULL
        UNION ALL SELECT session FROM sold_accounts WHERE session IS NOT NULL
        UNION ALL SELECT session FROM purchased_accounts WHERE session IS NOT NULL
        UNION ALL SELECT session FROM active_accounts WHERE session IS NOT NULL
        """,
        fetchall=True,
    )
    # The same account may appear in several tables; only connect once per session.
    sessions = set()
    for row in rows:
        session = decrypt_session(row[0])
        if session:
            sessions.add(session)
    summary = {"checked": 0, "joined": 0, "failed": 0, "disabled": False}
    for session in sessions:
        result = await ensure_mandatory_channel_membership(session, mandatory_channel)
        summary["checked"] += 1
        if result == "joined":
            summary["joined"] += 1
        elif result == "failed":
            summary["failed"] += 1

    logger.info(
        "Mandatory-channel check finished: checked=%d, joined=%d, failed=%d",
        summary["checked"], summary["joined"], summary["failed"],
    )
    return summary

async def is_vip(user_id):
    result = await execute_query(
        "SELECT 1 FROM vip_users WHERE user_id=?",
        (user_id,),
        fetchone=True
    )
    return bool(result)

async def add_vip_user(user_id):
    await execute_query(
        "INSERT OR IGNORE INTO vip_users (user_id) VALUES (?)",
        (user_id,),
        commit=True
    )

async def remove_vip_user(user_id):
    await execute_query(
        "DELETE FROM vip_users WHERE user_id=?",
        (user_id,),
        commit=True
    )

async def is_admin(user_id):
    from config import DEVELOPER_ID
    if user_id and user_id in {ADMIN_ID,DEVELOPER_ID}:
        return True
    result = await execute_query(
        "SELECT 1 FROM admins WHERE user_id=?",
        (user_id,),
        fetchone=True
    )
    return bool(result)

async def is_banned(user_id):
    result = await execute_query(
        "SELECT 1 FROM bad_guys WHERE user_id=?",
        (user_id,),
        fetchone=True
    )
    return bool(result)

async def get_countries():
    return await execute_query(
        "SELECT calling_code, name, price, sell_price, is_active, country_id FROM countries ORDER BY name",
        (),
        fetchall=True
    )

async def get_country(calling_code):
    return await execute_query(
        """SELECT calling_code, name, price, sell_price, is_active, country_id
           FROM countries WHERE calling_code=? ORDER BY country_id LIMIT 1""",
        (calling_code,),
        fetchone=True
    )


async def get_country_by_id(country_id):
    """Return the legacy country tuple plus its canonical ID as the last item."""
    return await execute_query(
        """SELECT calling_code, name, price, sell_price, is_active, country_id
           FROM countries WHERE country_id=?""",
        (country_id,),
        fetchone=True,
    )

async def add_new_country(name, calling_code, price, sell_price):
    def save():
        from inventory_pricing import seed_country_prices

        with connect() as db:
            country = db.execute(
                """INSERT INTO countries (name, calling_code, price, sell_price) VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    calling_code=excluded.calling_code,
                    price=excluded.price,
                    sell_price=excluded.sell_price,
                    is_active=1
                RETURNING country_id""",
                (name, calling_code, price, sell_price),
            ).fetchone()[0]
            seed_country_prices(db, country)

    await to_thread(save)

async def delete_country(calling_code):
    await execute_query(
        "DELETE FROM countries WHERE calling_code=?",
        (calling_code,),
        commit=True
    )

async def get_accounts(calling_code):
    rows = await execute_query(
        "SELECT * FROM accounts WHERE calling_code=? AND is_sold=0 AND is_active=1",
        (calling_code,),
        fetchall=True
    )
    return _decrypt_session_rows(rows, secret_indexes=(3,))

async def get_available_accounts_count(calling_code):
    row = await execute_query(
        """
        SELECT COUNT(*) FROM accounts
        WHERE calling_code=? AND is_sold=0 AND is_active=1
        """,
        (calling_code,),
        fetchone=True
    )
    return int(row[0]) if row else 0

async def get_countries_with_available_accounts():
    return await execute_query(
        """
        SELECT c.calling_code, c.name, c.price, c.sell_price, c.is_active,
               COUNT(a.phone) AS available_count, c.country_id
        FROM countries c
        JOIN accounts a
            ON a.calling_code=c.calling_code
            AND a.is_sold=0
            AND a.is_active=1
        WHERE c.is_active=1
        GROUP BY c.country_id, c.calling_code, c.name, c.price, c.sell_price, c.is_active
        HAVING COUNT(a.phone) > 0
        ORDER BY c.name
        """,
        (),
        fetchall=True
    )

async def get_all_accounts():
    rows = await execute_query(
        "SELECT * FROM accounts",
        (),
        fetchall=True
    )
    return _decrypt_session_rows(rows, secret_indexes=(3,))

async def get_account_info(phone):
    row = await execute_query(
        "SELECT * FROM accounts WHERE phone=?",
        (phone,),
        fetchone=True
    )
    return _decrypt_session_row(row, secret_indexes=(3,))

async def account_exists_anywhere(phone):
    """Prevent a submitted account from being listed twice across workflows."""
    row = await execute_query(
        """SELECT 1 FROM accounts WHERE phone=? UNION ALL
           SELECT 1 FROM pending_sales WHERE phone=? UNION ALL
           SELECT 1 FROM sold_accounts WHERE phone=? AND session IS NOT NULL UNION ALL
           SELECT 1 FROM purchased_accounts WHERE phone=? UNION ALL
           SELECT 1 FROM stored_accounts WHERE phone=? LIMIT 1""",
        (phone, phone, phone, phone, phone), fetchone=True,
    )
    return row is not None

async def add_account(phone, session, calling_code, twofa='لا يوجد', seller_id=0):
    await execute_query(
        """
        INSERT INTO accounts (phone, session, calling_code, twofa, seller_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            session=excluded.session,
            calling_code=excluded.calling_code,
            twofa=excluded.twofa,
            seller_id=excluded.seller_id,
            is_sold=0,
            is_active=1
        """,
        (phone, encrypt_session(session), calling_code, encrypt_session(twofa), seller_id),
        commit=True
    )


async def add_account_if_absent(phone, session, calling_code, twofa='no password', seller_id=0):
    from number_catalog import submission, insert_submission_sync
    data = submission.get()
    if not data:
        raise ValueError('Number type and country selection required')
    return await to_thread(insert_submission_sync,phone,session,calling_code,twofa,seller_id,data,False)


async def delete_account(phone):
    await execute_query(
        "DELETE FROM accounts WHERE phone=?",
        (phone,),
        commit=True
    )

async def add_pending_purchase(user_id, phone, calling_code, name, price, session, twofa):
    await execute_query(
        """INSERT INTO pending_purchases (user_id, phone, calling_code, name, price, session, twofa)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, phone) DO UPDATE SET
             calling_code=excluded.calling_code, name=excluded.name, price=excluded.price,
             session=excluded.session, twofa=excluded.twofa, status='pending', processing=0,
             updated_at=CURRENT_TIMESTAMP""",
        (user_id, phone, calling_code, name, price, encrypt_session(session), encrypt_session(twofa)),
        commit=True
    )


def _reserve_account_sync(user_id, phone):
    with connect() as db:
        cursor = db.cursor()
        cursor.execute("DELETE FROM account_reservations WHERE reserved_until<=CURRENT_TIMESTAMP")
        cursor.execute("SELECT phone, session, calling_code, twofa FROM accounts WHERE phone=? AND is_sold=0 AND is_active=1 FOR UPDATE", (phone,))
        account = cursor.fetchone()
        if not account:
            db.rollback()
            return None, "unavailable"
        cursor.execute(
            """INSERT INTO account_reservations (phone, user_id, reserved_until)
               VALUES (?, ?, CURRENT_TIMESTAMP + INTERVAL '5 minutes')
               ON CONFLICT(phone) DO UPDATE SET user_id=excluded.user_id,reserved_until=excluded.reserved_until,star_order_id=NULL
               WHERE (account_reservations.user_id=excluded.user_id AND account_reservations.star_order_id IS NULL)
                   OR account_reservations.reserved_until<=CURRENT_TIMESTAMP
               RETURNING user_id""",
            (phone, user_id),
        )
        holder = cursor.fetchone()
        if not holder or holder[0] != user_id:
            db.rollback()
            return None, "reserved"
        db.commit()
        return _decrypt_session_row(account, 1, (3,)), "reserved"


async def reserve_account_atomically(user_id, phone):
    try:
        return await to_thread(_reserve_account_sync, user_id, phone)
    except DatabaseError:
        logger.exception("Account reservation failed")
        return None, "database_error"


async def reservation_belongs_to_user(user_id, phone):
    row = await execute_query(
        "SELECT 1 FROM account_reservations WHERE phone=? AND user_id=? AND star_order_id IS NULL AND reserved_until>CURRENT_TIMESTAMP",
        (phone, user_id), fetchone=True,
    )
    return row is not None


async def release_account_reservation(phone, user_id=None):
    if user_id is None:
        await execute_query("DELETE FROM account_reservations r WHERE phone=? AND star_order_id IS NULL AND NOT EXISTS(SELECT 1 FROM deferred_purchases d WHERE d.phone=r.phone AND d.status='pending')", (phone,), commit=True)
    else:
        await execute_query("DELETE FROM account_reservations r WHERE phone=? AND user_id=? AND star_order_id IS NULL AND NOT EXISTS(SELECT 1 FROM deferred_purchases d WHERE d.phone=r.phone AND d.status='pending')", (phone, user_id), commit=True)


async def release_user_reservations(user_id):
    await execute_query("DELETE FROM account_reservations r WHERE user_id=? AND star_order_id IS NULL AND NOT EXISTS(SELECT 1 FROM deferred_purchases d WHERE d.phone=r.phone AND d.status='pending')", (user_id,), commit=True)


async def expire_account_reservations():
    await execute_query("DELETE FROM account_reservations WHERE reserved_until<=CURRENT_TIMESTAMP", commit=True)

async def get_pending_purchase(user_id, phone):
    row = await execute_query(
        "SELECT * FROM pending_purchases WHERE user_id=? AND phone=?",
        (user_id, phone),
        fetchone=True
    )
    return _decrypt_session_row(row, 6, (7,))

async def delete_pending_purchase(user_id, phone):
    await execute_query(
        "DELETE FROM pending_purchases WHERE user_id=? AND phone=?",
        (user_id, phone),
        commit=True
    )

async def add_stored_account(phone, session, user_id, twofa='لا يوجد', *, schedule_rotation=False):
    from number_catalog import submission
    if schedule_rotation and not submission.get():
        raise ValueError('اختر نوع الرقم والدولة قبل التنصيب.')
    return await to_thread(_store_account_sync, str(phone), session, user_id, twofa, schedule_rotation)


def _store_account_sync(phone, session, user_id, twofa, schedule_rotation):
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (phone,))
        from session_lifecycle import require_fresh_session
        require_fresh_session(db,session)
        for table in ('stored_accounts', 'purchased_accounts'):
            owner = db.execute(f"SELECT user_id FROM {table} WHERE phone=? FOR UPDATE", (phone,)).fetchone()
            if owner and owner[0] != user_id:
                raise ValueError('الحساب مسجل لمستخدم آخر ولا يمكن تغيير ملكيته بالتنصيب.')
        if schedule_rotation and db.execute("SELECT phone FROM accounts WHERE phone=? FOR UPDATE", (phone,)).fetchone():
            raise ValueError('الحساب موجود في مخزون البيع ولا يمكن إعادة تنصيبه.')
        stored = db.execute(
        """
        INSERT INTO stored_accounts (phone, session, user_id, twofa) VALUES (?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            session=excluded.session,
            user_id=excluded.user_id,
            twofa=CASE
                WHEN ?
                THEN stored_accounts.twofa
                ELSE excluded.twofa
            END
        WHERE stored_accounts.user_id=excluded.user_id
        """,
        (phone, encrypt_session(session), user_id, encrypt_session(twofa), twofa in (None, '', 'لا يوجد')),
        )
        if stored.rowcount != 1:
            raise ValueError('تغيرت ملكية الحساب أثناء العملية؛ لم يتم حفظه.')
        from number_catalog import submission, installed_metadata
        if submission.get():
            installed_metadata(db,phone,user_id,submission.get())
        if schedule_rotation:
            db.execute(
                """INSERT INTO scheduled_twofa_changes (phone, session, current_password, new_password, due_at, owner_id)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                   ON CONFLICT(phone) DO UPDATE SET current_password=excluded.current_password,
                   session=excluded.session, attempts=0, due_at=excluded.due_at,owner_id=excluded.owner_id,last_error=NULL
                   WHERE scheduled_twofa_changes.attempts>=3""",
                (phone, encrypt_session(session), encrypt_session(twofa if twofa != 'لا يوجد' else None),
                 encrypt_session(generate_account_twofa_password()), user_id),
            )
        db.commit()

async def get_stored_accounts(user_id):
    from account_access import is_manager,accounts
    if is_manager(user_id):
        return await accounts(user_id)
    rows = await execute_query(
        """SELECT s.phone,s.session,s.user_id,s.twofa,s.storage_date FROM stored_accounts s WHERE s.user_id=?
            AND NOT EXISTS(SELECT 1 FROM purchased_accounts p WHERE p.phone=s.phone AND p.user_id<>s.user_id)""",
        (user_id,),
        fetchall=True
    )
    return _decrypt_session_rows(rows, secret_indexes=(3,))


async def service_account_authorized(user_id, account):
    from account_access import authorized
    return await authorized(user_id,account)


def _release_expired_rentals_sync():
    with connect() as db:
        cursor = db.cursor()
        cursor.execute("SELECT phone FROM rental_leases WHERE status='active' AND ends_at<=CURRENT_TIMESTAMP")
        phones = [row[0] for row in cursor.fetchall()]
        if phones:
            cursor.execute("UPDATE rental_leases SET status='expired' WHERE status='active' AND ends_at<=CURRENT_TIMESTAMP")
            db.commit()


async def get_rented_accounts(user_id):
    """Return active leased sessions for service-only use; never expose 2FA."""
    await to_thread(_release_expired_rentals_sync)
    rows = await execute_query(
        """SELECT a.phone, a.session, l.ends_at FROM rental_leases l
           JOIN accounts a ON a.phone=l.phone
           WHERE l.renter_id=? AND l.status='active' AND l.ends_at>CURRENT_TIMESTAMP""",
        (user_id,), fetchall=True,
    )
    return _decrypt_session_rows(rows)


async def get_available_rental_inventory_count(user_id=None):
    """Return the current inventory count; it is informational, not a cap."""
    await to_thread(_release_expired_rentals_sync)
    row = await execute_query(
        """SELECT COUNT(*) FROM accounts a
           WHERE a.is_sold=0 AND a.is_active=1
             AND NOT EXISTS (
                 SELECT 1 FROM account_health h WHERE h.phone=a.phone
                 AND h.inventory_session=a.session
                 AND h.status IN ('revoked','banned','deleted','restricted','invalid','mismatch')
             )
             AND NOT EXISTS (
                 SELECT 1 FROM account_reservations r
                 WHERE r.phone=a.phone AND r.reserved_until>CURRENT_TIMESTAMP
             )""" + (" AND NOT EXISTS(SELECT 1 FROM rental_leases l WHERE l.phone=a.phone AND l.renter_id=? AND l.status='active' AND l.ends_at>CURRENT_TIMESTAMP)" if user_id is not None else ''),
        (user_id,) if user_id is not None else (), fetchone=True,
    )
    return int(row[0]) if row else 0


def _rent_accounts_sync(user_id, count, days, daily_price, quote_id=None):
    total = Decimal(str(daily_price)) * count * days
    ends_at = datetime.datetime.now() + timedelta(days=days)
    with connect() as db:
        cursor = db.cursor()
        cursor.execute("SELECT coins,referral_balance FROM users WHERE user_id=? FOR UPDATE", (user_id,))
        user = cursor.fetchone()
        from commerce import consume_quote, award_spend
        if quote_id:
            total = consume_quote(db, user_id, quote_id, 'rental', f'{count}:{days}:{Decimal(str(daily_price)).quantize(Decimal(".01"))}')
        if not user or Decimal(str(user[0])) < total:
            db.rollback()
            return None, "insufficient_balance"
        cursor.execute(
            """SELECT a.phone FROM accounts a
               WHERE a.is_sold=0 AND a.is_active=1
                 AND NOT EXISTS (
                     SELECT 1 FROM account_health h WHERE h.phone=a.phone
                     AND h.inventory_session=a.session
                     AND h.status IN ('revoked','banned','deleted','restricted','invalid','mismatch')
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM account_reservations r
                     WHERE r.phone=a.phone AND r.reserved_until>CURRENT_TIMESTAMP
                 )
                 AND NOT EXISTS(SELECT 1 FROM rental_leases l WHERE l.phone=a.phone AND l.renter_id=?
                     AND l.status='active' AND l.ends_at>CURRENT_TIMESTAMP)
               ORDER BY a.added_date FOR UPDATE SKIP LOCKED LIMIT ?""", (user_id, count)
        )
        phones = [row[0] for row in cursor.fetchall()]
        if len(phones) != count:
            db.rollback()
            return None, "insufficient_inventory"
        if not quote_id:
            award_spend(db, user_id, total)
        cursor.execute("""UPDATE users SET coins=coins-?,
            referral_balance=GREATEST(0,referral_balance-?) WHERE user_id=?""", (total, total, user_id))
        for phone in phones:
            cursor.execute(
                "INSERT INTO rental_leases (phone, renter_id, price, ends_at) VALUES (?, ?, ?, ?)",
                (phone, user_id, total / count, ends_at),
            )
        cursor.execute("SELECT coins FROM users WHERE user_id=?", (user_id,))
        balance = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO balance_ledger (user_id, amount, balance_after, operation, reference) VALUES (?, ?, ?, ?, ?)",
            (user_id, -total, balance, "number_rental", f"{count} accounts/{days} days"),
        )
        db.commit()
        return phones, total


async def rent_available_accounts(user_id, count, days, daily_price, quote_id=None):
    """Atomically charge a lease; an account may have distinct concurrent renters."""
    await to_thread(_release_expired_rentals_sync)
    return await to_thread(_rent_accounts_sync, user_id, count, days, daily_price, quote_id)

async def get_all_stored_accounts():
    rows = await execute_query(
        "SELECT phone, session, user_id, twofa, storage_date FROM stored_accounts",
        (),
        fetchall=True
    )
    return _decrypt_session_rows(rows, secret_indexes=(3,))


async def get_all_bot_service_accounts(actor=None):
    from account_access import is_manager,accounts
    from account_health import session_key
    if actor is None or not is_manager(actor):
        return []
    result=[]
    seen=set()
    for phone,session,owner,twofa,source in await accounts(actor):
        key=session_key(session)
        if key not in seen:
            seen.add(key)
            result.append((phone,session,source))
    return await filter_usable_accounts(result)


async def delete_stored_account(phone, *, user_id=None):
    if user_id is None:
        return await execute_query("DELETE FROM stored_accounts WHERE phone=?", (phone,), commit=True)
    return await to_thread(_delete_owned_installation, phone, user_id)


def _delete_owned_installation(phone, user_id):
    """Delete an installation locally, without logging out any Telegram session."""
    with connect() as db:
        removed = db.execute("DELETE FROM stored_accounts WHERE phone=? AND user_id=? RETURNING phone", (phone, user_id)).fetchone()
        if not removed:
            return False
        # Do not leave an installation's pending security change running after
        # the owner explicitly removes that installation from bot storage.
        db.execute("DELETE FROM scheduled_twofa_changes WHERE phone=? AND owner_id=?", (phone, user_id))
        return True

async def add_sold_account(phone, session, user_id):
    await execute_query(
        """
        INSERT INTO sold_accounts (phone, session, user_id) VALUES (?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET session=excluded.session, user_id=excluded.user_id
        """,
        (phone, encrypt_session(session), user_id),
        commit=True
    )

async def get_sold_accounts(user_id):
    rows = await execute_query(
        "SELECT * FROM sold_accounts WHERE user_id=?",
        (user_id,),
        fetchall=True
    )
    return _decrypt_session_rows(rows)

async def add_purchased_account(phone, session, user_id, calling_code=None, name=None, price=0, seller_id=None, twofa='لا يوجد'):
    await execute_query(
        """
        INSERT INTO purchased_accounts (phone, session, user_id, seller_id, calling_code, name, price, twofa)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            session=excluded.session,
            user_id=excluded.user_id,
            seller_id=excluded.seller_id,
            calling_code=excluded.calling_code,
            name=excluded.name,
            price=excluded.price,
            twofa=excluded.twofa
        """,
        (phone, encrypt_session(session), user_id, seller_id, calling_code, name, price, encrypt_session(twofa)),
        commit=True
    )

async def get_purchased_accounts(user_id):
    rows = await execute_query(
        "SELECT * FROM purchased_accounts WHERE user_id=?",
        (user_id,),
        fetchall=True
    )
    return _decrypt_session_rows(rows, secret_indexes=(8,))

async def add_active_account(phone, session, user_id, activity):
    await execute_query(
        """
        INSERT INTO active_accounts (phone, session, user_id, activity)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            session=excluded.session,
            user_id=excluded.user_id,
            activity=excluded.activity,
            last_activity=CURRENT_TIMESTAMP
        """,
        (phone, encrypt_session(session), user_id, activity),
        commit=True
    )

async def get_active_accounts(user_id):
    rows = await execute_query(
        "SELECT * FROM active_accounts WHERE user_id=?",
        (user_id,),
        fetchall=True
    )
    return _decrypt_session_rows(rows)

async def remove_active_account(phone):
    await execute_query(
        "DELETE FROM active_accounts WHERE phone=?",
        (phone,),
        commit=True
    )

async def get_force_channels():
    result = await execute_query(
        "SELECT channel_id FROM force_channels",
        (),
        fetchall=True
    )
    return [row[0] for row in result] if result else []

async def add_force_channel(channel_id):
    await execute_query(
        "INSERT OR IGNORE INTO force_channels (channel_id) VALUES (?)",
        (channel_id,),
        commit=True
    )

async def remove_force_channel(channel_id):
    await execute_query(
        "DELETE FROM force_channels WHERE channel_id=?",
        (channel_id,),
        commit=True
    )

async def add_super_channel(user_id, channel_id, title):
    channel_id = normalize_super_target(channel_id)
    await execute_query(
        """
        INSERT INTO user_super_channels (user_id, channel_id, title) VALUES (?, ?, ?)
        ON CONFLICT(user_id, channel_id) DO UPDATE SET title=excluded.title
        """,
        (user_id, channel_id, title),
        commit=True
    )

def normalize_super_target(target):
    target = str(target or "").strip()
    if target.startswith("https://t.me/"):
        target = target.replace("https://t.me/", "", 1)
    elif target.startswith("http://t.me/"):
        target = target.replace("http://t.me/", "", 1)
    elif target.startswith("t.me/"):
        target = target.replace("t.me/", "", 1)
    target = target.strip().strip("/")
    if "/" in target and not target.startswith(("joinchat/", "+")):
        target = target.split("/", 1)[0]
    return target

def extract_invite_hash(target):
    target = normalize_super_target(target)
    if target.startswith("+"):
        return target[1:]
    if target.startswith("joinchat/"):
        return target.split("/", 1)[1]
    return None

async def resolve_posting_target(client, target):
    target = normalize_super_target(target)
    invite_hash = extract_invite_hash(target)

    if invite_hash:
        try:
            updates = await client(ImportChatInviteRequest(invite_hash))
            if getattr(updates, "chats", None):
                return updates.chats[0]
        except UserAlreadyParticipantError:
            invite = await client(CheckChatInviteRequest(invite_hash))
            if hasattr(invite, "chat"):
                return invite.chat
        except Exception:
            invite = await client(CheckChatInviteRequest(invite_hash))
            if hasattr(invite, "chat"):
                return invite.chat

    try:
        numeric_target = int(target)
        return await client.get_entity(numeric_target)
    except ValueError:
        pass

    public_target = target
    if public_target.startswith("@"):
        public_target = public_target[1:]

    entity = await client.get_entity(public_target)
    try:
        await client(JoinChannelRequest(entity))
    except UserAlreadyParticipantError:
        pass
    except Exception:
        pass
    return entity

async def send_post_to_super_target(client, target, message, entity=None):
    entity = entity or await resolve_posting_target(client, target)
    await client.send_message(entity, message)
    return entity


async def save_super_account_target(user_id, phone, target, entity):
    peer_id = getattr(entity, "id", None) or getattr(entity, "channel_id", None)
    access_hash = getattr(entity, "access_hash", None)
    if peer_id is None or access_hash is None:
        return False
    await execute_query(
        """INSERT INTO super_account_targets (user_id, phone, target, peer_id, access_hash)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (user_id, phone, target) DO UPDATE SET
               peer_id=excluded.peer_id, access_hash=excluded.access_hash,
               updated_at=CURRENT_TIMESTAMP""",
        (user_id, str(phone), normalize_super_target(target), int(peer_id), int(access_hash)),
        commit=True,
    )
    return True


async def get_super_account_target(user_id, phone, target):
    row = await execute_query(
        "SELECT peer_id, access_hash FROM super_account_targets WHERE user_id=? AND phone=? AND target=?",
        (user_id, str(phone), normalize_super_target(target)),
        fetchone=True,
    )
    return InputPeerChannel(int(row[0]), int(row[1])) if row else None

async def verify_super_target_with_stored_account(session, target):
    client = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        entity = await resolve_posting_target(client, target)
        return entity
    finally:
        if client and client.is_connected():
            await client.disconnect()


async def join_super_target_with_stored_account(session, target):
    """Join a super target and propagate failures instead of treating lookup as success."""
    client = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        return await join_target_with_client(client, target)
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def join_target_with_client(client, target):
    """Join using the caller's client; never disconnect a shared client here."""
    target = normalize_super_target(target)
    invite_hash = extract_invite_hash(target)
    if invite_hash:
        try:
            updates = await client(ImportChatInviteRequest(invite_hash))
            return updates.chats[0] if getattr(updates, 'chats', None) else None
        except UserAlreadyParticipantError:
            invite = await client(CheckChatInviteRequest(invite_hash))
            return getattr(invite, 'chat', None)
    try:
        entity = await client.get_entity(int(target))
    except ValueError:
        entity = await client.get_entity(target.lstrip('@'))
    if not isinstance(entity, types.Channel):
        raise ValueError('Target must be a channel or supergroup')
    try:
        await client(JoinChannelRequest(entity))
    except UserAlreadyParticipantError:
        pass
    return entity

async def get_super_channels(user_id):
    return await execute_query(
        "SELECT channel_id, title FROM user_super_channels WHERE user_id=? ORDER BY created_at",
        (user_id,),
        fetchall=True
    )

async def delete_super_channel(user_id, channel_id):
    await execute_query(
        "DELETE FROM user_super_channels WHERE user_id=? AND channel_id=?",
        (user_id, channel_id),
        commit=True
    )

async def add_posting_template(user_id, template_text):
    await execute_query(
        "INSERT INTO user_posting_templates (user_id, template_text) VALUES (?, ?)",
        (user_id, template_text),
        commit=True
    )

async def get_posting_templates(user_id):
    return await execute_query(
        "SELECT template_id, template_text FROM user_posting_templates WHERE user_id=? ORDER BY template_id",
        (user_id,),
        fetchall=True
    )

async def delete_posting_template(user_id, template_id):
    await execute_query(
        "DELETE FROM user_posting_templates WHERE user_id=? AND template_id=?",
        (user_id, template_id),
        commit=True
    )

async def update_posting_template(user_id, template_id, new_text):
    await execute_query(
        "UPDATE user_posting_templates SET template_text=? WHERE user_id=? AND template_id=?",
        (new_text, user_id, template_id),
        commit=True
    )

async def set_timed_name_active(user_id, active):
    await execute_query(
        """
        INSERT INTO timed_names (user_id, active) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET active=excluded.active
        """,
        (user_id, active),
        commit=True
    )

async def is_timed_name_active(user_id):
    result = await execute_query(
        "SELECT active FROM timed_names WHERE user_id=?",
        (user_id,),
        fetchone=True
    )
    return result[0] if result else False

async def create_auto_creation_task(user_id, seconds_interval, hours_duration):
    await execute_query(
        "INSERT INTO auto_creation_tasks (user_id, seconds_interval, hours_duration) VALUES (?, ?, ?)",
        (user_id, seconds_interval, hours_duration),
        commit=True
    )

async def get_active_auto_creation_tasks():
    return await execute_query(
        "SELECT * FROM auto_creation_tasks WHERE active=1",
        (),
        fetchall=True
    )

async def update_auto_creation_task(task_id, remaining_runs=None, active=None):
    if remaining_runs is not None and active is not None:
        await execute_query(
            "UPDATE auto_creation_tasks SET remaining_runs=?, active=? WHERE task_id=?",
            (remaining_runs, active, task_id),
            commit=True
        )
    elif remaining_runs is not None:
        await execute_query(
            "UPDATE auto_creation_tasks SET remaining_runs=? WHERE task_id=?",
            (remaining_runs, task_id),
            commit=True
        )
    elif active is not None:
        await execute_query(
            "UPDATE auto_creation_tasks SET active=? WHERE task_id=?",
            (active, task_id),
            commit=True
        )

async def add_pending_sale(phone, seller_id, price, calling_code, session, twofa):
    await execute_query(
        """
        INSERT INTO pending_sales (phone, seller_id, price, calling_code, session, twofa)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            seller_id=excluded.seller_id,
            price=excluded.price,
            calling_code=excluded.calling_code,
            session=excluded.session,
            twofa=excluded.twofa
        """,
        (phone, seller_id, price, calling_code, encrypt_session(session), encrypt_session(twofa)),
        commit=True
    )


async def add_pending_sale_if_absent(phone, seller_id, price, calling_code, session, twofa):
    from number_catalog import submission, insert_submission_sync
    data = submission.get()
    if not data:
        raise ValueError('Number type and country selection required')
    return await to_thread(insert_submission_sync,phone,session,calling_code,twofa,seller_id,data,True)


async def get_pending_sale(phone):
    row = await execute_query(
        "SELECT * FROM pending_sales WHERE phone=?",
        (phone,),
        fetchone=True
    )
    return _decrypt_session_row(row, 4, (5,))


def migrate_sensitive_data():
    """Encrypt legacy plaintext session and 2FA values after configuration validation."""
    fields = {
        "star_purchase_orders": ("order_id", "inventory_session"),
        "accounts": ("phone", "session", "twofa"), "pending_purchases": ("purchase_id", "session", "twofa"),
        "stored_accounts": ("phone", "session", "twofa"), "purchased_accounts": ("phone", "session", "twofa"),
        "pending_sales": ("phone", "session", "twofa"), "auctions": ("auction_id", "session", "twofa"),
        "sold_accounts": ("phone", "session"), "active_accounts": ("phone", "session"),
    }
    with connect() as db:
        for table, values in fields.items():
            key, *columns = values
            for column in columns:
                for record_id, value in db.execute(f"SELECT {key}, {column} FROM {table}").fetchall():
                    if value and not str(value).startswith(_SESSION_PREFIX):
                        db.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (encrypt_session(value), record_id))
        db.commit()


def migrate_user_scoped_control_data():
    """Assign legacy global control data to the owner once, preserving it safely."""
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (ADMIN_ID,))
        db.execute(
            """INSERT OR IGNORE INTO user_super_channels (user_id, channel_id, title)
               SELECT ?, channel_id, title FROM super_channels WHERE is_active=1""",
            (ADMIN_ID,),
        )
        db.execute(
            """INSERT INTO user_posting_templates (user_id, template_text)
               SELECT ?, template_text FROM posting_templates
               WHERE NOT EXISTS (SELECT 1 FROM user_posting_templates LIMIT 1)""",
            (ADMIN_ID,),
        )
        db.commit()

async def delete_pending_sale(phone):
    await execute_query(
        "DELETE FROM pending_sales WHERE phone=?",
        (phone,),
        commit=True
    )

async def get_active_posting_tasks():
    return await execute_query(
        "SELECT * FROM active_posting_tasks WHERE active=1",
        (),
        fetchall=True
    )

async def create_active_posting_task(
    user_id,
    phone,
    interval,
    repetitions,
    all_channels=False,
    channel_id=None,
    posting_mode="single_template",
    selected_template_id=None,
):
    row = await execute_query(
        """
        INSERT INTO active_posting_tasks
            (
                user_id, phone, channel_id, all_channels, seconds_interval,
                repetitions, remaining, active, posting_mode, selected_template_id
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING task_id
        """,
        (
            user_id,
            phone,
            str(channel_id) if channel_id is not None else None,
            bool(all_channels),
            interval,
            repetitions,
            repetitions,
            True,
            posting_mode,
            selected_template_id,
        ),
        fetchone=True,
        commit=True,
    )
    return row[0] if row else None


async def is_posting_task_id_active(task_id):
    row = await execute_query(
        "SELECT active FROM active_posting_tasks WHERE task_id=?",
        (task_id,),
        fetchone=True,
    )
    return bool(row and row[0])

async def is_posting_task_active(user_id, channel_id=None, all_channels=False):
    if all_channels:
        row = await execute_query(
            """
            SELECT active FROM active_posting_tasks
            WHERE user_id=? AND all_channels=1
            ORDER BY task_id DESC LIMIT 1
            """,
            (user_id,),
            fetchone=True
        )
    else:
        row = await execute_query(
            """
            SELECT active FROM active_posting_tasks
            WHERE user_id=? AND channel_id=?
            ORDER BY task_id DESC LIMIT 1
            """,
            (user_id, str(channel_id) if channel_id is not None else None),
            fetchone=True
        )
    return bool(row[0]) if row else True

async def update_posting_task(task_id, remaining=None, active=None):
    if remaining is not None and active is not None:
        await execute_query(
            "UPDATE active_posting_tasks SET remaining=?, active=? WHERE task_id=?",
            (remaining, active, task_id),
            commit=True
        )
    elif remaining is not None:
        await execute_query(
            "UPDATE active_posting_tasks SET remaining=? WHERE task_id=?",
            (remaining, task_id),
            commit=True
        )
    elif active is not None:
        await execute_query(
            "UPDATE active_posting_tasks SET active=? WHERE task_id=?",
            (active, task_id),
            commit=True
        )

async def count_ses(session):
    client = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        auths = await client(GetAuthorizationsRequest())
        return sum(1 for auth in auths.authorizations if not auth.current)
    except Exception as e:
        logger.error(f"Session count error: {str(e)}")
        return None
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def create_private_group(session_str, group_title=None):
    """
    إنشاء مجموعة خاصة باستخدام جلسة معينة
    :param session_str: جلسة الحساب
    :param group_title: عنوان المجموعة (اختياري)
    :return: (group_id, invite_link, participants_count) أو (None, None, 0) في حالة الفشل
    """
    client = None
    try:
        # إنشاء عميل تليثون
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        
        # اختيار اسم عشوائي إذا لم يتم تحديده
        if not group_title:
            group_title = random.choice(GROUP_NAMES)
        
        # إنشاء المجموعة
        result = await client(CreateChannelRequest(
            title=group_title,
            about="مجموعة تم إنشاؤها بواسطة البوت",
            megagroup=True
        ))
        
        channel = result.chats[0]
        chat_id = channel.id
        chat_entity = InputPeerChannel(chat_id, channel.access_hash)
        
        # إعداد حقوق المحادثة
        banned_rights = ChatBannedRights(
            until_date=None,
            view_messages=False,
            send_messages=True,  # منع الأعضاء من إرسال الرسائل
            send_media=True,
            send_stickers=True,
            send_gifs=True,
            send_games=True,
            send_inline=True,
            embed_links=True,
            send_polls=True,
            change_info=True,
            invite_users=True,
            pin_messages=True
        )
        
        await client(EditChatDefaultBannedRightsRequest(
            peer=chat_entity,
            banned_rights=banned_rights
        ))
        
        # إرسال رسائل ترحيبية
        await client(SendMessageRequest(
            peer=chat_entity,
            message="تم انشاء هذه المجموعة بواسطة البوت",
            no_webpage=True
        ))
        
        await client(SendMessageRequest(
            peer=chat_entity,
            message=f"مرحبا بكم في {group_title}",
            no_webpage=True
        ))
        
        # إنشاء رابط الدعوة
        invite_link = await client(ExportChatInviteRequest(peer=chat_entity))
        
        return chat_id, invite_link.link, 1  # العدد 1 لأن البوت هو العضو الوحيد
        
    except Exception as e:
        logger.error(f"Error creating group: {str(e)}")
        return None, None, 0
    finally:
        if client and client.is_connected():
            await client.disconnect()

@workflow.tracked('creation')
async def run_auto_creation(user_id, interval_seconds, duration_hours, selected_accounts=None):
    """
    تشغيل عملية الإنشاء التلقائي في الخلفية
    """
    end_time = datetime.datetime.now() + timedelta(hours=duration_hours)
    stored_accounts = selected_accounts or await get_stored_accounts(user_id)
    
    if not stored_accounts:
        await bot.send_message(user_id, "❌ لا توجد حسابات مخزنة لاستخدامها في الإنشاء!")
        return
    
    processed = 0
    while datetime.datetime.now() < end_time and auto_creation_tasks.get(user_id, {}).get('active', False):
        try:
            # Every selected account executes one creation per interval. Do
            # not distribute one requested operation across all accounts.
            for account in stored_accounts:
                await workflow.progress(success=auto_creation_tasks.get(user_id, {}).get('total_created', 0))
                if not auto_creation_tasks.get(user_id, {}).get('active', False):
                    break
                group_id, invite_link, _ = await create_private_group(account[1])
                processed += 1
                if group_id:
                    auto_creation_tasks[user_id]['total_created'] += 1
                    await bot.send_message(
                        user_id,
                        f"✅ تم إنشاء مجموعة تلقائية #{auto_creation_tasks[user_id]['total_created']} من الحساب +{account[0]}\n"
                        f"🆔: {group_id}\n"
                        f"🔗: {invite_link}"
                    )
                count = auto_creation_tasks.get(user_id, {}).get('total_created', 0)
                await workflow.progress(processed=processed, success=count, failed=max(0,processed-count))
            
            await workflow.pause(interval_seconds)
            
        except Exception as e:
            logger.error(f"Error in auto creation: {str(e)}")
            await asyncio.sleep(interval_seconds)
    
    # إرسال ملخص النتائج
    total_created = auto_creation_tasks.get(user_id, {}).get('total_created', 0)
    await bot.send_message(
        user_id,
        f"⏹ انتهت مهمة الإنشاء التلقائي\n"
        f"✅ تم إنشاء {total_created} مجموعات بنجاح"
    )
    
    if user_id in auto_creation_tasks:
        del auto_creation_tasks[user_id]

async def update_timed_name(session):
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        
        # الحصول على الوقت الحالي بنظام 12 ساعة
        now = datetime.datetime.now()
        time_str = now.strftime("%I:%M %p")
        
        # تحديث الاسم
        await client(UpdateProfileRequest(
            last_name=time_str
        ))
        
        return True
    except Exception as e:
        print(f"Error updating timed name: {str(e)}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def remove_timed_name(session):
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        
        # الحصول على الاسم الأول
        me = await client.get_me()
        first_name = me.first_name if me.first_name else ""
        
        # تحديث الاسم بإزالة الحقل الثاني
        await client(UpdateProfileRequest(
            last_name="",
            first_name=first_name
        ))
        
        return True
    except Exception as e:
        print(f"Error removing timed name: {str(e)}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

@workflow.tracked('posting')
async def run_posting_task(
    user_id,
    phone,
    interval,
    repetitions,
    all_channels=False,
    channel_id=None,
    posting_mode="single_template",
    selected_template_id=None,
    task_id=None,
    total_repetitions=None,
):
    if task_id is None:
        task_id = await create_active_posting_task(
            user_id, phone, interval, repetitions, all_channels, channel_id,
            posting_mode, selected_template_id,
        )
    if task_id is None or task_id in _running_posting_task_ids:
        return
    _running_posting_task_ids.add(task_id)
    total_repetitions = total_repetitions or repetitions
    # الحصول على الحساب المحدد
    stored_accounts = await get_stored_accounts(user_id)
    account = next((acc for acc in stored_accounts if acc[0] == phone), None)
    
    if not account:
        await workflow.note_failure('الحساب المحدد لم يعد متاحًا للنشر')
        await bot.send_message(user_id, "❌ الحساب المحدد لم يعد متاحاً!")
        await update_posting_task(task_id, active=False)
        _running_posting_task_ids.discard(task_id)
        return
    
    templates = await get_posting_templates(user_id)
    super_channels = await get_super_channels(user_id)
    
    if not templates or not super_channels:
        await workflow.note_failure('توقف النشر بسبب نقص القوالب أو القنوات')
        await bot.send_message(user_id, "❌ توقف النشر بسبب نقص البيانات!")
        await update_posting_task(task_id, active=False)
        _running_posting_task_ids.discard(task_id)
        return

    if posting_mode == "all_templates":
        posting_templates = list(templates)
    elif selected_template_id is not None:
        posting_templates = [template for template in templates if int(template[0]) == int(selected_template_id)]
    else:
        posting_templates = [random.choice(templates)]

    if not posting_templates:
        await bot.send_message(user_id, "❌ توقف النشر لأن الكليشة المحددة غير موجودة!")
        await update_posting_task(task_id, active=False)
        _running_posting_task_ids.discard(task_id)
        return
    
    # إعداد الجلسة
    session_str = account[1]
    progress_message = None
    job_success = job_failed = 0

    async def update_progress_message(text):
        nonlocal progress_message
        if 'عطل مؤكد' in text:
            await workflow.note_failure('توقف النشر بسبب عطل مؤكد في الحساب')
        if progress_message:
            try:
                await progress_message.edit(text)
                return
            except Exception as e:
                logger.error(f"Error editing posting progress message: {str(e)}")
        progress_message = await bot.send_message(user_id, text)
    
    try:
        total_cycles = total_repetitions
        await workflow.progress(total=total_repetitions, processed=total_repetitions-repetitions)
        while repetitions > 0:
            await workflow.progress(processed=total_cycles-repetitions)
            if not await service_account_authorized(user_id,account) or not await session_is_usable(session_str):
                await update_posting_task(task_id, active=False)
                await update_progress_message("🩺 توقف النشر بسبب عطل مؤكد في الحساب. راجع صحة الحسابات.")
                break
            if not await is_posting_task_id_active(task_id):
                await update_progress_message("⏹ تم إيقاف مهمة النشر.")
                break

            retry_cycle_after_flood = False
            for template_index, template in enumerate(posting_templates, 1):
                await workflow.progress(message='جاري إرسال قالب النشر')
                if not await service_account_authorized(user_id,account) or not await session_is_usable(session_str):
                    await update_posting_task(task_id, active=False)
                    await update_progress_message("🩺 توقف النشر بسبب عطل مؤكد في الحساب.")
                    return
                if not await is_posting_task_id_active(task_id):
                    await update_progress_message("⏹ تم إيقاف مهمة النشر.")
                    break

                client = None
                success_count = 0
                failed_count = 0
                failed_details = []
                flood_wait_seconds = 0

                try:
                    cooldown_key = (str(phone), str(channel_id) if channel_id else "all")
                    cooldown = _posting_flood_wait_until.get(cooldown_key, 0)
                    cooldown_remaining = cooldown - asyncio.get_running_loop().time()
                    if cooldown_remaining > 0:
                        flood_wait_seconds = math.ceil(cooldown_remaining)
                        raise FloodWaitError(request=None, capture=flood_wait_seconds)
                    client = TelegramClient(StringSession(session_str), API_ID, API_HASH, actor_id=user_id,account_phone=phone,exclusive_session=True)
                    await client.connect()

                    if all_channels:
                        for channel in super_channels:
                            await workflow.progress()
                            if not await service_account_authorized(user_id,account) or not await session_is_usable(session_str):
                                await update_posting_task(task_id, active=False)
                                await update_progress_message("🩺 توقف النشر بسبب عطل مؤكد في الحساب.")
                                return
                            if not await is_posting_task_id_active(task_id):
                                break
                            try:
                                cached_entity = await get_super_account_target(user_id, phone, channel[0])
                                entity = await send_post_to_super_target(
                                    client, channel[0], template[1], entity=cached_entity,
                                )
                                if cached_entity is None:
                                    await save_super_account_target(user_id, phone, channel[0], entity)
                                success_count += 1
                                await asyncio.sleep(1)
                            except FloodWaitError as e:
                                flood_wait_seconds = int(e.seconds) + 1
                                _posting_flood_wait_until[cooldown_key] = (
                                    asyncio.get_running_loop().time() + flood_wait_seconds
                                )
                                failed_count += 1
                                failed_details.append((channel[1] or channel[0], f"انتظار تيليجرام {flood_wait_seconds} ثانية"))
                                break
                            except Exception as e:
                                failed_count += 1
                                label = channel[1] or channel[0]
                                failed_details.append((label, str(e)))
                                logger.error(f"Error posting to {channel[0]}: {str(e)}")
                    elif channel_id:
                        try:
                            cached_entity = await get_super_account_target(user_id, phone, channel_id)
                            entity = await send_post_to_super_target(
                                client, channel_id, template[1], entity=cached_entity,
                            )
                            if cached_entity is None:
                                await save_super_account_target(user_id, phone, channel_id, entity)
                            success_count += 1
                        except FloodWaitError as e:
                            flood_wait_seconds = int(e.seconds) + 1
                            _posting_flood_wait_until[cooldown_key] = (
                                asyncio.get_running_loop().time() + flood_wait_seconds
                            )
                            failed_count += 1
                            label = next(
                                (item[1] for item in super_channels if str(item[0]) == str(channel_id)),
                                str(channel_id),
                            )
                            failed_details.append((label, f"انتظار تيليجرام {flood_wait_seconds} ثانية"))
                        except Exception as e:
                            failed_count += 1
                            label = next(
                                (item[1] for item in super_channels if str(item[0]) == str(channel_id)),
                                str(channel_id),
                            )
                            failed_details.append((label, str(e)))
                            logger.error(f"Error posting to {channel_id}: {str(e)}")

                    cycle_number = total_cycles - repetitions + 1
                    template_status = (
                        f"📝 الكليشة: {template_index}/{len(posting_templates)}\n"
                        if posting_mode == "all_templates"
                        else ""
                    )
                    failure_text = ""
                    if failed_details:
                        detail_lines = [
                            f"• +{phone} — {target}: {reason[:180]}"
                            for target, reason in failed_details
                        ]
                        failure_text = "\nتفاصيل الفشل:\n" + "\n".join(detail_lines)
                        failure_text = failure_text[:3000]
                    if success_count:
                        await update_progress_message(
                            f"📢 تم النشر باستخدام الحساب: +{phone}\n"
                            f"{template_status}"
                            f"✅ الناجح: {success_count}\n"
                            f"❌ الفاشل: {failed_count}\n"
                            f"🔁 الدورة: {cycle_number}/{total_cycles}\n"
                            f"🔄 الدورات المتبقية: {repetitions - 1}"
                            f"{failure_text}"
                        )
                    else:
                        await update_progress_message(
                            f"❌ لم يتم النشر في أي مجموعة بهذه المحاولة.\n"
                            f"{template_status}"
                            f"• الحساب: +{phone}\n"
                            f"🔁 الدورة: {cycle_number}/{total_cycles}\n"
                            f"🔄 الدورات المتبقية: {repetitions - 1}"
                            f"{failure_text}"
                        )
                except FloodWaitError as e:
                    flood_wait_seconds = max(flood_wait_seconds, int(e.seconds) + 1)
                    _posting_flood_wait_until[cooldown_key] = (
                        asyncio.get_running_loop().time() + flood_wait_seconds
                    )
                    await update_progress_message(
                        f"تم تأجيل النشر للحساب +{phone} لمدة {flood_wait_seconds} ثانية بسبب حد تيليجرام."
                    )
                except Exception as e:
                    failed_count += 1
                    target = "جميع السوبرات" if all_channels else next(
                        (item[1] for item in super_channels if str(item[0]) == str(channel_id)),
                        str(channel_id),
                    )
                    logger.exception("Posting account %s failed before delivery", mask_phone(phone))
                    await update_progress_message(
                        "❌ فشلت محاولة النشر.\n"
                        f"• الحساب: +{phone}\n"
                        f"• السوبر: {target}\n"
                        f"• السبب: {str(e)[:500]}"
                    )
                finally:
                    if client and client.is_connected():
                        await client.disconnect()

                job_success += success_count
                job_failed += failed_count
                await workflow.progress(success=job_success, failed=job_failed,
                                        message='انتظار تيليجرام قبل إعادة المحاولة' if flood_wait_seconds else 'جارٍ تنفيذ دورات النشر')
                if flood_wait_seconds:
                    retry_cycle_after_flood = True
                    next_post = datetime.datetime.now() + timedelta(seconds=flood_wait_seconds)
                    await execute_query(
                        "UPDATE active_posting_tasks SET next_post=? WHERE task_id=?",
                        (next_post, task_id), commit=True,
                    )
                    await workflow.pause(flood_wait_seconds)
                    break

                is_last_template = template_index == len(posting_templates)
                is_last_cycle = repetitions == 1 and is_last_template
                if not is_last_cycle:
                    next_post = datetime.datetime.now() + timedelta(seconds=interval)
                    await execute_query(
                        "UPDATE active_posting_tasks SET next_post=? WHERE task_id=?",
                        (next_post, task_id), commit=True,
                    )
                    await workflow.pause(interval)
            else:
                repetitions -= 1
                await execute_query(
                    "UPDATE active_posting_tasks SET remaining=?, last_post=CURRENT_TIMESTAMP, next_post=NULL WHERE task_id=?",
                    (repetitions, task_id), commit=True,
                )
                continue

            if retry_cycle_after_flood:
                continue
            break
        else:
            await workflow.progress(processed=total_cycles, success=job_success, failed=job_failed, message='اكتملت دورات النشر')
            await update_progress_message("✅ اكتملت مهمة النشر التلقائي!")
    finally:
        # تنظيف الذاكرة
        if user_id in user_posting_accounts:
            del user_posting_accounts[user_id]
        _running_posting_task_ids.discard(task_id)
        # A cancellation during shutdown must leave the row active for restart.
        if isinstance(sys.exc_info()[1], workflow.JobStopped) or not isinstance(sys.exc_info()[1], asyncio.CancelledError):
            await update_posting_task(task_id, active=False)

async def run_multi_template_posting_task(
    user_id,
    phone,
    interval,
    repetitions,
    all_channels=False,
    channel_id=None,
    posting_mode="all_templates",
    selected_template_id=None,
):
    await run_posting_task(
        user_id=user_id,
        phone=phone,
        interval=interval,
        repetitions=repetitions,
        all_channels=all_channels,
        channel_id=channel_id,
        posting_mode=posting_mode,
        selected_template_id=selected_template_id,
    )


async def resume_active_posting_tasks():
    """Recreate every persisted posting worker after a bot restart."""
    rows = await get_active_posting_tasks() or []

    async def resume_one(row):
        (
            task_id, user_id, phone, channel_id, all_channels, interval,
            repetitions, remaining, _active, _last_post, next_post,
            posting_mode, selected_template_id,
        ) = row
        remaining = int(remaining or 0)
        if remaining <= 0:
            await update_posting_task(task_id, active=False)
            return
        if next_post:
            delay = max(0, (next_post - datetime.datetime.now()).total_seconds())
            if delay:
                await asyncio.sleep(delay)
        await run_posting_task(
            user_id=user_id,
            phone=phone,
            interval=int(interval),
            repetitions=remaining,
            all_channels=bool(all_channels),
            channel_id=channel_id,
            posting_mode=posting_mode or "single_template",
            selected_template_id=selected_template_id,
            task_id=task_id,
            total_repetitions=int(repetitions or remaining),
        )

    resumed = 0
    for row in rows:
        task_id = row[0]
        if task_id in _running_posting_task_ids:
            continue
        start_posting_worker(
            resume_one(row),
            name=f"posting-resume-{task_id}",
            user_id=row[1],
            task_id=task_id,
        )
        resumed += 1
    if resumed:
        logger.info("Resumed %s persistent posting task(s)", resumed)
    return resumed
async def create_groups(bot,user_id, count, manual=False):
    # الحصول على الحسابات المخزنة للمستخدم
    stored_accounts = await get_stored_accounts(user_id)
    
    if not stored_accounts:
        await bot.send_message(user_id, "❌ لا توجد حسابات مخزنة لاستخدامها في الإنشاء!")
        return
    
    created_count = 0
    for i in range(count):
        if manual and not manual_creation_tasks.get(user_id, {}).get('active', True):
            break
        
        # اختيار حساب عشوائي
        account = random.choice(stored_accounts)
        session = account[1]
        
        # إنشاء المجموعة
        group_id, invite_link, participants_count = await create_private_group(session)
        
        if group_id:
            created_count += 1
            await bot.send_message(
                user_id,
                msgs['GROUP_CREATED'].format(
                    group_id, 
                    invite_link, 
                    participants_count
                )
            )
            
            # الانضمام إلى القناة الإجبارية بعد إنشاء المجموعة
            await join_mandatory_channel(session)
            
            # الانتظار قبل الإنشاء التالي
            if manual:
                await asyncio.sleep(10)
    
    if manual:
        await bot.send_message(user_id, f"✅ تم إنشاء {created_count} من أصل {count} مجموعات بنجاح!")

async def start_auto_creation(user_id, seconds_interval, hours_duration):
    end_time = datetime.datetime.now() + timedelta(hours=hours_duration)
    runs = 0
    
    # الحصول على المهام النشطة من قاعدة البيانات
    tasks = await get_active_auto_creation_tasks()
    user_task = next((t for t in tasks if t[1] == user_id), None)
    
    if not user_task:
        return
    
    task_id = user_task[0]
    
    while datetime.datetime.now() < end_time and runs < 10:
        # التحقق من حالة المهمة
        if not auto_creation_tasks.get(user_id, False):
            break
        
        # إنشاء 10 مجموعات
        await create_groups(user_id, 50)
        runs += 1
        
        # تحديث عدد العمليات المتبقية
        remaining_runs = 50 - runs
        await update_auto_creation_task(task_id, remaining_runs=remaining_runs)
        
        # الانتظار لفترة محددة
        await asyncio.sleep(seconds_interval)
    
    # تحديث حالة المهمة عند الانتهاء
    await update_auto_creation_task(task_id, active=False)
    auto_creation_tasks[user_id] = False
    
    if runs >= 50:
        await bot.send_message(user_id, "✅ اكتملت 50 عمليات إنشاء تلقائية لهذا اليوم!")
    else:
        await bot.send_message(user_id, "⏹ توقف الإنشاء التلقائي حسب طلبك.")

async def mass_join(user_id, target):
    accounts = await get_stored_accounts(user_id)
    success_count = 0
    
    for account in accounts:
        session = account[1]
        try:
            client = TelegramClient(StringSession(session), API_ID, API_HASH)
            await client.connect()
            await client(JoinChannelRequest(channel=target))
            success_count += 1
        except Exception as e:
            logger.error(f"Error joining channel: {str(e)}")
        finally:
            if client and client.is_connected():
                await client.disconnect()
    
    return success_count

async def get_joined_channels_for_session(session_str, limit=100):
    client = None
    channels = []
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()

        async for dialog in client.iter_dialogs():
            if not dialog.is_channel or getattr(dialog.entity, "megagroup", False):
                continue

            entity = dialog.entity
            channels.append({
                "title": dialog.name or f"Channel {dialog.id}",
                "entity": entity,
                "id": getattr(entity, "id", dialog.id),
                "username": getattr(entity, "username", None),
            })
            if len(channels) >= limit:
                break

        return channels
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def leave_channel_with_session(session_str, entity):
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        await client(LeaveChannelRequest(channel=entity))
        return True
    except Exception as e:
        logger.error(f"Error leaving channel: {str(e)}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def leave_all_channels_with_session(session_str):
    client = None
    success_count = 0
    failed_count = 0
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()

        channels = []
        async for dialog in client.iter_dialogs():
            if dialog.is_channel and not getattr(dialog.entity, "megagroup", False):
                channels.append(dialog.entity)

        for entity in channels:
            try:
                await client(LeaveChannelRequest(channel=entity))
                success_count += 1
                await asyncio.sleep(1)
            except Exception as e:
                failed_count += 1
                logger.error(f"Error leaving channel: {str(e)}")

        return success_count, failed_count
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def leave_all_groups_with_session(session_str):
    client = None
    success_count = 0
    failed_count = 0
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()

        groups = []
        async for dialog in client.iter_dialogs():
            if dialog.is_group:
                groups.append(dialog.entity)

        for entity in groups:
            try:
                if getattr(entity, "megagroup", False):
                    await client(LeaveChannelRequest(channel=entity))
                else:
                    await client.delete_dialog(entity)
                success_count += 1
                await asyncio.sleep(1)
            except Exception as e:
                failed_count += 1
                logger.error(f"Error leaving group: {str(e)}")

        return success_count, failed_count
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def send_comment_to_post(client, entity, message_id, comment_text):
    """إرسال تعليق على منشور قناة عبر رسالة النقاش إن وجدت."""
    try:
        discussion = await client(GetDiscussionMessageRequest(peer=entity, msg_id=message_id))
        if discussion.messages:
            discussion_message = discussion.messages[0]
            await client.send_message(
                discussion_message.peer_id,
                comment_text,
                reply_to=discussion_message.id
            )
            return True
    except Exception as e:
        logger.warning(f"Could not resolve discussion message, trying direct reply: {str(e)}")

    await client.send_message(entity, comment_text, reply_to=message_id)
    return True

async def mass_comment(user_id, url, comment_text, count, accounts=None):
    """التعليق الجماعي باستخدام عدة حسابات"""
    accounts = accounts if accounts is not None else await get_stored_accounts(user_id)
    if not accounts or not 1 <= count <= len(accounts):
        return 0
    
    # استخراج معرف القناة ورقم الرسالة من الرابط
    try:
        if "t.me" not in url:
            return 0
            
        parts = url.split("/")
        if "c/" in url:  # رابط مشاركة (https://t.me/c/123456789/123)
            channel_id = int(parts[-2])
            if channel_id < 0:
                channel_id = -1000000000000 - channel_id
            else:
                channel_id = int(f"-100{channel_id}")
            message_id = int(parts[-1])
            entity = channel_id
        else:  # رابط مباشر (https://t.me/channel_name/123)
            channel_username = parts[-2]
            message_id = int(parts[-1])
            entity = channel_username
    except Exception as e:
        logger.error(f"Error parsing URL: {str(e)}")
        return 0
    
    success_count = 0
    for account in accounts[:count]:
        if not await service_account_authorized(user_id, account):
            continue
        session = account[1]
        if not await session_is_usable(session):
            continue
        client = None
        try:
            client = TelegramClient(StringSession(session), API_ID, API_HASH)
            await client.connect()
            if isinstance(entity, str):
                posting_entity = await resolve_posting_target(client, entity)
            else:
                posting_entity = entity
            
            if await send_comment_to_post(client, posting_entity, message_id, comment_text):
                success_count += 1
        except Exception as e:
            logger.error(f"Error commenting: {str(e)}")
        finally:
            if client and client.is_connected():
                await client.disconnect()
    
    return success_count

async def verify_and_add_super_channel(channel_id, title, user_id):
    accounts = await get_stored_accounts(user_id)
    all_joined = True
    
    for account in accounts:
        session = account[1]
        try:
            client = TelegramClient(StringSession(session), API_ID, API_HASH)
            await client.connect()
            
            if not await is_user_in_channel(client, int(session.split(":")[0]), channel_id):
                all_joined = False
                break
                
        except Exception as e:
            logger.error(f"Error checking membership: {str(e)}")
            all_joined = False
            break
        finally:
            if client and client.is_connected():
                await client.disconnect()
    
    if all_joined:
        await add_super_channel(user_id, channel_id, title)
        return True
    return False
# ===== دوال الرشق التلقائي =====

async def get_auto_engagement_settings():
    """إرجاع جميع إعدادات الرشق التلقائي النشطة."""
    return await execute_query(
        "SELECT * FROM auto_engagement WHERE is_active=1 AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)",
        fetchall=True
    )

async def get_engagement_channels():
    """يعيد القنوات المضافة للمراقبة لاستخدامها عند إنشاء خدمة رشق."""
    return await execute_query(
        "SELECT id, channel_id, channel_name FROM engagement_channels ORDER BY id",
        fetchall=True,
    )

async def add_monitored_engagement_channel(channel_id, channel_name, created_by):
    await execute_query(
        """INSERT INTO engagement_channels (channel_id, channel_name, created_by)
           VALUES (?, ?, ?)
           ON CONFLICT (channel_id) DO UPDATE SET channel_name=excluded.channel_name""",
        (channel_id, channel_name, created_by),
        commit=True,
    )


async def delete_monitored_engagement_channel(channel_row_id):
    """Delete a monitored channel and every scheduled job attached to it."""
    await execute_query(
        """WITH removed AS (
               DELETE FROM engagement_channels WHERE id=? RETURNING channel_id
           )
           DELETE FROM auto_engagement
           WHERE channel_id IN (SELECT channel_id FROM removed)""",
        (channel_row_id,),
        commit=True,
    )

async def add_auto_engagement(
    channel_id, engagement_type, emoji=None, vote_option=None, comment_text=None,
    created_by=None, period=None, expires_at=None, account_count=None,
):
    if engagement_type == "views_react":
        await execute_query(
            """DELETE FROM auto_engagement
               WHERE channel_id=? AND engagement_type='views_react' AND created_by=?""",
            (channel_id, created_by),
            commit=True,
        )
    await execute_query(
        """INSERT INTO auto_engagement 
           (channel_id, engagement_type, emoji, vote_option, comment_text, created_by, period, expires_at, account_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (channel_id, engagement_type, emoji, vote_option, comment_text, created_by, period, expires_at, account_count),
        commit=True
    )


async def get_user_sessions_for_engagement(user_id, account_count=None):
    """Return distinct sessions owned by the user, limited by their chosen count."""
    accounts = await get_stored_accounts(user_id)
    sessions = []
    seen = set()
    for account in accounts or []:
        session = account[1]
        if session and session not in seen:
            seen.add(session)
            sessions.append(session)
    if account_count is not None:
        sessions = sessions[:max(0, int(account_count))]
    return sessions

async def delete_auto_engagement(id):
    await execute_query("DELETE FROM auto_engagement WHERE id=?", (id,), commit=True)

async def toggle_auto_engagement(id, active):
    await execute_query(
        "UPDATE auto_engagement SET is_active=? WHERE id=?",
        (bool(active), id),
        commit=True
    )

async def update_last_processed_msg_id(id, msg_id):
    await execute_query(
        "UPDATE auto_engagement SET last_processed_msg_id=? WHERE id=?",
        (msg_id, id),
        commit=True
    )

async def get_all_bot_accounts_for_engagement(actor=None):
    """Return the complete deduplicated administrative account pool."""
    return [account[1] for account in await get_all_bot_service_accounts(actor)]

async def execute_engagement_on_accounts(engagement_type, channel_target, msg_id, sessions, **kwargs):
    """
    تنفيذ نوع الرشق على جميع الحسابات.
    engagement_type: 'react', 'vote', 'comment', 'views'
    sessions: قائمة الجلسات
    kwargs: تحتوي على emoji, vote_option, comment_text حسب النوع
    """
    success = 0
    reaction_offset = random.randrange(1000)
    for account_index, session in enumerate(sessions):
        actor=kwargs.get('actor_id')
        if actor is None:
            continue
        from account_access import accounts as permitted_accounts
        selected=next((a for a in await permitted_accounts(actor) if a[1]==session),None)
        if not selected or not await service_account_authorized(actor,selected):
            continue
        try:
            if engagement_type == 'views_react':
                if await view_and_react_from_session(
                    session,
                    channel_target,
                    msg_id,
                    reaction_index=reaction_offset + account_index,
                ):
                    success += 1
            elif engagement_type == 'react':
                emoji = kwargs.get('emoji', '❤️')
                if await react_to_message_by_entity(session, channel_target, msg_id, emoji):
                    success += 1
            elif engagement_type == 'vote':
                option = kwargs.get('vote_option', 1)
                if await vote_in_poll_by_entity(session, channel_target, msg_id, option):
                    success += 1
            elif engagement_type == 'comment':
                comment = kwargs.get('comment_text', '')
                if comment:
                    # سنستخدم دالة mass_comment لكن بتعديل بسيط لتقبل entity و msg_id مباشرة
                    if await send_comment_to_post_from_session(session, channel_target, msg_id, comment):
                        success += 1
            elif engagement_type == 'views':
                if await increment_views_count_from_session(session, channel_target, msg_id):
                    success += 1
            await asyncio.sleep(0.5)  # تأخير بين كل حساب لتجنب الحظر
        except Exception as e:
            logger.error(f"خطأ أثناء الرشق للحساب: {e}")
    return success


def _auto_engagement_target_matches(event, configured_target):
    """يتحقق من تطابق القناة المهيأة مع القناة التي وصل منها المنشور."""
    target = normalize_super_target(configured_target).lstrip("@").lower()
    chat = event.chat
    candidates = {str(event.chat_id), str(getattr(chat, "id", ""))}
    username = getattr(chat, "username", None)
    if username:
        candidates.update({username.lower(), f"@{username.lower()}"})
    return target in {candidate.lower() for candidate in candidates if candidate}


async def _run_auto_engagement_for_post(event):
    """ينفّذ إعدادات الرشق المفعّلة مرة واحدة للمنشور الوارد."""
    try:
        settings = await get_auto_engagement_settings()
        if not settings:
            return

        sessions_by_count = {}
        for row in settings:
            (
                setting_id, channel_id, engagement_type, emoji, vote_option,
                comment_text, _, created_by, _, last_msg_id, _, _, account_count,
            ) = row
            if last_msg_id == event.message.id or not _auto_engagement_target_matches(event, channel_id):
                continue

            # الرشق الدوري إداري ويعمل من المخزون العام بعد إزالة التكرار.
            from account_access import is_manager
            if not is_manager(created_by):
                continue
            selected_count = max(0, int(account_count or 0))
            if selected_count not in sessions_by_count:
                all_sessions = await get_all_bot_accounts_for_engagement(created_by)
                sessions_by_count[selected_count] = all_sessions[:selected_count]
            sessions = sessions_by_count[selected_count]
            if sessions:
                await execute_engagement_on_accounts(
                    engagement_type,
                    channel_id,
                    event.message.id,
                    sessions,
                    emoji=emoji,
                    vote_option=vote_option,
                    comment_text=comment_text,actor_id=created_by,
                )
            # حتى في حال عدم وجود جلسات، لا نعيد معالجة نفس المنشور لاحقاً.
            await update_last_processed_msg_id(setting_id, event.message.id)
    except Exception:
        logger.exception("Automatic engagement failed for message %s", event.message.id)
    finally:
        _auto_engagement_in_progress.discard((event.chat_id, event.message.id))


def register_auto_engagement_listener(client):
    """يسجّل مستمعاً يعالج أي منشور قناة جديد مطابق للإعدادات المفعّلة."""
    async def on_new_channel_post(event):
        if not event.message.post:
            return
        key = (event.chat_id, event.message.id)
        if key in _auto_engagement_in_progress:
            return
        _auto_engagement_in_progress.add(key)
        asyncio.create_task(_run_auto_engagement_for_post(event))

    client.add_event_handler(on_new_channel_post, events.NewMessage(incoming=True))

# نسخ معدلة من الدوال الأصلية لتحلّ القناة داخل جلسة كل حساب.
async def _get_allowed_channel_reaction(client, entity, msg_id, reaction_index=None):
    """Choose a Telegram reaction object allowed by this channel."""
    full = await client(functions.channels.GetFullChannelRequest(channel=entity))
    allowed = getattr(full.full_chat, "available_reactions", None)
    kind = type(allowed).__name__
    if kind == "ChatReactionsNone":
        return None
    if kind == "ChatReactionsSome":
        reactions = list(getattr(allowed, "reactions", None) or [])
        if not reactions:
            return None
        return reactions[reaction_index % len(reactions)] if reaction_index is not None else random.choice(reactions)

    # For channels allowing all reactions, prefer one already used on the post.
    message = await client.get_messages(entity, ids=msg_id)
    results = getattr(getattr(message, "reactions", None), "results", None) or []
    existing = [getattr(item, "reaction", None) for item in results]
    existing = [reaction for reaction in existing if reaction is not None]
    if existing:
        return existing[reaction_index % len(existing)] if reaction_index is not None else random.choice(existing)

    available = await client(functions.messages.GetAvailableReactionsRequest(hash=0))
    choices = [
        types.ReactionEmoji(emoticon=item.reaction)
        for item in (getattr(available, "reactions", None) or [])
        if getattr(item, "reaction", None)
    ]
    if not choices:
        return None
    return choices[reaction_index % len(choices)] if reaction_index is not None else random.choice(choices)


async def view_and_react_from_session(session_str, channel_target, msg_id, reaction_index=None):
    """Increment the view and use one of the channel's allowed reactions."""
    if not await session_is_usable(session_str):
        return False
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        entity = await resolve_posting_target(client, channel_target)
        await client(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
        reaction = await _get_allowed_channel_reaction(
            client, entity, msg_id, reaction_index=reaction_index,
        )
        if reaction is not None:
            await client(functions.messages.SendReactionRequest(
                peer=entity, msg_id=msg_id, reaction=[reaction], big=False,
            ))
        return True
    except Exception as exc:
        logger.error("خطأ في المشاهدة والتفاعل التلقائي: %s", exc)
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()


async def react_to_message_by_entity(session_str, channel_target, msg_id, emoji):
    """يتفاعل بعد حل القناة باستخدام جلسة الحساب الحالية."""
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        entity = await resolve_posting_target(client, channel_target)
        await client(functions.messages.SendReactionRequest(
            peer=entity,
            msg_id=msg_id,
            reaction=[types.ReactionEmoji(emoticon=emoji)],
            big=True
        ))
        return True
    except Exception as e:
        logger.error(f"خطأ في التفاعل: {e}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def vote_in_poll_by_entity(session_str, channel_target, msg_id, option_number):
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        entity = await resolve_posting_target(client, channel_target)
        await client(functions.messages.SendVoteRequest(
            peer=entity,
            msg_id=msg_id,
            options=[str(option_number - 1)]
        ))
        return True
    except Exception as e:
        logger.error(f"خطأ في التصويت: {e}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def send_comment_to_post_from_session(session_str, channel_target, msg_id, comment_text):
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        entity = await resolve_posting_target(client, channel_target)
        await client.send_message(entity, comment_text, reply_to=msg_id)
        return True
    except Exception as e:
        logger.error(f"خطأ في التعليق: {e}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def increment_views_count_from_session(session_str, channel_target, msg_id):
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        entity = await resolve_posting_target(client, channel_target)
        await client(GetMessagesViewsRequest(
            peer=entity,
            id=[msg_id],
            increment=True
        ))
        return True
    except Exception as e:
        logger.error(f"خطأ في زيادة المشاهدات: {e}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()
def set_global_bot(client):
    """تعيين كائن البوت العام للمعالجات"""
    global bot
    bot = client
    try:
        import handlers
        handlers.bot = client
    except ImportError:
        pass
    try:
        import config
        config.bot = client
    except ImportError:
        pass
    print("✅ تم تعيين كائن البوت العالمي")
