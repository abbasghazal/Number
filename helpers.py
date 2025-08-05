import re
import asyncio
import random
import datetime
import aiosqlite
import logging
from datetime import timedelta
from config import DB_PATH, API_ID, API_HASH, GROUP_NAMES
from telethon.sessions import StringSession
from telethon import TelegramClient
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest, UpdateProfileRequest
from telethon.tl.functions.channels import JoinChannelRequest, CreateChannelRequest, GetParticipantRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, SendMessageRequest, EditChatDefaultBannedRightsRequest
from telethon.tl.types import ChatBannedRights, InputPeerChannel
from telethon.errors import SessionPasswordNeededError, ChannelPrivateError, UserNotParticipantError
from session_converter import MangSession

# إعداد نظام تسجيل الأخطاء
logger = logging.getLogger(__name__)

async def execute_query(query, args=(), fetchone=False, fetchall=False, commit=False):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.cursor()
            await cursor.execute(query, args)
            
            if commit:
                await db.commit()
            
            if fetchone:
                return await cursor.fetchone()
            if fetchall:
                return await cursor.fetchall()
            return None
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
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
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
        await client(JoinChannelRequest(channel=mandatory_channel))
        return True
    except Exception as e:
        logger.error(f"Error joining mandatory channel: {str(e)}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def create_private_group(session):
    client = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        
        group_title = random.choice(GROUP_NAMES)
        
        result = await client(CreateChannelRequest(
            title=group_title,
            about="مجموعة تم إنشاؤها بواسطة بوت شهم",
            megagroup=True
        ))
        
        channel = result.chats[0]
        chat_id = channel.id
        chat_entity = InputPeerChannel(chat_id, channel.access_hash)
        
        banned_rights = ChatBannedRights(
            until_date=None,
            view_messages=False,
            send_messages=False,
            send_media=False,
            send_stickers=False,
            send_gifs=False,
            send_games=False,
            send_inline=False,
            embed_links=False,
            send_polls=False,
            change_info=False,
            invite_users=False,
            pin_messages=False
        )
        
        await client(EditChatDefaultBannedRightsRequest(
            peer=chat_entity,
            banned_rights=banned_rights
        ))
        
        msg1 = await client(SendMessageRequest(
            peer=chat_entity,
            message="تم انشاء هذه المجموعة بواسطة بوت شهم التلقائي",
            no_webpage=True
        ))
        
        msg2 = await client(SendMessageRequest(
            peer=chat_entity,
            message=f"مرحبا بكم في {group_title}",
            no_webpage=True
        ))
        
        invite_link = await client(ExportChatInviteRequest(peer=chat_entity))
        
        participants_count = 1
        
        return chat_id, invite_link.link, participants_count
    except Exception as e:
        logger.error(f"Error creating group: {str(e)}")
        return None, None, 0
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

async def get_countries(only_with_accounts=False):
    if only_with_accounts:
        return await execute_query(
            """SELECT c.* FROM countries c 
            WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.calling_code = c.calling_code)""",
            (),
            fetchall=True
        )
    return await execute_query("SELECT * FROM countries", (), fetchall=True)
async def search_accounts(query):
    # البحث عن رقم هاتف
    phone_accounts = await execute_query(
        "SELECT * FROM accounts WHERE phone LIKE ?",
        (f"%{query}%",),
        fetchall=True
    )
    
    # البحث عن دول
    country_accounts = []
    country = await get_country(query)
    if country:
        country_accounts = await get_accounts(country[0])
    
    return phone_accounts + country_accounts

async def add_new_country(name, calling_code):
    await execute_query(
        "INSERT OR IGNORE INTO countries (name, calling_code) VALUES (?, ?)",
        (name, calling_code),
        commit=True
    )
    
async def get_country(calling_code):
    return await execute_query(
        "SELECT * FROM countries WHERE calling_code=?",
        (calling_code,),
        fetchone=True
    )

async def add_new_country(name, calling_code, price, sell_price):
    await execute_query(
        "INSERT OR REPLACE INTO countries (name, calling_code, price, sell_price) VALUES (?, ?, ?, ?)",
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
    return await execute_query(
        "SELECT * FROM accounts WHERE calling_code=? AND is_sold=0",
        (calling_code,),
        fetchall=True
    )

async def add_account(phone, session, calling_code, twofa='لا يوجد', seller_id=0):
    await execute_query(
        "INSERT OR REPLACE INTO accounts (phone, session, calling_code, twofa, seller_id) VALUES (?, ?, ?, ?, ?)",
        (phone, session, calling_code, twofa, seller_id),
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
        (user_id, phone, calling_code, name, price, session, twofa),
        commit=True
    )

async def get_pending_purchase(user_id, phone):
    return await execute_query(
        "SELECT * FROM pending_purchases WHERE user_id=? AND phone=?",
        (user_id, phone),
        fetchone=True
    )

async def delete_pending_purchase(user_id, phone):
    await execute_query(
        "DELETE FROM pending_purchases WHERE user_id=? AND phone=?",
        (user_id, phone),
        commit=True
    )

async def add_stored_account(phone, session, user_id):
    await execute_query(
        "INSERT OR REPLACE INTO stored_accounts (phone, session, user_id) VALUES (?, ?, ?)",
        (phone, session, user_id),
        commit=True
    )

async def get_stored_accounts(user_id):
    return await execute_query(
        "SELECT * FROM stored_accounts WHERE user_id=?",
        (user_id,),
        fetchall=True
    )

async def delete_stored_account(phone):
    await execute_query(
        "DELETE FROM stored_accounts WHERE phone=?",
        (phone,),
        commit=True
    )

async def add_sold_account(phone, session, user_id):
    await execute_query(
        "INSERT OR REPLACE INTO sold_accounts (phone, session, user_id) VALUES (?, ?, ?)",
        (phone, session, user_id),
        commit=True
    )

async def get_sold_accounts(user_id):
    return await execute_query(
        "SELECT * FROM sold_accounts WHERE user_id=?",
        (user_id,),
        fetchall=True
    )

async def add_purchased_account(phone, session, user_id):
    await execute_query(
        "INSERT OR REPLACE INTO purchased_accounts (phone, session, user_id) VALUES (?, ?, ?)",
        (phone, session, user_id),
        commit=True
    )

async def get_purchased_accounts(user_id):
    return await execute_query(
        "SELECT * FROM purchased_accounts WHERE user_id=?",
        (user_id,),
        fetchall=True
    )

async def add_active_account(phone, session, user_id, activity):
    await execute_query(
        "INSERT OR REPLACE INTO active_accounts (phone, session, user_id, activity) VALUES (?, ?, ?, ?)",
        (phone, session, user_id, activity),
        commit=True
    )

async def get_active_accounts(user_id):
    return await execute_query(
        "SELECT * FROM active_accounts WHERE user_id=?",
        (user_id,),
        fetchall=True
    )

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
    await execute_query(
        "INSERT OR REPLACE INTO super_channels (channel_id, title) VALUES (?, ?)",
        (channel_id, title),
        commit=True
    )

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
        "INSERT OR REPLACE INTO timed_names (user_id, active) VALUES (?, ?)",
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
        "INSERT OR REPLACE INTO pending_sales (phone, seller_id, price, calling_code, session, twofa) VALUES (?, ?, ?, ?, ?, ?)",
        (phone, seller_id, price, calling_code, session, twofa),
        commit=True
    )

async def get_pending_sale(phone):
    return await execute_query(
        "SELECT * FROM pending_sales WHERE phone=?",
        (phone,),
        fetchone=True
    )

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

async def get_code(session):
    try:
        X = TelegramClient(StringSession(session), api_id=API_ID, api_hash=API_HASH)
        await X.connect()
        async for x in X.iter_messages(777000, limit=1):
            code_match = re.search(r'\b(\d{5})\b', x.text)
            if code_match:
                return code_match.group(1)
            else:
                return "لم يتم العثور"
    except Exception as a:
        return f"Error: {str(a)}"
    finally:
        if 'X' in locals() and X.is_connected():
            await X.disconnect()

async def count_ses(session):
    client = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        auths = await client(GetAuthorizationsRequest())
        return len(auths.authorizations)
    except Exception as e:
        print(f"Session count error: {str(e)}")
        return 0
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def logout_all_sessions(session):
    client = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        auths = await client(GetAuthorizationsRequest())
        
        # تسجيل الخروج من جميع الجلسات ما عدا الجلسة الحالية
        for auth in auths.authorizations:
            if auth.current:
                continue
            try:
                await client(ResetAuthorizationRequest(hash=auth.hash))
            except Exception as e:
                print(f"Failed to logout session {auth.hash}: {str(e)}")
        
        return True
    except Exception as e:
        print(f"Logout all sessions error: {str(e)}")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def join_mandatory_channel(session):
    mandatory_channel = await get_setting("mandatory_channel")
    if not mandatory_channel:
        return True
    
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        await client(JoinChannelRequest(channel=mandatory_channel))
        return True
    except Exception as e:
        print(f"Error joining mandatory channel: {str(e)}")
        return False
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
    
    while repetitions > 0:
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()
            
            # اختيار قالب عشوائي
            template = random.choice(templates)
            
            if all_channels:
                # النشر في جميع القنوات
                for channel in super_channels:
                    try:
                        await client.send_message(int(channel[0]), template[1])
                        await asyncio.sleep(1)  # تأخير بسيط بين الرسائل
                    except Exception as e:
                        print(f"Error posting to {channel[0]}: {str(e)}")
            else:
                # النشر في قناة محددة
                if channel_id:
                    try:
                        await client.send_message(int(channel_id), template[1])
                    except Exception as e:
                        print(f"Error posting to {channel_id}: {str(e)}")
            
            await client.disconnect()
            
            repetitions -= 1
            await bot.send_message(
                user_id,
                f"📢 تم النشر بنجاح باستخدام الحساب: +{phone}\n"
                f"🔄 التكرارات المتبقية: {repetitions}"
            )
            
            if repetitions > 0:
                await asyncio.sleep(interval)
                
        except Exception as e:
            print(f"Error in posting task: {str(e)}")
            await asyncio.sleep(interval)
    
    await bot.send_message(user_id, "✅ اكتملت مهمة النشر التلقائي!")
    # تنظيف الذاكرة
    if user_id in user_posting_accounts:
        del user_posting_accounts[user_id]

async def create_groups(bot,user_id, count, manual=False):
    # الحصول على الحسابات المخزنة للمستخدم
    stored_accounts = await execute_query(
        "SELECT * FROM stored_accounts WHERE user_id=?",
        (user_id,),
        fetchall=True
    )
    
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

# إضافة هذه الدالة في نهاية الملف
async def count_ses(session_str):
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        auths = await client(GetAuthorizationsRequest())
        count = len(auths.authorizations)
        await client.disconnect()
        return count
    except Exception as e:
        logger.error(f"Session count error: {str(e)}")
        return 0
def set_global_bot(client):
    """تعيين كائن البوت العام للمعالجات"""
    from handlers import bot as global_bot
    global_bot = client
    print("✅ تم تعيين كائن البوت العالمي")