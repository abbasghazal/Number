import re
import asyncio
import random
import datetime
import logging
import sqlite3
import secrets
import string
from datetime import timedelta
from config import DB_PATH, API_ID, API_HASH, GROUP_NAMES, SESSION_ENCRYPTION_KEY, manual_creation_tasks, auto_creation_tasks
from messages import msgs
from telethon.sessions import StringSession
from telethon import TelegramClient
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest, UpdateProfileRequest
from telethon.tl.functions.channels import JoinChannelRequest, CreateChannelRequest, GetParticipantRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, SendMessageRequest, EditChatDefaultBannedRightsRequest, GetMessagesViewsRequest, GetDiscussionMessageRequest, ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.types import ChatBannedRights, InputPeerChannel
from telethon.errors import SessionPasswordNeededError, ChannelPrivateError, UserNotParticipantError, UserAlreadyParticipantError
from telethon.tl import functions, types
from session_converter import MangSession
from cryptography.fernet import Fernet, InvalidToken

# إعداد نظام تسجيل الأخطاء
logger = logging.getLogger(__name__)
bot = None
user_posting_accounts = {}

_SESSION_PREFIX = "enc:"

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

def _decrypt_session_row(row, index=1):
    if row is None:
        return None
    return _replace_row_value(row, index, decrypt_session(row[index]))

def _decrypt_session_rows(rows, index=1):
    if not rows:
        return rows
    return [_decrypt_session_row(row, index) for row in rows]

async def execute_query(query, args=(), fetchone=False, fetchall=False, commit=False):
    try:
        with sqlite3.connect(DB_PATH) as db:
            db.execute("PRAGMA foreign_keys = ON")
            cursor = db.cursor()
            cursor.execute(query, args)

            result = None
            if fetchone:
                result = cursor.fetchone()
            elif fetchall:
                result = cursor.fetchall()

            if commit:
                db.commit()

            return result
    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        return None

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
        "SELECT * FROM users WHERE user_id=?",
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
            'join_date': result[5],
            'last_active': result[6]
        }
    return None

async def create_user(user_id):
    await execute_query(
        "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
        (user_id,),
        commit=True
    )

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
    await execute_query(
        """
        UPDATE licenses
        SET used_by=?, used_at=?, expires_at=?, is_active=0
        WHERE license_key=?
        """,
        (user_id, format_db_datetime(now), format_db_datetime(expires_at), license_key),
        commit=True
    )
    await execute_query(
        """
        INSERT INTO user_licenses (
            user_id, license_key, activated_at, expires_at, is_valid,
            reminded_3d, reminded_1d, expired_notified
        )
        VALUES (?, ?, ?, ?, 1, 0, 0, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            license_key=excluded.license_key,
            activated_at=excluded.activated_at,
            expires_at=excluded.expires_at,
            is_valid=1,
            reminded_3d=0,
            reminded_1d=0,
            expired_notified=0
        """,
        (user_id, license_key, format_db_datetime(now), format_db_datetime(expires_at)),
        commit=True
    )
    return True, expires_at

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
    return _decrypt_session_row(row)

async def get_stored_account_info(phone):
    row = await execute_query(
        "SELECT phone, session, user_id, twofa, storage_date FROM stored_accounts WHERE phone=?",
        (phone,),
        fetchone=True
    )
    return _decrypt_session_row(row)

async def react_to_message(session_str, url, emoji):
    """التفاعل مع منشور بإيموجي محدد"""
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
    if not accounts or len(accounts) < count:
        return 0
    
    success = 0
    for account in accounts[:count]:
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
    if not accounts or len(accounts) < count:
        return 0
    
    success = 0
    for account in accounts[:count]:
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
    if not accounts or len(accounts) < count:
        return 0
    
    success = 0
    for account in accounts[:count]:
        try:
            if await increment_views_count(account[1], url):
                success += 1
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error in increment views: {str(e)}")
    return success

async def update_user_coins(user_id, coins):
    await execute_query(
        "UPDATE users SET coins=? WHERE user_id=?",
        (coins, user_id),
        commit=True
    )

async def is_user_in_channel(client, user_id, channel):
    try:
        await client(GetParticipantRequest(channel=channel, participant=user_id))
        return True
    except (ChannelPrivateError, UserNotParticipantError):
        return False
    except Exception as e:
        logger.error(f"Error checking channel membership: {str(e)}")
        return False

async def logout_all_sessions(session_str):
    client = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        auths = await client(GetAuthorizationsRequest())
        
        for auth in auths.authorizations:
            if auth.current:
                continue
            try:
                await client(ResetAuthorizationRequest(hash=auth.hash))
            except Exception as e:
                logger.error(f"Failed to logout session: {str(e)}")
        
        return True
    except Exception as e:
        logger.error(f"Logout error: {str(e)}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def join_mandatory_channel(session):
    mandatory_channel = await get_setting("mandatory_channel")
    if not mandatory_channel:
        return True
    
    client = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        channel_entity = await client.get_input_entity(mandatory_channel)
        await client(JoinChannelRequest(channel=channel_entity))
        await asyncio.sleep(2)
        return True
    except Exception as e:
        logger.error(f"Error joining mandatory channel: {str(e)}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

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
        "SELECT * FROM countries",
        (),
        fetchall=True
    )

async def get_country(calling_code):
    return await execute_query(
        "SELECT * FROM countries WHERE calling_code=?",
        (calling_code,),
        fetchone=True
    )

async def add_new_country(name, calling_code, price, sell_price):
    await execute_query(
        """
        INSERT INTO countries (name, calling_code, price, sell_price) VALUES (?, ?, ?, ?)
        ON CONFLICT(calling_code) DO UPDATE SET
            name=excluded.name,
            price=excluded.price,
            sell_price=excluded.sell_price,
            is_active=1
        """,
        (name, calling_code, price, sell_price),
        commit=True
    )

async def delete_country(calling_code):
    await execute_query(
        "DELETE FROM countries WHERE calling_code=?",
        (calling_code,),
        commit=True
    )

async def get_accounts(calling_code):
    rows = await execute_query(
        "SELECT * FROM accounts WHERE calling_code=?",
        (calling_code,),
        fetchall=True
    )
    return _decrypt_session_rows(rows)

async def get_all_accounts():
    rows = await execute_query(
        "SELECT * FROM accounts",
        (),
        fetchall=True
    )
    return _decrypt_session_rows(rows)

async def get_account_info(phone):
    row = await execute_query(
        "SELECT * FROM accounts WHERE phone=?",
        (phone,),
        fetchone=True
    )
    return _decrypt_session_row(row)

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
        (phone, encrypt_session(session), calling_code, twofa, seller_id),
        commit=True
    )

async def delete_account(phone):
    await execute_query(
        "DELETE FROM accounts WHERE phone=?",
        (phone,),
        commit=True
    )

async def add_pending_purchase(user_id, phone, calling_code, name, price, session, twofa):
    await execute_query(
        "INSERT INTO pending_purchases (user_id, phone, calling_code, name, price, session, twofa) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, phone, calling_code, name, price, encrypt_session(session), twofa),
        commit=True
    )

async def get_pending_purchase(user_id, phone):
    row = await execute_query(
        "SELECT * FROM pending_purchases WHERE user_id=? AND phone=?",
        (user_id, phone),
        fetchone=True
    )
    return _decrypt_session_row(row, 6)

async def delete_pending_purchase(user_id, phone):
    await execute_query(
        "DELETE FROM pending_purchases WHERE user_id=? AND phone=?",
        (user_id, phone),
        commit=True
    )

async def add_stored_account(phone, session, user_id, twofa='لا يوجد'):
    await execute_query(
        """
        INSERT INTO stored_accounts (phone, session, user_id, twofa) VALUES (?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            session=excluded.session,
            user_id=excluded.user_id,
            twofa=CASE
                WHEN excluded.twofa IS NULL OR excluded.twofa='' OR excluded.twofa='لا يوجد'
                THEN stored_accounts.twofa
                ELSE excluded.twofa
            END
        """,
        (phone, encrypt_session(session), user_id, twofa),
        commit=True
    )

async def get_stored_accounts(user_id):
    rows = await execute_query(
        "SELECT phone, session, user_id, twofa, storage_date FROM stored_accounts WHERE user_id=?",
        (user_id,),
        fetchall=True
    )
    return _decrypt_session_rows(rows)

async def get_all_stored_accounts():
    rows = await execute_query(
        "SELECT phone, session, user_id, twofa, storage_date FROM stored_accounts",
        (),
        fetchall=True
    )
    return _decrypt_session_rows(rows)

async def delete_stored_account(phone):
    await execute_query(
        "DELETE FROM stored_accounts WHERE phone=?",
        (phone,),
        commit=True
    )

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
        (phone, encrypt_session(session), user_id, seller_id, calling_code, name, price, twofa),
        commit=True
    )

async def get_purchased_accounts(user_id):
    rows = await execute_query(
        "SELECT * FROM purchased_accounts WHERE user_id=?",
        (user_id,),
        fetchall=True
    )
    return _decrypt_session_rows(rows)

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

async def add_super_channel(channel_id, title):
    channel_id = normalize_super_target(channel_id)
    await execute_query(
        """
        INSERT INTO super_channels (channel_id, title) VALUES (?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title, is_active=1
        """,
        (channel_id, title),
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

async def send_post_to_super_target(client, target, message):
    entity = await resolve_posting_target(client, target)
    await client.send_message(entity, message)

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

async def get_super_channels():
    return await execute_query(
        "SELECT * FROM super_channels",
        (),
        fetchall=True
    )

async def delete_super_channel(channel_id):
    await execute_query(
        "DELETE FROM super_channels WHERE channel_id=?",
        (channel_id,),
        commit=True
    )

async def add_posting_template(template_text):
    await execute_query(
        "INSERT INTO posting_templates (template_text) VALUES (?)",
        (template_text,),
        commit=True
    )

async def get_posting_templates():
    return await execute_query(
        "SELECT * FROM posting_templates",
        (),
        fetchall=True
    )

async def delete_posting_template(template_id):
    await execute_query(
        "DELETE FROM posting_templates WHERE template_id=?",
        (template_id,),
        commit=True
    )

async def update_posting_template(template_id, new_text):
    await execute_query(
        "UPDATE posting_templates SET template_text=? WHERE template_id=?",
        (new_text, template_id),
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
        (phone, seller_id, price, calling_code, encrypt_session(session), twofa),
        commit=True
    )

async def get_pending_sale(phone):
    row = await execute_query(
        "SELECT * FROM pending_sales WHERE phone=?",
        (phone,),
        fetchone=True
    )
    return _decrypt_session_row(row, 4)

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

async def create_active_posting_task(user_id, phone, interval, repetitions, all_channels=False, channel_id=None):
    await execute_query(
        """
        INSERT INTO active_posting_tasks
            (user_id, phone, channel_id, all_channels, seconds_interval, repetitions, remaining, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (user_id, phone, str(channel_id) if channel_id is not None else None, int(all_channels), interval, repetitions, repetitions),
        commit=True
    )

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
        return 0
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

async def run_auto_creation(user_id, interval_seconds, duration_hours):
    """
    تشغيل عملية الإنشاء التلقائي في الخلفية
    """
    end_time = datetime.datetime.now() + timedelta(hours=duration_hours)
    stored_accounts = await get_stored_accounts(user_id)
    
    if not stored_accounts:
        await bot.send_message(user_id, "❌ لا توجد حسابات مخزنة لاستخدامها في الإنشاء!")
        return
    
    while datetime.datetime.now() < end_time and auto_creation_tasks.get(user_id, {}).get('active', False):
        try:
            # اختيار حساب عشوائي
            account = random.choice(stored_accounts)
            group_id, invite_link, _ = await create_private_group(account[1])
            
            if group_id:
                auto_creation_tasks[user_id]['total_created'] += 1
                
                await bot.send_message(
                    user_id,
                    f"✅ تم إنشاء مجموعة تلقائية #{auto_creation_tasks[user_id]['total_created']}\n"
                    f"🆔: {group_id}\n"
                    f"🔗: {invite_link}"
                )
            
            await asyncio.sleep(interval_seconds)
            
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

async def run_posting_task(user_id, phone, interval, repetitions, all_channels=False, channel_id=None):
    await create_active_posting_task(user_id, phone, interval, repetitions, all_channels, channel_id)
    # الحصول على الحساب المحدد
    stored_accounts = await get_stored_accounts(user_id)
    account = next((acc for acc in stored_accounts if acc[0] == phone), None)
    
    if not account:
        await bot.send_message(user_id, "❌ الحساب المحدد لم يعد متاحاً!")
        return
    
    # الحصول على القوالب والقنوات
    templates = await get_posting_templates()
    super_channels = await get_super_channels()
    
    if not templates or not super_channels:
        await bot.send_message(user_id, "❌ توقف النشر بسبب نقص البيانات!")
        return
    
    # إعداد الجلسة
    session_str = account[1]
    progress_message = None

    async def update_progress_message(text):
        nonlocal progress_message
        if progress_message:
            try:
                await progress_message.edit(text)
                return
            except Exception as e:
                logger.error(f"Error editing posting progress message: {str(e)}")
        progress_message = await bot.send_message(user_id, text)
    
    try:
        while repetitions > 0:
            if not await is_posting_task_active(user_id, channel_id, all_channels):
                await update_progress_message("⏹ تم إيقاف مهمة النشر.")
                break

            client = None
            try:
                client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
                await client.connect()
                
                # اختيار قالب عشوائي
                template = random.choice(templates)
                success_count = 0
                failed_count = 0
                
                if all_channels:
                    # النشر في جميع القنوات
                    for channel in super_channels:
                        if not await is_posting_task_active(user_id, channel_id, all_channels):
                            break
                        try:
                            await send_post_to_super_target(client, channel[0], template[1])
                            success_count += 1
                            await asyncio.sleep(1)  # تأخير بسيط بين الرسائل
                        except Exception as e:
                            failed_count += 1
                            logger.error(f"Error posting to {channel[0]}: {str(e)}")
                else:
                    # النشر في قناة محددة
                    if channel_id:
                        try:
                            await send_post_to_super_target(client, channel_id, template[1])
                            success_count += 1
                        except Exception as e:
                            failed_count += 1
                            logger.error(f"Error posting to {channel_id}: {str(e)}")
                
                repetitions -= 1
                if success_count:
                    await update_progress_message(
                        f"📢 تم النشر باستخدام الحساب: +{phone}\n"
                        f"✅ الناجح: {success_count}\n"
                        f"❌ الفاشل: {failed_count}\n"
                        f"🔄 التكرارات المتبقية: {repetitions}"
                    )
                else:
                    await update_progress_message(
                        f"❌ لم يتم النشر في أي مجموعة بهذه المحاولة.\n"
                        f"🔄 التكرارات المتبقية: {repetitions}"
                    )
                
                if repetitions > 0:
                    await asyncio.sleep(interval)
                    
            except Exception as e:
                print(f"Error in posting task: {str(e)}")
                await asyncio.sleep(interval)
            finally:
                if client and client.is_connected():
                    await client.disconnect()
        else:
            await update_progress_message("✅ اكتملت مهمة النشر التلقائي!")
    finally:
        # تنظيف الذاكرة
        if user_id in user_posting_accounts:
            del user_posting_accounts[user_id]
        await execute_query(
            """
            UPDATE active_posting_tasks SET active=0
            WHERE user_id=? AND phone=? AND COALESCE(channel_id, '')=COALESCE(?, '')
            """,
            (user_id, phone, str(channel_id) if channel_id is not None else None),
            commit=True
        )
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
    if not accounts or len(accounts) < count:
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
        session = account[1]
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
        await execute_query(
            """
            INSERT INTO super_channels (channel_id, title) VALUES (?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title, is_active=1
            """,
            (channel_id, title),
            commit=True
        )
        return True
    return False

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
