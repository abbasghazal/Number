import re
import asyncio
import random
import datetime
import logging
import shutil
import os
from datetime import timedelta
import aiosqlite
from telethon import events, Button, functions, types
from telethon.errors import SessionPasswordNeededError, ChannelPrivateError, UserNotParticipantError
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest, UpdateProfileRequest
from telethon.tl.functions.channels import JoinChannelRequest, CreateChannelRequest, GetParticipantRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, SendMessageRequest, EditChatDefaultBannedRightsRequest
from telethon.tl.types import ChatBannedRights, InputPeerChannel
from config import API_ID, API_HASH, ADMIN_ID, TOKEN, DB_PATH, GROUP_NAMES
from helpers import *
from messages import msgs
from keyboards import *
from session_converter import MangSession

# إعداد نظام تسجيل الأخطاء
logger = logging.getLogger(__name__)

# متغيرات حالة المحادثة
active_conversations = {}
manual_creation_tasks = {}
auto_creation_tasks = {}
posting_active = False
user_pages = {}
user_purchased_accounts = {}
user_posting_accounts = {}
bot = None

# ===== دالة إرسال رسالة التسليم =====
async def send_delivery_message(client, phone, buyer_id, price):
    trust_channel = await get_setting("trust_channel")
    if trust_channel:
        try:
            user_info = await client.get_entity(buyer_id)
            first_name = user_info.first_name if user_info.first_name else "مستخدم"
            bot_username = (await client.get_me()).username
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            bot_url = f"https://t.me/{bot_username}"
            bot_button = Button.url("• الدخول الى المتجر •", bot_url)
            
            message = msgs['DELIVERY_MESSAGE'].format(
                f"@{bot_username}",
                phone,
                price,
                now,
                first_name,
                buyer_id,
                buyer_id
            )
            
            await client.send_message(
                trust_channel,
                message,
                parse_mode='markdown',
                buttons=[bot_button]
            )
        except Exception as e:
            logger.error(f"Error sending delivery message: {str(e)}")

# ===== معالجات الأحداث =====

@events.register(events.NewMessage(pattern="/sell_price", func=lambda e: e.is_private))
async def sell_price_handler(event):
    countries = await get_countries()
    text = "\n".join([f"{c[1]} ({c[0]}): {c[3]}$" for c in countries])
    await event.respond(text)

@events.register(events.NewMessage(pattern="/start", func=lambda e: e.is_private))
async def start_handler(event):
    user_id = event.chat_id
    if await is_banned(user_id):
        return
    
    # التحقق من الاشتراك في القنوات الإجبارية
    force_channels = await get_force_channels()
    for channel in force_channels:
        try:
            await event.client(functions.channels.GetParticipantRequest(
                channel=channel,
                participant=user_id
            ))
        except:
            await event.respond(f"**⚠️︙عذراً عزيزي يجب عليك الاشتراك بقناة البوت**\n🚀︙القناه: @{channel}\n\n• اشترك في القناه ثم أرسل: /start")
            return

    # تسجيل المستخدم الجديد
    user_data = await get_user(user_id)
    if not user_data:
        await create_user(user_id)
        user_info = await event.client.get_entity(user_id)
        username = f"@{user_info.username}" if user_info.username else "None"
        await event.client.send_message(
            ADMIN_ID,
            f'• عضو جديد:\n- الاسم: <a href="tg://user?id={user_id}">{user_info.first_name}</a>\n- المعرف: {username}\n- الايدي: {user_id}',
            parse_mode="html"
        )
        user_data = await get_user(user_id)

    coins = user_data['coins'] if user_data else 0
    
    # تحضير الأزرار
    await event.respond(msgs['START_MESSAGE'].format(
        user_id, 
        coins
    ), buttons=start_keyboard(user_id, await is_admin(user_id)))

@events.register(events.CallbackQuery())
async def callback_handler(event):
    data = event.data.decode('utf-8')
    user_id = event.chat_id
    
    if await is_banned(user_id):
        return
    
    # التحقق من الاشتراك في القنوات الإجبارية
    force_channels = await get_force_channels()
    for channel in force_channels:
        try:
            await event.client(functions.channels.GetParticipantRequest(
                channel=channel,
                participant=user_id
            ))
        except:
            await event.answer("عليك الاشتراك في القناة أولاً!", alert=True)
            return

    # معالجة الأحداث
    
    if data == "ajxjao":
        await num_settings(event)
    elif data == "change_price":
        await change_price_menu(event)
    elif data == "change_sell_price":
        await change_sell_price_menu(event)
    elif data == "ajxkho":
        await force_settings(event)
    elif data == "aksgl":
        await admin_settings(event)
    elif data == "ajkofgl":
        await buy_sell_settings(event)
    elif data == "ajkcoingl":
        await balance_settings(event)
    elif data == "bbvjls":
        await ban_settings(event)
    elif data == "set_trust_channel":
        await set_trust_channel(event)
    elif data == "edit_rules":
        await edit_rules(event)
    elif data == "numbers_menu":
        await numbers_menu(event)
    elif data == "control_menu":
        await control_menu(event)
    elif data == "buy":
        await buy_number(event)
    elif data == "ssart":
        await withdraw_balance(event)
    elif data == "transfer":
        await transfer_balance(event)
    elif data == "supper":
        await support_request(event)
    elif data == "liscgh":
        await show_rules(event)
    elif data == "sell":
        await sell_account(event)
    elif data == "admin_panel":
        await admin_panel(event)
    elif data == "control_settings_super":
        await super_settings_menu(event)
    elif data == "control_settings_posting":
        await posting_settings_menu(event)
    elif data == "control_settings_creation":
        await creation_settings_menu(event)
    elif data == "control_settings_account":
        await account_settings_menu(event)
    elif data == "add_super":
        await event.answer(msgs['SUPER_FEATURE_COMING_SOON'], alert=True)
    elif data == "del_super":
        await del_super_channel_handler(event)
    elif data == "show_supers":
        await show_super_channels(event)
    elif data == "clear_supers":
        await clear_super_channels(event)
    elif data == "add_template":
        await add_posting_template_handler(event)
    elif data == "del_template":
        await del_posting_template_handler(event)
    elif data == "show_templates":
        await show_posting_templates(event)
    elif data == "edit_template":
        await edit_posting_template_handler(event)
    elif data == "enable_multi_posting":
        await set_posting_setting(event, "multi_posting", "1")
    elif data == "disable_multi_posting":
        await set_posting_setting(event, "multi_posting", "0")
    elif data == "start_posting":
        await start_posting_handler(event)
    elif data == "stop_posting_group":
        await stop_posting_group_handler(event)
    elif data == "stop_all_posting":
        await stop_all_posting_handler(event)
    elif data == "manual_creation":
        await manual_group_creation_handler(event)
    elif data == "stop_manual_creation":
        await stop_manual_creation_handler(event)
    elif data == "auto_creation":
        await auto_group_creation_handler(event)
    elif data == "stop_auto_creation":
        await stop_auto_creation_handler(event)
    elif data == "timed_name_on":
        await toggle_timed_name(event, True)
    elif data == "timed_name_off":
        await toggle_timed_name(event, False)
    elif data == "change_profile_photo":
        await change_profile_photo(event)
    elif data == "change_bio":
        await change_bio(event)
    elif data == "change_username":
        await change_username(event)
    elif data == "enable_stealth_mode":
        await enable_stealth_mode(event)
    elif data == "disable_stealth_mode":
        await disable_stealth_mode(event)
    elif data == "enable_notifications":
        await enable_notifications(event)
    elif data == "disable_notifications":
        await disable_notifications(event)
    elif data == "add_mandatory_channel":
        await add_mandatory_channel_handler(event)
    elif data == "funding":
        await funding_handler(event)
    elif data == "install_session":
        await install_session_handler(event)
    elif data.startswith("country_"):
        await country_selected(event, data)
    elif data.startswith("select_account_"):
        await select_account(event, data)
    elif data.startswith("buy_"):
        await buy_confirmed(event, data)
    elif data.startswith("confirm_purchase:"):
        await confirm_purchase_handler(event, data)
    elif data.startswith("store_account:"):
        await store_account_handler(event, data)
    elif data.startswith("logout:"):
        await logout_account(event, data)
    elif data.startswith("sell_logout_"):
        await sell_logout_handler(event, data)
    elif data.startswith("get:"):
        await get_code_handler(event, data)
    elif data.startswith("show_"):
        await show_accounts(event, data)
    elif data.startswith("v:"):
        await account_details(event, data)
    elif data.startswith("del:"):
        await del_account_confirm(event, data)
    elif data.startswith("del_done:"):
        await del_account_done(event, data)
    elif data.startswith("rig_"):
        await add_number_process(event, data)
    elif data.startswith("delete_"):
        await del_country(event, data)
    elif data.startswith("next_sell:"):
        await next_sell(event, data)
    elif data.startswith("check:"):
        await check_account(event, data)
    elif data.startswith("chs_"):
        await change_sell_price(event, data)
    elif data.startswith("chg_"):
        await change_buy_price(event, data)
    elif data == "add_force":
        await add_force_channel_handler(event)
    elif data == "del_force":
        await del_force_channel_handler(event)
    elif data == "add_admin":
        await add_admin(event)
    elif data == "del_admin":
        await del_admin(event)
    elif data == "add_coins":
        await add_coins(event)
    elif data == "del_coins":
        await del_coins(event)
    elif data == "ban":
        await ban_user(event)
    elif data == "unban":
        await unban_user(event)
    elif data.startswith("confirm_withdraw_"):
        await confirm_withdraw(event, data)
    elif data.startswith("reply_"):
        await reply_to_user(event, data)
    elif data == "zip_all":
        await zip_database(event)
    elif data == "all_of_number":
        await all_numbers_count(event)
    elif data == "add_country":
        await add_country_handler(event)
    elif data == "del_country":
        await del_country_menu(event)
    elif data == "add":
        await add_number_menu(event)
    elif data == "del_account":
        await del_account_menu(event)
    elif data == "cancel_operation":
        await cancel_operation(event)
    elif data == "main":
        await main_menu(event)
    elif data == "back":
        await buy_number(event)
    elif data == "accounts_view":
        await accounts_view_menu(event)
    elif data == "view_stored":
        await view_stored_accounts(event)
    elif data == "view_purchased":
        await view_purchased_accounts(event)
    elif data == "view_sold":
        await view_sold_accounts(event)
    elif data == "view_active":
        await view_active_accounts(event)
    elif data == "next_page_countries":
        await next_countries_page(event)
    elif data == "prev_page_countries":
        await prev_countries_page(event)
    elif data.startswith("account_actions:"):
        await account_actions_menu(event, data)
    elif data.startswith("purchased_account:"):
        await purchased_account_selected(event, data)
    elif data.startswith("select_acc_for_posting:"):
        await select_account_for_posting(event, data)
    elif data == "posting_all":
        await posting_all_channels(event)
    elif data == "posting_specific":
        await select_channel_for_posting(event)
    elif data.startswith("select_channel:"):
        await posting_specific_channel(event, data)
    elif data == "broadcast_message":
        await broadcast_start(event)
    elif data.startswith("del_super_channel:"):
        await handle_delete_super_channel(event, data)
    # إضافة معالجات أزرار التنصيب والمزاد
    elif data == "install_menu":
        await install_menu_handler(event)
    elif data == "delete_install":
        await delete_install_handler(event)
    elif data.startswith("del_install:"):
        await delete_install_account(event, data)
    elif data == "auction_menu":
        await auction_menu_handler(event)
    elif data == "add_auction":
        await add_auction_handler(event)
    elif data == "auction_list":
        await auction_list_handler(event)
    elif data.startswith("view_auction:"):
        await view_auction_handler(event, data)
    elif data.startswith("bid:"):
        await place_bid_handler(event, data)
    elif data.startswith("sell_auction:"):
        await sell_auction_handler(event, data)
    elif data.startswith("auction_logout:"):
        await auction_logout_handler(event, data)

# ===== وظائف القوائم الرئيسية =====

async def change_buy_price_menu(event):
    countries = await get_countries()
    buttons = []
    for c in countries:
        buttons.append([Button.inline(f"{c[1]} (شراء: {c[2]}$)", data=f"chg_{c[0]}_{c[1]}_{c[2]}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="ajkofgl")])
    await event.respond("اختر الدولة لتغيير سعر الشراء:", buttons=buttons)

async def change_sell_price_menu(event):
    countries = await get_countries()
    buttons = []
    for c in countries:
        # تقليل حجم البيانات باستخدام رمز الدولة فقط
        buttons.append([Button.inline(f"{c[1]} (بيع: {c[3]}$)", data=f"chs_{c[0]}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="ajkofgl")])
    await event.edit("اختر الدولة لتغيير سعر البيع:", buttons=buttons)

async def change_price_menu(event):
    countries = await get_countries()
    buttons = []
    for c in countries:
        # تقليل حجم البيانات باستخدام رمز الدولة فقط
        buttons.append([Button.inline(f"{c[1]} (شراء: {c[2]}$)", data=f"chg_{c[0]}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="ajkofgl")])
    await event.edit("اختر الدولة لتغيير سعر الشراء:", buttons=buttons)
async def change_sell_price_menu(event):
    countries = await get_countries()
    buttons = []
    for c in countries:
        buttons.append([Button.inline(f"{c[1]} (بيع: {c[3]}$)", data=f"chs_{c[0]}_{c[1]}_{c[3]}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="ajkofgl")])
    await event.edit("اختر الدولة لتغيير سعر البيع:", buttons=buttons)
    
async def change_buy_price(event, data):
    # استخراج رمز الدولة فقط من البيانات
    calling_code = data.split('_')[1]
    
    # جلب بيانات الدولة من قاعدة البيانات
    country = await get_country(calling_code)
    if not country:
        await event.answer("❌ الدولة غير موجودة!", alert=True)
        return
        
    name = country[1]
    old_price = country[2]
    
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(f"أرسل سعر الشراء الجديد لـ {name}:", buttons=cancel_operation_keyboard())
            new_price = await conv.get_response(timeout=300)
            
            try:
                await execute_query(
                    "UPDATE countries SET price=? WHERE calling_code=?",
                    (float(new_price.text), calling_code),
                    commit=True
                )
                await conv.send_message(f"✅ تم تحديث سعر الشراء لـ {name} إلى {new_price.text}$")
            except:
                await conv.send_message("❌ قيمة غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]
    
async def change_sell_price(event, data):
    # استخراج رمز الدولة فقط من البيانات
    calling_code = data.split('_')[1]
    
    # جلب بيانات الدولة من قاعدة البيانات
    country = await get_country(calling_code)
    if not country:
        await event.answer("❌ الدولة غير موجودة!", alert=True)
        return
        
    name = country[1]
    price = country[2]
    
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(f"أرسل سعر البيع الجديد لـ {name}:", buttons=cancel_operation_keyboard())
            new_price = await conv.get_response(timeout=300)
            
            try:
                await execute_query(
                    "UPDATE countries SET sell_price=? WHERE calling_code=?",
                    (float(new_price.text), calling_code),
                    commit=True
                )
                await conv.send_message(f"✅ تم تحديث سعر البيع لـ {name} إلى {new_price.text}$")
            except:
                await conv.send_message("❌ قيمة غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def num_settings(event):
    await event.edit(
        msgs['ADMIN_NUM_SETTINGS'],
        buttons=num_settings_keyboard()
    )

async def force_settings(event):
    await event.edit(
        msgs['ADMIN_FORCE_SETTINGS'],
        buttons=force_settings_keyboard()
    )

async def admin_settings(event):
    await event.edit(
        msgs['ADMIN_ADMIN_SETTINGS'],
        buttons=admin_settings_keyboard()
    )

async def buy_sell_settings(event):
    await event.edit(
        msgs['ADMIN_BUY_SELL_SETTINGS'],
        buttons=buy_sell_settings_keyboard()
    )

async def balance_settings(event):
    await event.edit(
        msgs['ADMIN_BALANCE_SETTINGS'],
        buttons=balance_settings_keyboard()
    )

async def ban_settings(event):
    await event.edit(
        msgs['ADMIN_BAN_SETTINGS'],
        buttons=ban_settings_keyboard()
    )

async def set_trust_channel(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        await conv.send_message("أرسل معرف القناة:", buttons=cancel_operation_keyboard())
        try:
            response = await conv.get_response(timeout=300)
            channel = response.text.replace('https://t.me/', '').replace('@', '').strip()
            try:
                await event.client.send_message(channel, "تم تفعيل القناة بنجاح")
                await set_setting("trust_channel", channel)
                await conv.send_message("✅ تم تعيين قناة الثبات بنجاح")
            except:
                await conv.send_message("❌ فشل في تعيين القناة! تأكد من رفع البوت كمسؤول")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def edit_rules(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        await conv.send_message("أرسل نص القوانين الجديد:", buttons=cancel_operation_keyboard())
        try:
            response = await conv.get_response(timeout=300)
            await set_setting("rules_message", response.text)
            await conv.send_message("✅ تم تحديث القوانين بنجاح")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def numbers_menu(event):
    await event.edit(msgs['NUMBERS_MENU'], buttons=numbers_menu_keyboard())

async def control_menu(event):
    await event.edit(msgs['CONTROL_MENU'], buttons=control_menu_keyboard())

async def buy_number(event):
    user_id = event.chat_id
    countries = await get_countries()
     # إذا لم توجد دول
    if not countries:
        await event.edit("⭕️ لا توجد دول متاحة حالياً")
        return
    # تهيئة التخزين المؤقت للصفحات
    if user_id not in user_pages:
        user_pages[user_id] = 0
    
    # تجهيز الأزرار للصفحة الحالية
    buttons = []
    start_index = user_pages[user_id] * 20
    end_index = min(start_index + 20, len(countries))
    
    # تنظيم الأزرار في صفوف (صفين لكل صف)
    country_chunk = countries[start_index:end_index]
    for i in range(0, len(country_chunk), 2):
        row = []
        if i < len(country_chunk):
            c1 = country_chunk[i]
            row.append(Button.inline(f"{c1[1]}: {c1[2]}$", data=f"country_{c1[0]}"))
        if i+1 < len(country_chunk):
            c2 = country_chunk[i+1]
            row.append(Button.inline(f"{c2[1]}: {c2[2]}$", data=f"country_{c2[0]}"))
        buttons.append(row)
    
    # إضافة أزرار التنقل بين الصفحات
    nav_buttons = []
    if user_pages[user_id] > 0:
        nav_buttons.append(Button.inline("◀ السابق", data="prev_page_countries"))
    if end_index < len(countries):
        nav_buttons.append(Button.inline("التالي ▶", data="next_page_countries"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([Button.inline("• رجوع • ↩️", data="numbers_menu")])
    buttons.append(cancel_operation_keyboard()[0])
    
    await event.edit(msgs['COUNTRY_LIST'], buttons=buttons)

async def next_countries_page(event):
    user_id = event.chat_id
    if user_id not in user_pages:
        user_pages[user_id] = 0
    
    # احصل على قائمة الدول
    countries = await get_countries()
    
    # احسب عدد الصفحات المتاحة
    total_pages = (len(countries) // 20)
    
    # إذا كانت الصفحة الحالية هي الأخيرة
    if user_pages[user_id] >= total_pages:
        await event.answer("❗️ هذه آخر صفحة", alert=True)
        return  # لا تقم بأي تحديث
    
    # غير الصفحة فقط إذا كان هناك صفحات أخرى
    user_pages[user_id] += 1
    await buy_number(event)

async def prev_countries_page(event):
    user_id = event.chat_id
    if user_id not in user_pages:
        user_pages[user_id] = 0
    
    # إذا كانت الصفحة الحالية هي الأولى
    if user_pages[user_id] <= 0:
        await event.answer("❗️ هذه أول صفحة", alert=True)
        return  # لا تقم بأي تحديث
    
    user_pages[user_id] -= 1
    await buy_number(event)
    
async def prev_countries_page(event):
    user_id = event.chat_id
    if user_id not in user_pages:
        user_pages[user_id] = 0
    if user_pages[user_id] > 0:
        user_pages[user_id] -= 1
    await buy_number(event)

async def withdraw_balance(event):
    user_id = event.chat_id
    user_data = await get_user(user_id)
    coins = user_data['coins'] if user_data else 0
    if coins < 1:
        await event.answer("الحد الأدنى للسحب هو 1$", alert=True)
        return
    
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رقم الكاش أو المحفظة:", buttons=cancel_operation_keyboard())
            cash_info = await conv.get_response(timeout=300)
            
            await conv.send_message("أدخل المبلغ المراد سحبه:", buttons=cancel_operation_keyboard())
            amount_info = await conv.get_response(timeout=300)
            
            try:
                amount = float(amount_info.text)
                if amount > coins:
                    await conv.send_message("رصيدك غير كافي!")
                    return
                new_coins = coins - amount
                await update_user_coins(user_id, new_coins)
                
                await event.client.send_message(
                    ADMIN_ID, 
                    f"• طلب سحب رصيد:\n- العضو: {user_id}\n- المبلغ: {amount}$\n- المحفظة: {cash_info.text}",
                    buttons=[[Button.inline("• تأكيد التحويل • ✅", data=f"confirm_withdraw_{user_id}")]]
                )
                await conv.send_message(msgs['WITHDRAW_SUCCESS'].format(amount))
            except ValueError:
                await conv.send_message("المبلغ غير صحيح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def transfer_balance(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(msgs['TRANSFER_MESSAGE'], buttons=cancel_operation_keyboard())
            user_id_resp = await conv.get_response(timeout=300)
            try: target_id = int(user_id_resp.text)
            except: 
                await conv.send_message("ايدي غير صحيح!")
                return
            
            if event.chat_id == target_id: 
                await conv.send_message("لا يمكن التحويل لنفسك!")
                return
            if not await get_user(target_id): 
                await conv.send_message("العضو غير موجود!")
                return
            
            min_transfer = float(await get_setting("transfer_minimum") or 5)
            await conv.send_message(f"أدخل المبلغ (الحد الأدنى: {min_transfer}$):", buttons=cancel_operation_keyboard())
            amount_resp = await conv.get_response(timeout=300)
            try: amount = float(amount_resp.text)
            except: 
                await conv.send_message("مبلغ غير صحيح!")
                return
            
            if amount < min_transfer: 
                await conv.send_message(f"الحد الأدنى: {min_transfer}$")
                return
            sender_data = await get_user(event.chat_id)
            sender_coins = sender_data['coins'] if sender_data else 0
            if sender_coins < amount: 
                await conv.send_message("رصيدك غير كافي!")
                return
            
            # تطبيق العمولة
            fee = amount * 0.02
            total = amount + fee
            
            # تحديث الرصيد
            await update_user_coins(event.chat_id, sender_coins - total)
            receiver_data = await get_user(target_id)
            receiver_coins = receiver_data['coins'] if receiver_data else 0
            await update_user_coins(target_id, receiver_coins + amount)
            
            await conv.send_message(f"✅ تم تحويل {amount}$ للعضو {target_id}")
            await event.client.send_message(target_id, f"استلمت تحويل بقيمة {amount}$ من {event.chat_id}")
            await event.client.send_message(ADMIN_ID, f"• تحويل رصيد:\n- من: {event.chat_id}\n- إلى: {target_id}\n- المبلغ: {amount}$\n- العمولة: {fee}$")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def support_request(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل رسالتك للدعم:", buttons=cancel_operation_keyboard())
            message = await conv.get_response(timeout=300)
            
            user_info = await event.client.get_entity(event.chat_id)
            username = f"@{user_info.username}" if user_info.username else "لا يوجد"
            
            await event.client.send_message(
                ADMIN_ID,
                f"📩 رسالة دعم جديدة:\n\n"
                f"- العضو: <a href='tg://user?id={event.chat_id}'>{user_info.first_name}</a>\n"
                f"- المعرف: {username}\n"
                f"- الايدي: {event.chat_id}\n\n"
                f"الرسالة: {message.text}",
                parse_mode="html",
                buttons=[[Button.inline("• الرد على العضو • ↩️", data=f"reply_{event.chat_id}")]]
            )
            await conv.send_message("تم إرسال رسالتك للدعم، سيتم الرد قريباً ✅")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def show_rules(event):
    rules = await get_setting("rules_message") or "مرحباً، القوانين قيد التحديث..."
    await event.edit(rules, buttons=[[Button.inline("• رجوع • ↩️", data="control_menu")]])

async def sell_account(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل رقم الهاتف (مثال: +9647801234567):", buttons=cancel_operation_keyboard())
            phone_resp = await conv.get_response(timeout=300)
            phone = phone_resp.text.replace("+", "").replace(" ", "")
            
            # التحقق من الدولة
            country = None
            countries = await get_countries()
            for c in countries:
                if phone.startswith(c[0]):
                    country = c
                    break
            
            if not country:
                await conv.send_message("❌ الدولة غير مدعومة!")
                return
            
            # عملية تسجيل الدخول
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                await client.send_code_request(f"+{phone}")
                await conv.send_message("أرسل الكود (5 أرقام):", buttons=cancel_operation_keyboard())
                code_resp = await conv.get_response(timeout=300)
                code = code_resp.text.replace(" ", "")
                
                try:
                    # محاولة تسجيل الدخول بدون كلمة مرور
                    await client.sign_in(f"+{phone}", code)
                    twofa = 'لا يوجد'
                except SessionPasswordNeededError:
                    # إذا طلب كلمة المرور
                    await conv.send_message("🔐 الحساب محمي بتحقق بخطوتين. أرسل كلمة المرور:", buttons=cancel_operation_keyboard())
                    password_resp = await conv.get_response(timeout=300)
                    await client.sign_in(password=password_resp.text)
                    twofa = password_resp.text
                
                # حفظ الجلسة
                session_str = client.session.save()
                
                # إضافة الحساب كبيع معلق
                await add_pending_sale(phone, event.chat_id, country[3], country[0], session_str, twofa)
                
                # إرسال رسالة نجاح
                await conv.send_message(msgs['PENDING_SALE'].format(phone, country[3]))
                
                # طلب تسجيل الخروج من الجلسات الأخرى
                buttons = [[Button.inline("• تم الخروج • ✅", data=f"sell_logout_{phone}")]]
                await conv.send_message(msgs['SELL_LOGOUT_INSTRUCTIONS'], buttons=buttons)
                
            except Exception as e:
                await conv.send_message(f"❌ خطأ: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def admin_panel(event):
    await event.edit(msgs['ADMIN_MESSAGE'], buttons=admin_panel_keyboard())

async def super_settings_menu(event):
    await event.edit(msgs['SUPER_MENU'], buttons=super_settings_keyboard())

async def show_super_channels(event):
    channels = await get_super_channels()
    if not channels:
        await event.answer("❌ لا توجد قنوات سوبر مضافّة!", alert=True)
        return
    
    text = "📢 القنوات السوبر المضافّة:\n\n"
    for i, channel in enumerate(channels, 1):
        text += f"{i}. {channel[1]} (ID: {channel[0]})\n"
    
    await event.answer(text, alert=True)

async def clear_super_channels(event):
    await execute_query("DELETE FROM super_channels", commit=True)
    await event.answer("✅ تم حذف جميع القنوات السوبر!", alert=True)

async def del_super_channel_handler(event):
    channels = await get_super_channels()
    if not channels:
        await event.answer("❌ لا توجد قنوات سوبر!", alert=True)
        return
    
    buttons = []
    for channel in channels:
        buttons.append([Button.inline(channel[1], data=f"del_super_channel:{channel[0]}")])
    
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_super")])
    await event.edit("اختر القناة لحذفها:", buttons=buttons)

async def handle_delete_super_channel(event, data):
    channel_id = data.split(':')[1]
    await delete_super_channel(channel_id)
    await event.answer(f"✅ تم حذف القناة السوبر {channel_id}!", alert=True)
    await super_settings_menu(event)
    
async def posting_settings_menu(event):
    await event.edit(msgs['POSTING_MENU'], buttons=posting_settings_keyboard())

async def add_posting_template_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل كليشة النشر الجديدة:", buttons=cancel_operation_keyboard())
            template = await conv.get_response(timeout=300)
            await add_posting_template(template.text)
            await conv.send_message("✅ تم إضافة الكليشة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_posting_template_handler(event):
    templates = await get_posting_templates()
    buttons = []
    for template in templates:
        preview = template[1][:30] + "..." if len(template[1]) > 30 else template[1]
        buttons.append([Button.inline(preview, data=f"del_template:{template[0]}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="posting_settings_menu")])
    await event.edit("اختر الكليشة للحذف:", buttons=buttons)

async def show_posting_templates(event):
    templates = await get_posting_templates()
    if not templates:
        return await event.answer("لا توجد كلايش مضافّة!", alert=True)
    
    text = "📝 الكلايش المضافّة:\n\n"
    for i, template in enumerate(templates, 1):
        text += f"{i}. {template[1]}\n\n"
    
    await event.answer(text, alert=True)

async def edit_posting_template_handler(event):
    templates = await get_posting_templates()
    buttons = []
    for template in templates:
        preview = template[1][:30] + "..." if len(template[1]) > 30 else template[1]
        buttons.append([Button.inline(preview, data=f"edit_template:{template[0]}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="posting_settings_menu")])
    await event.edit("اختر الكليشة للتعديل:", buttons=buttons)

async def set_posting_setting(event, key, value):
    await set_setting(key, value)
    await event.answer(f"✅ تم تحديث الإعداد بنجاح!", alert=True)

async def start_posting_handler(event):
    user_id = event.chat_id
    
    # التحقق من وجود حسابات مخزنة للمستخدم
    stored_accounts = await get_stored_accounts(user_id)
    if not stored_accounts:
        await event.answer("❌ لا توجد حسابات مخزنة لاستخدامها في النشر!", alert=True)
        return
    
    # عرض قائمة الحسابات المخزنة للاختيار
    buttons = []
    for account in stored_accounts:
        phone = account[0]
        buttons.append([Button.inline(f"+{phone}", data=f"select_acc_for_posting:{phone}")])
    
    buttons.append(cancel_operation_keyboard()[0])
    
    await event.edit("📱 اختر الحساب الذي تريد استخدامه للنشر:", buttons=buttons)

async def select_account_for_posting(event, data):
    phone = data.split(':')[1]
    user_id = event.chat_id
    
    # تخزين الحساب المختار في الذاكرة
    user_posting_accounts[user_id] = phone
    
    # المتابعة لإعدادات النشر
    await ask_posting_settings(event)

async def ask_posting_settings(event):
    user_id = event.chat_id
    phone = user_posting_accounts.get(user_id)
    
    if not phone:
        await event.answer("❌ لم يتم تحديد حساب للنشر!", alert=True)
        return
    
    # التحقق من وجود قوالب نشر
    templates = await get_posting_templates()
    if not templates:
        await event.answer("❌ لا توجد قوالب نشر مضافّة!", alert=True)
        return
    
    # التحقق من وجود قنوات سوبر
    super_channels = await get_super_channels()
    if not super_channels:
        await event.answer("❌ لا توجد قنوات سوبر مضافّة!", alert=True)
        return
    
    buttons = [
        [Button.inline("النشر في جميع القنوات", data="posting_all")],
        [Button.inline("النشر في قناة محددة", data="posting_specific")],
        cancel_operation_keyboard()[0]
    ]
    
    await event.edit(
        f"🔧 إعدادات النشر للحساب: +{phone}\n\n"
        "اختر طريقة النشر:",
        buttons=buttons
    )

async def posting_all_channels(event):
    user_id = event.chat_id
    phone = user_posting_accounts.get(user_id)
    
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                "أدخل الفترة بين النشرات بالثواني (180 ثانية كحد أدنى):",
                buttons=cancel_operation_keyboard()
            )
            interval_resp = await conv.get_response(timeout=300)
            
            await conv.send_message(
                "أدخل عدد مرات التكرار:",
                buttons=cancel_operation_keyboard()
            )
            repetitions_resp = await conv.get_response(timeout=300)
            
            try:
                interval = int(interval_resp.text)
                repetitions = int(repetitions_resp.text)
                
                if interval < 180:
                    await conv.send_message("يجب أن تكون الفترة 180 ثانية على الأقل!")
                    return
                
                # بدء مهمة النشر
                asyncio.create_task(
                    run_posting_task(
                        user_id=user_id,
                        phone=phone,
                        interval=interval,
                        repetitions=repetitions,
                        all_channels=True
                    )
                )
                
                await conv.send_message("✅ بدأ النشر التلقائي في جميع القنوات بنجاح!")
            except ValueError:
                await conv.send_message("الرجاء إدخال أرقام صحيحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def select_channel_for_posting(event):
    user_id = event.chat_id
    super_channels = await get_super_channels()
    
    buttons = []
    for channel in super_channels:
        buttons.append([Button.inline(channel[1], data=f"select_channel:{channel[0]}")])
    
    buttons.append(cancel_operation_keyboard()[0])
    
    await event.edit("📢 اختر القناة للنشر:", buttons=buttons)

async def posting_specific_channel(event, data):
    channel_id = data.split(':')[1]
    user_id = event.chat_id
    phone = user_posting_accounts.get(user_id)
    
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                "أدخل الفترة بين النشرات بالثواني (180 ثانية كحد أدنى):",
                buttons=cancel_operation_keyboard()
            )
            interval_resp = await conv.get_response(timeout=300)
            
            await conv.send_message(
                "أدخل عدد مرات التكرار:",
                buttons=cancel_operation_keyboard()
            )
            repetitions_resp = await conv.get_response(timeout=300)
            
            try:
                interval = int(interval_resp.text)
                repetitions = int(repetitions_resp.text)
                
                if interval < 180:
                    await conv.send_message("يجب أن تكون الفترة 180 ثانية على الأقل!")
                    return
                
                # بدء مهمة النشر
                asyncio.create_task(
                    run_posting_task(
                        user_id=user_id,
                        phone=phone,
                        interval=interval,
                        repetitions=repetitions,
                        all_channels=False,
                        channel_id=channel_id
                    )
                )
                
                await conv.send_message("✅ بدأ النشر التلقائي في القناة المحددة بنجاح!")
            except ValueError:
                await conv.send_message("الرجاء إدخال أرقام صحيحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def stop_posting_group_handler(event):
    supers = await get_super_channels()
    buttons = []
    for super_ch in supers:
        buttons.append([Button.inline(super_ch[1], data=f"stop_posting:{super_ch[0]}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="posting_settings_menu")])
    await event.edit("اختر السوبر لإيقاف النشر فيه:", buttons=buttons)

async def stop_all_posting_handler(event):
    # إيقاف جميع مهام النشر للمستخدم
    await execute_query(
        "UPDATE active_posting_tasks SET active=0 WHERE user_id=?",
        (event.chat_id,),
        commit=True
    )
    await event.answer(msgs['POSTING_STOPPED'], alert=True)

async def creation_settings_menu(event):
    await event.edit(msgs['CREATION_MENU'], buttons=creation_settings_keyboard())

async def manual_group_creation_handler(event):
    """
    معالج الإنشاء اليدوي للمجموعات
    """
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        try:
            await conv.send_message("أرسل عدد المجموعات المراد إنشاؤها:", buttons=cancel_operation_keyboard())
            count_resp = await conv.get_response(timeout=300)
            
            try:
                count = int(count_resp.text)
                if count < 1 or count > 50:  # حد أقصى 50 مجموعة لكل عملية
                    await conv.send_message("الرجاء إدخال عدد بين 1 و 50!")
                    return
                
                # تخزين المهمة
                manual_creation_tasks[user_id] = {
                    'count': count,
                    'created': 0,
                    'active': True
                }
                
                # بدء عملية الإنشاء
                await conv.send_message(f"⏳ جاري إنشاء {count} مجموعات...")
                
                success_count = 0
                stored_accounts = await get_stored_accounts(user_id)
                
                if not stored_accounts:
                    await conv.send_message("❌ لا توجد حسابات مخزنة!")
                    return
                
                for i in range(count):
                    if not manual_creation_tasks.get(user_id, {}).get('active', True):
                        break
                    
                    # اختيار حساب عشوائي
                    account = random.choice(stored_accounts)
                    group_id, invite_link, _ = await create_private_group(account[1])
                    
                    if group_id:
                        success_count += 1
                        manual_creation_tasks[user_id]['created'] = success_count
                        
                        # إرسال نتيجة الإنشاء
                        await event.client.send_message(
                            user_id,
                            f"✅ المجموعة #{success_count}\n"
                            f"🆔: {group_id}\n"
                            f"🔗: {invite_link}"
                        )
                    
                    await asyncio.sleep(10)  # تأخير بين كل إنشاء
                
                # إرسال ملخص النتائج
                await conv.send_message(
                    f"🎉 تم إنشاء {success_count} من أصل {count} مجموعات بنجاح!"
                )
                
            except ValueError:
                await conv.send_message("الرجاء إدخال عدد صحيح!")
                
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in manual_creation_tasks:
                del manual_creation_tasks[user_id]
                
async def stop_manual_creation_handler(event):
    if event.chat_id in manual_creation_tasks:
        manual_creation_tasks[event.chat_id]['active'] = False
        await event.answer("✅ تم إيقاف الإنشاء اليدوي بنجاح!", alert=True)
    else:
        await event.answer("لا توجد مهمة إنشاء يدوي نشطة!", alert=True)

async def auto_group_creation_handler(event):
    """
    معالج الإنشاء التلقائي للمجموعات
    """
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        try:
            await conv.send_message("أرسل الفترة بين كل إنشاء (بالثواني - بين 20 و 3600):", buttons=cancel_operation_keyboard())
            seconds_resp = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل المدة الزمنية للتكرار (بـ الساعات - بين 1 و 24):", buttons=cancel_operation_keyboard())
            hours_resp = await conv.get_response(timeout=300)
            
            try:
                seconds = int(seconds_resp.text)
                hours = int(hours_resp.text)
                
                if not 20 <= seconds <= 3600:
                    await conv.send_message("يجب أن تكون الفترة بين 20 و 3600 ثانية!")
                    return
                
                if not 1 <= hours <= 24:
                    await conv.send_message("يجب أن تكون المدة بين 1 و 24 ساعة!")
                    return
                
                # إنشاء مهمة الإنشاء التلقائي
                await create_auto_creation_task(user_id, seconds, hours)
                auto_creation_tasks[user_id] = {
                    'active': True,
                    'total_created': 0
                }
                
                # بدء عملية الإنشاء في الخلفية
                asyncio.create_task(run_auto_creation(user_id, seconds, hours))
                
                await conv.send_message(
                    f"✅ تم بدء الإنشاء التلقائي بنجاح!\n"
                    f"⏱ الفترة بين الإنشاءات: {seconds} ثانية\n"
                    f"🕒 المدة الإجمالية: {hours} ساعة"
                )
                
            except ValueError:
                await conv.send_message("الرجاء إدخال أرقام صحيحة!")
                
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
            
async def stop_auto_creation_handler(event):
    user_id = event.chat_id
    # إيقاف جميع مهام الإنشاء التلقائي لهذا المستخدم
    if user_id in auto_creation_tasks:
        auto_creation_tasks[user_id] = False

    # تحديث قاعدة البيانات لإيقاف المهام النشطة
    tasks = await get_active_auto_creation_tasks()
    for task in tasks:
        if task[1] == user_id:  # task[1] is user_id
            await update_auto_creation_task(task[0], active=False)

    await event.answer(msgs['AUTO_CREATION_STOPPED'], alert=True)

async def account_settings_menu(event):
    await event.edit(msgs['ACCOUNT_MENU'], buttons=account_settings_keyboard())

async def toggle_timed_name(event, active):
    user_id = event.chat_id
    
    # الحصول على الحسابات المخزنة
    stored_accounts = await get_stored_accounts(user_id)
    if not stored_accounts:
        return await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
    
    if active:
        # تفعيل الاسم الوقتي
        await set_timed_name_active(user_id, True)
        for account in stored_accounts:
            await update_timed_name(account[1])
            await add_active_account(account[0], account[1], user_id, "تحديث الاسم الوقتي")
        await event.answer(msgs['TIMED_NAME_ACTIVATED'], alert=True)
    else:
        # إيقاف الاسم الوقتي
        await set_timed_name_active(user_id, False)
        for account in stored_accounts:
            await remove_timed_name(account[1])
            await remove_active_account(account[0])
        await event.answer(msgs['TIMED_NAME_DEACTIVATED'], alert=True)

async def change_profile_photo(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل الصورة الجديدة:", buttons=cancel_operation_keyboard())
            photo = await conv.get_response(timeout=300)
            if not photo.media:
                await conv.send_message("❌ يجب إرسال صورة!")
                return
            
            # الحصول على الحسابات المخزنة للمستخدم
            stored_accounts = await get_stored_accounts(event.chat_id)
            if not stored_accounts:
                await conv.send_message("❌ لا توجد حسابات مخزنة!")
                return
            
            # تحديث الصورة لكل حساب
            for account in stored_accounts:
                session = account[1]
                try:
                    client = TelegramClient(StringSession(session), API_ID, API_HASH)
                    await client.connect()
                    await client.upload_profile_photo(await photo.download_media())
                    await client.disconnect()
                except Exception as e:
                    print(f"Error updating profile photo: {str(e)}")
            
            await conv.send_message("✅ تم تحديث الصورة الشخصية لجميع الحسابات المخزنة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def change_bio(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل البايو الجديد:", buttons=cancel_operation_keyboard())
            bio = await conv.get_response(timeout=300)
            
            # الحصول على الحسابات المخزنة للمستخدم
            stored_accounts = await get_stored_accounts(event.chat_id)
            if not stored_accounts:
                await conv.send_message("❌ لا توجد حسابات مخزنة!")
                return
            
            # تحديث البايو لكل حساب
            for account in stored_accounts:
                session = account[1]
                try:
                    client = TelegramClient(StringSession(session), API_ID, API_HASH)
                    await client.connect()
                    await client(functions.account.UpdateProfileRequest(about=bio.text))
                    await client.disconnect()
                except Exception as e:
                    print(f"Error updating bio: {str(e)}")
            
            await conv.send_message("✅ تم تحديث البايو لجميع الحسابات المخزنة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def change_username(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل اسم المستخدم الجديد (بدون @):", buttons=cancel_operation_keyboard())
            username = await conv.get_response(timeout=300)
            
            # الحصول على الحسابات المخزنة للمستخدم
            stored_accounts = await get_stored_accounts(event.chat_id)
            if not stored_accounts:
                await conv.send_message("❌ لا توجد حسابات مخزنة!")
                return
            
            # تحديث اسم المستخدم لكل حساب
            for account in stored_accounts:
                session = account[1]
                try:
                    client = TelegramClient(StringSession(session), API_ID, API_HASH)
                    await client.connect()
                    await client(functions.account.UpdateUsernameRequest(username.text))
                    await client.disconnect()
                except Exception as e:
                    print(f"Error updating username: {str(e)}")
            
            await conv.send_message("✅ تم تحديث اسم المستخدم لجميع الحسابات المخزنة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def enable_stealth_mode(event):
    await event.answer("✅ تم تفعيل الوضع الخفي بنجاح!", alert=True)

async def disable_stealth_mode(event):
    await event.answer("✅ تم إيقاف الوضع الخفي بنجاح!", alert=True)

async def enable_notifications(event):
    await event.answer("✅ تم تفعيل التنبيهات بنجاح!", alert=True)

async def disable_notifications(event):
    await event.answer("✅ تم إيقاف التنبيهات بنجاح!", alert=True)

async def add_mandatory_channel_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل معرف القناة الإجبارية:", buttons=cancel_operation_keyboard())
            channel = await conv.get_response(timeout=300)
            await set_setting("mandatory_channel", channel.text.strip())
            await conv.send_message(msgs['MANDATORY_CHANNEL_SET'])
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def funding_handler(event):
    user_id = event.chat_id
    is_admin = user_id == ADMIN_ID or await is_admin(user_id)  # التحقق من صلاحيات المستخدم
    
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                "أرسل رابط القناة أو المجموعة (رابط مباشر أو رابط مشاركة):\n"
                "مثال للرابط المباشر: @channel_name أو https://t.me/channel_name\n"
                "مثال لرابط المشاركة: https://t.me/c/123456789/123",
                buttons=cancel_operation_keyboard()
            )
            channel_input = await conv.get_response(timeout=300)
            input_text = channel_input.text.strip()

            # تحليل الرابط لتحديد المعرف أو ID
            target = None
            if input_text.startswith("https://t.me/"):
                parts = input_text.split("/")
                if "c/" in input_text:  # رابط مشاركة (https://t.me/c/123456789/123)
                    target = parts[3]  # الجزء الذي يحتوي على ID القناة
                else:  # رابط مباشر (https://t.me/channel_name)
                    target = parts[3].replace("@", "")
            elif input_text.startswith("@"):  # معرف مباشر (@channel_name)
                target = input_text.replace("@", "")
            else:  # قد يكون ID رقمي مباشر
                target = input_text

            if not target:
                await conv.send_message("❌ الرابط غير صالح!")
                return

            # تحديد مصدر الحسابات بناءً على صلاحية المستخدم
            if is_admin:
                # للمطور/المشرف: استخدام جميع الحسابات المخزونة في البوت
                accounts = await execute_query(
                    "SELECT phone, session FROM stored_accounts", 
                    (), 
                    fetchall=True
                )
                message = "✅ تم بدء تمويل القناة باستخدام **جميع الحسابات المخزنة في البوت**!"
            else:
                # للمستخدم العادي: استخدام الحسابات المخزنة الخاصة به فقط
                accounts = await get_stored_accounts(user_id)
                message = "✅ تم بدء تمويل القناة باستخدام **حساباتك المخزنة**!"

            if not accounts:
                await conv.send_message("❌ لا توجد حسابات متاحة للتمويل!")
                return

            success_count = 0
            failed_count = 0
            processing_message = await conv.send_message("⏳ جاري معالجة الحسابات...")

            for account in accounts:
                phone, session = account[0], account[1]
                client = None
                try:
                    client = TelegramClient(StringSession(session), API_ID, API_HASH)
                    await client.connect()
                    
                    # محاولة الانضمام إلى القناة/المجموعة
                    try:
                        # محاولة الانضمام باستخدام المعرف أو ID
                        await client(JoinChannelRequest(channel=target))
                        success_count += 1
                        
                        # تحديث الرسالة كل 10 حسابات
                        if success_count % 10 == 0:
                            await processing_message.edit(
                                f"⏳ جاري المعالجة...\n"
                                f"✅ الحسابات الناجحة: {success_count}\n"
                                f"❌ الحسابات الفاشلة: {failed_count}"
                            )
                            
                        await asyncio.sleep(2)  # تأخير بين المحاولات
                    except Exception as join_error:
                        print(f"فشل الانضمام للحساب {phone}: {str(join_error)}")
                        failed_count += 1
                    finally:
                        if client and client.is_connected():
                            await client.disconnect()
                            
                except Exception as client_error:
                    print(f"خطأ في جلسة الحساب {phone}: {str(client_error)}")
                    failed_count += 1
                    if client and client.is_connected():
                        await client.disconnect()

            # إرسال النتيجة النهائية
            result_message = (
                f"{message}\n\n"
                f"• الرابط المستهدف: {input_text}\n"
                f"• المعرف/ID المستخدم: {target}\n"
                f"• عدد الحسابات الناجحة: {success_count}\n"
                f"• عدد الحسابات الفاشلة: {failed_count}"
            )
            
            await processing_message.edit(result_message)

        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        except Exception as e:
            await conv.send_message(f"❌ حدث خطأ غير متوقع: {str(e)}")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def install_session_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(
                "أرسل كود الجلسة (تليثون أو بايروجرام):",
                buttons=cancel_operation_keyboard()
            )
            session_response = await conv.get_response(timeout=300)
            session_str = session_response.text.strip()
            
            try:
                # التحقق من نوع الجلسة وتحويلها إذا لزم الأمر
                if session_str.startswith('1'):
                    # جلسة تليثون
                    tele_session = session_str
                else:
                    # جلسة بايروجرام، تحويل إلى تليثون
                    tele_session = MangSession.PYROGRAM_TO_TELETHON(session_str)
                
                # اختبار الجلسة
                client = TelegramClient(StringSession(tele_session), API_ID, API_HASH)
                await client.connect()
                me = await client.get_me()
                phone = me.phone
                
                # تخزين الجلسة
                await add_stored_account(phone, tele_session, event.chat_id)
                await conv.send_message(f"✅ تم تخزين الحساب بنجاح! الرقم: +{phone}")
                
            except Exception as e:
                await conv.send_message(f"❌ خطأ في الجلسة: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def country_selected(event, data):
    try:
        # تقسيم البيانات لاستخراج رمز الدولة
        parts = data.split('_')
        if len(parts) < 2:
            return await event.answer("❌ بيانات غير صالحة!", alert=True)
            
        calling_code = parts[1]
        
        # جلب بيانات الدولة من قاعدة البيانات
        country = await get_country(calling_code)
        if not country:
            return await event.answer("❌ الدولة غير موجودة في قاعدة البيانات!", alert=True)
            
        # استخراج المعلومات المطلوبة
        name = country[1]
        price = country[2]  # السعر من قاعدة البيانات
        
        # التحقق من رصيد المستخدم
        user_data = await get_user(event.chat_id)
        coins = user_data['coins'] if user_data else 0
        
        if coins < float(price):
            return await event.answer("رصيدك غير كافي لشراء رقم من هذه الدولة!", alert=True)
        
        # جلب الأرقام المتاحة لهذه الدولة
        accounts = await get_accounts(calling_code)
        if not accounts:
            return await event.answer("❌ لا توجد أرقام متاحة حالياً لهذه الدولة!", alert=True)
        
        # إنشاء أزرار الاختيار
        buttons = []
        for account in accounts:
            phone = account[0]
            buttons.append([Button.inline(f"+{phone}", data=f"select_account_{calling_code}_{phone}")])
        
        # إضافة أزرار التنقل
        buttons.append([Button.inline("• رجوع • ↩️", data="buy")])
        buttons.append(cancel_operation_keyboard()[0])
        
        # عرض القائمة للمستخدم
        await event.edit(f"حسابات {name} المتاحة:", buttons=buttons)
        
    except Exception as e:
        logger.error(f"Error in country_selected: {str(e)}")
        await event.answer("❌ حدث خطأ أثناء معالجة طلبك!", alert=True)
        
async def select_account(event, data):
    parts = data.split('_')
    calling_code = parts[2]
    phone = parts[3]
    
    # جلب بيانات الدولة من قاعدة البيانات
    country = await get_country(calling_code)
    if not country:
        return await event.answer("❌ الدولة غير موجودة!", alert=True)
    
    name = country[1]
    price = country[2]
    
    buttons = [
        [Button.inline("• تأكيد الشراء • ✅", data=f"buy_{calling_code}_{name}_{price}_{phone}")],
        [Button.inline("• رجوع • ↩️", data=f"country_{calling_code}")],
        cancel_operation_keyboard()[0]
    ]
    
    await event.edit(
        msgs['BUY_MESSAGE'].format(name, phone, price),
        buttons=buttons
    )
async def buy_confirmed(event, data):
    try:
        parts = data.split('_')
        if len(parts) < 5:
            return await event.answer("❌ بيانات غير صالحة!", alert=True)
            
        calling_code = parts[1]
        name = parts[2]
        price = parts[3]
        phone = parts[4]
        
        # تخزين عملية الشراء المؤقتة
        accounts = await get_accounts(calling_code)
        account = next((a for a in accounts if a[0] == phone), None)
        
        if not account:
            return await event.answer("الحساب غير موجود!", alert=True)
        
        await add_pending_purchase(
            event.chat_id,
            phone,
            calling_code,
            name,
            price,
            account[1],  # session
            account[3]   # twofa
        )
        
        # عرض خيارات ما بعد الشراء
        buttons = [
            [Button.inline("• تم الشراء • ✅", data=f"confirm_purchase:{phone}")],
            [Button.inline("• تخزين الحساب • 💾", data=f"store_account:{phone}")],
            [Button.inline("• الحصول على الكود • 🔑", data=f"get:{phone}:{calling_code}:{name}:{price}")],
            [Button.inline("• خروج جميع الجلسات • 🚪", data=f"logout:{phone}:{calling_code}:{name}:{price}")],
            cancel_operation_keyboard()[0]
        ]
        
        await event.edit(
            "✅ تم حجز الحساب بنجاح! اختر الإجراء:",
            buttons=buttons
        )
    except Exception as e:
        logger.error(f"Error in buy_confirmed: {str(e)}")
        await event.answer("❌ حدث خطأ أثناء معالجة طلبك!", alert=True)

async def confirm_purchase_handler(event, data):
    phone = data.split(':')[1]
    user_id = event.chat_id
    
    # الحصول على عملية الشراء المؤقتة
    purchase = await get_pending_purchase(user_id, phone)
    if not purchase:
        return await event.answer("لم يتم العثور على عملية شراء!", alert=True)
    
    # خصم الرصيد
    user_data = await get_user(user_id)
    new_coins = user_data['coins'] - float(purchase[5])
    await update_user_coins(user_id, new_coins)
    
    # إخراج جميع الجلسات عدا الجلسة الحالية
    session_str = purchase[6]
    success = await logout_all_sessions(session_str)
    
    if success:
        # حذف الحساب من قاعدة البيانات الرئيسية
        await delete_account(phone)
        
        # الانضمام إلى القناة الإجبارية
        await join_mandatory_channel(session_str)
        
        # إرسال رسالة تأكيد
        await event.answer(msgs['PURCHASE_CONFIRMED'], alert=True)
        await event.edit(msgs['PURCHASE_CONFIRMED'])
        
        # إرسال رسالة التسليم
        await send_delivery_message(event.client, phone, user_id, purchase[5])
        
    else:
        await event.answer(msgs['LOGOUT_FAILED'], alert=True)
    
    # حذف عملية الشراء المؤقتة
    await delete_pending_purchase(user_id, phone)

async def store_account_handler(event, data):
    phone = data.split(':')[1]
    user_id = event.chat_id
    
    # الحصول على عملية الشراء المؤقتة
    purchase = await get_pending_purchase(user_id, phone)
    if not purchase:
        return await event.answer("لم يتم العثور على عملية شراء!", alert=True)
    
    # خصم الرصيد
    user_data = await get_user(user_id)
    new_coins = user_data['coins'] - float(purchase[5])
    await update_user_coins(user_id, new_coins)
    
    # تخزين الحساب للاستخدام المستقبلي
    await add_stored_account(phone, purchase[6], user_id)
    await add_purchased_account(phone, purchase[6], user_id)
    
    # الانضمام إلى القناة الإجبارية
    await join_mandatory_channel(purchase[6])
    
    # حذف الحساب من قاعدة البيانات الرئيسية
    await delete_account(phone)
    
    # حذف عملية الشراء المؤقتة
    await delete_pending_purchase(user_id, phone)
    
    # إرسال رسالة تأكيد
    await event.answer(msgs['STORAGE_SUCCESS'], alert=True)
    
    # إرسال رسالة التسليم
    await send_delivery_message(event.client, phone, user_id, purchase[5])
    
    await main_menu(event)

async def logout_account(event, data):
    parts = data.split(':')
    phone = parts[1]
    calling_code = parts[2]
    name = parts[3]
    price = parts[4]
    
    accounts = await get_accounts(calling_code)
    account = next((a for a in accounts if a[0] == phone), None)
    
    if not account:
        return await event.answer("الحساب غير موجود!", alert=True)
    
    # تسجيل الخروج من جميع الجلسات
    success = await logout_all_sessions(account[1])
    if success:
        await event.answer(msgs['LOGOUT_SUCCESS'], alert=True)
    else:
        await event.answer(msgs['LOGOUT_FAILED'], alert=True)
    
    # حذف الحساب من قاعدة البيانات بعد تسليمه
    await delete_account(phone)

async def get_code_handler(event, data):
    parts = data.split(':')
    phone = parts[1]
    calling_code = parts[2]
    name = parts[3]
    price = parts[4]
    
    accounts = await get_accounts(calling_code)
    account = next((a for a in accounts if a[0] == phone), None)
    
    if not account:
        return await event.answer("الحساب غير موجود!", alert=True)
    
    code = await get_code(account[1])
    if code.isdigit():
        # تنسيق الكود بشكل منقط
        dotted_code = '-'.join(list(code))
        
        # إرسال إشعار للقناة
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trust_channel = await get_setting("trust_channel")
        if trust_channel:
            await event.client.send_message(
                trust_channel,
                msgs['TRUST_MESSAGE'].format(
                    name,
                    phone,
                    price,
                    event.chat_id,
                    account[3] if account[3] != 'لا يوجد' else 'لا يوجد',
                    now
                )
            )
        
        # إرسال كلمة المرور للمشتري
        password = account[3] if account[3] != 'لا يوجد' else 'لا يوجد'
        await event.answer(f"الكود: {dotted_code}\nكلمة المرور: {password}", alert=True)
    else:
        await event.answer("❌ لم يتم العثور على الكود!", alert=True)

async def show_accounts(event, data):
    parts = data.split('_')
    calling_code = parts[1]
    name = parts[2]
    price = parts[3]
    
    accounts = await get_accounts(calling_code)
    if not accounts:
        await event.answer("لا توجد حسابات!", alert=True)
        return
    
    buttons = []
    for i, acc in enumerate(accounts, 1):
        buttons.append([Button.inline(f"{i}: +{acc[0]}", data=f"v:{acc[0]}:{calling_code}:{name}:{price}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="del_account")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit(f"حسابات {name}:", buttons=buttons)

async def account_details(event, data):
    parts = data.split(':')
    phone = parts[1]
    calling_code = parts[2]
    name = parts[3]
    price = parts[4]
    
    accounts = await get_accounts(calling_code)
    account = next((a for a in accounts if a[0] == phone), None)
    
    if not account:
        return await event.answer("الحساب غير موجود!", alert=True)
    
    buttons = [
        [Button.inline("• الحصول على الكود • 🔑", data=f"get:{phone}:{calling_code}:{name}:{price}")],
        [Button.inline("• حذف الحساب • 🗑️", data=f"del:{phone}:{calling_code}:{name}")],
        [Button.inline("• رجوع • ↩️", data=f"show_{calling_code}_{name}_{price}")],
        cancel_operation_keyboard()[0]
    ]
    await event.edit(f"رقم الهاتف: +{phone}\nكلمة المرور: {account[3]}", buttons=buttons)

async def del_account_confirm(event, data):
    parts = data.split(':')
    phone = parts[1]
    calling_code = parts[2]
    name = parts[3]
    
    buttons = [
        [Button.inline("• إلغاء • ↩️", data=f"v:{phone}:{calling_code}:{name}")],
        [Button.inline("• تأكيد الحذف • ✅", data=f"del_done:{phone}:{calling_code}:{name}")],
        cancel_operation_keyboard()[0]
    ]
    await event.edit(f"هل تريد حذف +{phone}؟", buttons=buttons)

async def del_account_done(event, data):
    parts = data.split(':')
    phone = parts[1]
    calling_code = parts[2]
    name = parts[3]
    
    await delete_account(phone)
    await event.edit(f"✅ تم حذف +{phone} بنجاح!")

async def add_number_process(event, data):
    # استخراج رمز الدولة فقط من البيانات
    calling_code = data.split('_')[1]
    
    # جلب بيانات الدولة من قاعدة البيانات
    country = await get_country(calling_code)
    if not country:
        await event.answer("❌ الدولة غير موجودة!", alert=True)
        return
        
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل رقم الهاتف (مثال: +9647801234567):", buttons=cancel_operation_keyboard())
            phone_resp = await conv.get_response(timeout=300)
            phone = phone_resp.text.replace("+", "").replace(" ", "")
            
            # التحقق من أن الرقم يطابق رمز الدولة
            if not phone.startswith(calling_code):
                await conv.send_message(f"❌ الرقم يجب أن يبدأ ب{calling_code} لهذه الدولة!")
                return
            
            # عملية تسجيل الدخول
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                await client.send_code_request(f"+{phone}")
                await conv.send_message("أرسل الكود (5 أرقام):", buttons=cancel_operation_keyboard())
                code_resp = await conv.get_response(timeout=300)
                code = code_resp.text.replace(" ", "")
                
                try:
                    # محاولة تسجيل الدخول بدون كلمة مرور
                    await client.sign_in(f"+{phone}", code)
                    twofa = 'لا يوجد'
                except SessionPasswordNeededError:
                    # إذا طلب كلمة المرور
                    await conv.send_message("🔐 الحساب محمي بتحقق بخطوتين. أرسل كلمة المرور:", buttons=cancel_operation_keyboard())
                    password_resp = await conv.get_response(timeout=300)
                    await client.sign_in(password=password_resp.text)
                    twofa = password_resp.text
                
                # حفظ الجلسة
                session_str = client.session.save()
                
                # إضافة الحساب إلى قاعدة البيانات
                await add_account(phone, session_str, calling_code, twofa)
                await conv.send_message(f"✅ تم إضافة الحساب بنجاح!")
            except Exception as e:
                await conv.send_message(f"❌ خطأ: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_country(event, data):
    parts = data.split('_')
    calling_code = parts[1]
    name = parts[2]
    price = parts[3]
    
    await delete_country(calling_code)
    await event.edit(f"✅ تم حذف {name} بنجاح!")

async def next_sell(event, data):
    phone = data.split(':')[1]
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            # عملية تسجيل الدخول
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                await client.send_code_request(f"+{phone}")
                await conv.send_message("أرسل الكود (5 أرقام):", buttons=cancel_operation_keyboard())
                code_resp = await conv.get_response(timeout=300)
                await client.sign_in(f"+{phone}", code_resp.text.replace(" ", ""))
                
                # حفظ الجلسة
                session_str = client.session.save()
                await conv.send_message("✅ تم التحقق بنجاح! اضغط تحقق بعد تسجيل الخروج من الجلسات الأخرى", 
                                      buttons=[[Button.inline("• تحقق • ✅", data=f"check:{phone}")]])
            except Exception as e:
                await conv.send_message(f"❌ خطأ: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def check_account(event, data):
    phone = data.split(':')[1]
    sessions = await count_ses(phone)
    if sessions == 1:
        await event.answer(msgs['VERIFICATION_SUCCESS'], alert=True)
    else:
        await event.answer(msgs['SELL_VERIFICATION_FAILED'], alert=True)

async def add_force_channel_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل معرف القناة:", buttons=cancel_operation_keyboard())
            channel = await conv.get_response(timeout=300)
            await add_force_channel(channel.text.strip())
            await conv.send_message("✅ تم إضافة القناة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_force_channel_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل معرف القناة:", buttons=cancel_operation_keyboard())
            channel = await conv.get_response(timeout=300)
            await remove_force_channel(channel.text.strip())
            await conv.send_message("✅ تم حذف القناة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def add_admin(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            try:
                await execute_query(
                    "INSERT OR IGNORE INTO admins (user_id) VALUES (?)",
                    (int(user_id.text),),
                    commit=True
                )
                await conv.send_message("✅ تم رفع العضو إلى ادمن بنجاح!")
            except:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_admin(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            try:
                await execute_query(
                    "DELETE FROM admins WHERE user_id=?",
                    (int(user_id.text),),
                    commit=True
                )
                await conv.send_message("✅ تم حذف العضو من الادمنية بنجاح!")
            except:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def add_coins(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            await conv.send_message("أرسل المبلغ:", buttons=cancel_operation_keyboard())
            amount = await conv.get_response(timeout=300)
            
            try:
                user_data = await get_user(int(user_id.text))
                current_coins = user_data['coins'] if user_data else 0
                new_coins = current_coins + float(amount.text)
                await update_user_coins(int(user_id.text), new_coins)
                await conv.send_message(f"✅ تم إضافة {amount.text}$ للعضو {user_id.text}!")
            except:
                await conv.send_message("❌ قيم غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_coins(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            await conv.send_message("أرسل المبلغ:", buttons=cancel_operation_keyboard())
            amount = await conv.get_response(timeout=300)
            
            try:
                user_data = await get_user(int(user_id.text))
                current_coins = user_data['coins'] if user_data else 0
                new_coins = current_coins - float(amount.text)
                await update_user_coins(int(user_id.text), new_coins)
                await conv.send_message(f"✅ تم خصم {amount.text}$ من العضو {user_id.text}!")
            except:
                await conv.send_message("❌ قيم غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def ban_user(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            try:
                await execute_query(
                    "INSERT OR IGNORE INTO bad_guys (user_id) VALUES (?)",
                    (int(user_id.text),),
                    commit=True
                )
                await conv.send_message("✅ تم حظر العضو بنجاح!")
            except:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def unban_user(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            try:
                await execute_query(
                    "DELETE FROM bad_guys WHERE user_id=?",
                    (int(user_id.text),),
                    commit=True
                )
                await conv.send_message("✅ تم إلغاء حظر العضو بنجاح!")
            except:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def confirm_withdraw(event, data):
    user_id = int(data.split('_')[-1])
    await event.client.send_message(user_id, "✅ تم تحويل الرصيد بنجاح!")
    await event.edit("تم إرسال التأكيد للعضو!")

async def reply_to_user(event, data):
    user_id = int(data.split('_')[-1])
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ردك:", buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            await event.client.send_message(user_id, f"📩 رد الدعم:\n{response.text}")
            await conv.send_message("✅ تم إرسال الرد!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def zip_database(event):
    folder_path = "./database"
    zip_file = "database.zip"
    try:
        shutil.make_archive("database", 'zip', folder_path)
        await event.client.send_file(event.chat_id, zip_file)
        os.remove(zip_file)
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def sell_logout_handler(event, data):
    phone = data.split('_')[2]
    
    # الحصول على جلسة الحساب من المبيعات المعلقة
    pending_sale = await get_pending_sale(phone)
    if not pending_sale:
        await event.answer("❌ لم يتم العثور على الحساب المعلق!", alert=True)
        return
    
    session_str = pending_sale[4]  # session
    
    # التحقق من عدد الجلسات
    session_count = await count_ses(session_str)
    
    # إذا كانت هناك جلسة واحدة فقط (جلسة البوت)
    if session_count == 1:
        # نقل الحساب إلى قاعدة البيانات الرئيسية
        seller_id, price, calling_code, session_str, twofa = pending_sale[1], pending_sale[2], pending_sale[3], pending_sale[4], pending_sale[5]
        await add_account(phone, session_str, calling_code, twofa, seller_id)
        await add_sold_account(phone, session_str, seller_id)
        await delete_pending_sale(phone)
        
        # إضافة الرصيد للبائع
        seller_data = await get_user(seller_id)
        if seller_data:
            seller_coins = seller_data['coins']
            await update_user_coins(seller_id, seller_coins + float(price))
        
        await event.answer(msgs['VERIFICATION_SUCCESS'], alert=True)
        await main_menu(event)
    else:
        await event.answer(msgs['SELL_VERIFICATION_FAILED'], alert=True)

async def all_numbers_count(event):
    result = await execute_query("SELECT COUNT(*) FROM accounts", (), fetchone=True)
    count = result[0] if result else 0
    await event.answer(f"عدد ارقام البوت: {count}", alert=True)

async def add_country_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل اسم الدولة مع الرمز (مثال: العراق 🇮🇶):", buttons=cancel_operation_keyboard())
            name = await conv.get_response(timeout=300)
            
            await conv.send_message(f"أرسل رمز النداء (مثال: +964):", buttons=cancel_operation_keyboard())
            calling_code = await conv.get_response(timeout=300)
            
            await conv.send_message(f"أرسل سعر الشراء ($):", buttons=cancel_operation_keyboard())
            price = await conv.get_response(timeout=300)
            
            await conv.send_message(f"أرسل سعر البيع ($):", buttons=cancel_operation_keyboard())
            sell_price = await conv.get_response(timeout=300)
            
            try:
                await add_new_country(
                    name.text,
                    calling_code.text.replace("+", ""),
                    float(price.text),
                    float(sell_price.text)
                )
                await conv.send_message(f"✅ تم إضافة {name.text} بنجاح!")
            except Exception as e:
                await conv.send_message(f"❌ خطأ: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_country_menu(event):
    countries = await get_countries()
    buttons = []
    for c in countries:
        buttons.append([Button.inline(f"{c[1]}: {c[2]}$", data=f"delete_{c[0]}_{c[1]}_{c[2]}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="ajxjao")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit("اختر الدولة للحذف:", buttons=buttons)

async def add_number_menu(event):
    countries = await get_countries()
    if not countries:
        await event.answer("❌ لا توجد دول متاحة!", alert=True)
        return
    
    buttons = []
    for country in countries:
        buttons.append([Button.inline(f"{country[1]} ({country[0]})", data=f"rig_{country[0]}")])
    
    buttons.append([Button.inline("• رجوع • ↩️", data="ajxjao")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit("اختر الدولة لإضافة رقم:", buttons=buttons)
    
async def del_account_menu(event):
    calling_code = data.split('_')[1]  # استخراج رمز الدولة فقط
    country = await get_country(calling_code)  # جلب البيانات من DB
    name = country[1]
    price = country[2]
    countries = await get_countries()
    buttons = []
    for c in countries:
        buttons.append([Button.inline(f"{c[1]}", data=f"show_{c[0]}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="ajxjao")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit("اختر الدولة لحذف رقم منها:", buttons=buttons)

async def cancel_operation(event):
    try:
        if hasattr(event, 'data') and event.data:
            data = event.data.decode('utf-8')
            # معالجة البيانات إذا كانت موجودة
    except Exception as e:
        logger.error(f"Error in cancel_operation: {str(e)}")
    
    user_id = event.chat_id
    if user_id in active_conversations:
        active_conversations[user_id].cancel()
        del active_conversations[user_id]
    await event.answer(msgs['OPERATION_CANCELLED'], alert=True)
    await main_menu(event)
    
async def main_menu(event):
    user_id = event.chat_id
    user_data = await get_user(user_id)
    coins = user_data['coins'] if user_data else 0
    await event.edit(msgs['START_MESSAGE'].format(
        user_id, 
        coins
    ), buttons=start_keyboard(user_id, await is_admin(user_id)))

async def accounts_view_menu(event):
    await event.edit("**👤︙قائمة عرض الحسابات:**\nاختر نوع الحسابات التي تريد عرضها:", buttons=accounts_view_keyboard())

async def view_stored_accounts(event):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return
    
    text = "**💾 الحسابات المخزنة:**\n\n"
    for i, account in enumerate(accounts, 1):
        text += f"{i}. +{account[0]}\n"
    
    await event.answer(text, alert=True)

async def view_purchased_accounts(event):
    user_id = event.chat_id
    accounts = await get_purchased_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مشتراة!", alert=True)
        return
    
    # تخزين الحسابات المشتراة في الذاكرة للوصول السريع
    user_purchased_accounts[user_id] = accounts
    
    buttons = []
    for i, account in enumerate(accounts, 1):
        buttons.append([Button.inline(f"{i}. +{account[0]}", data=f"purchased_account:{account[0]}")])
    
    buttons.append([Button.inline("• رجوع • ↩️", data="accounts_view")])
    
    await event.edit("**🛒 الحسابات المشتراة:**\nاختر حساباً لرؤية خياراته:", buttons=buttons)

async def purchased_account_selected(event, data):
    phone = data.split(':')[1]
    user_id = event.chat_id
    
    # البحث عن الحساب المحدد
    accounts = user_purchased_accounts.get(user_id, [])
    account = next((acc for acc in accounts if acc[0] == phone), None)
    
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return
    
    # إنشاء أزرار الإجراءات
    buttons = [
        [Button.inline("• تم الشراء • ✅", data=f"confirm_purchase:{phone}")],
        [Button.inline("• تخزين الحساب • 💾", data=f"store_account:{phone}")],
        [Button.inline("• الحصول على الكود • 🔑", data=f"get:{phone}")],
        [Button.inline("• خروج جميع الجلسات • 🚪", data=f"logout:{phone}")],
        [Button.inline("• رجوع • ↩️", data="view_purchased")]
    ]
    
    await event.edit(f"**خيارات الحساب +{phone}:**\nاختر الإجراء المطلوب:", buttons=buttons)

async def view_sold_accounts(event):
    user_id = event.chat_id
    accounts = await get_sold_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مباعة!", alert=True)
        return
    
    text = "**💰 الحسابات المباعة:**\n\n"
    for i, account in enumerate(accounts, 1):
        text += f"{i}. +{account[0]}\n"
    
    await event.answer(text, alert=True)

async def view_active_accounts(event):
    user_id = event.chat_id
    accounts = await get_active_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات نشطة!", alert=True)
        return
    
    text = "**🔥 الحسابات النشطة:**\n\n"
    for i, account in enumerate(accounts, 1):
        text += f"{i}. +{account[0]} - {account[3]}\n"
    
    await event.answer(text, alert=True)

async def broadcast_start(event):
    user_id = event.chat_id
    if user_id != ADMIN_ID and not await is_admin(user_id):
        await event.answer("❌ ليس لديك صلاحية!", alert=True)
        return

    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("✉️ أرسل الرسالة التي تريد بثها لجميع المستخدمين:", buttons=cancel_operation_keyboard())
            msg = await conv.get_response(timeout=300)
            text = msg.text

            users = await execute_query("SELECT user_id FROM users", fetchall=True)
            success = 0
            failed = 0

            for user in users:
                try:
                    await event.client.send_message(user[0], text)
                    success += 1
                    await asyncio.sleep(0.1)
                except:
                    failed += 1

            await conv.send_message(f"✅ تم إرسال الإذاعة!\n\nتم الإرسال إلى: {success} ✅\nفشل الإرسال إلى: {failed} ❌")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت، لم يتم إرسال الإذاعة.")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

# قسم التنصيب
async def install_menu_handler(event):
    await event.edit("**💾︙قائمة التنصيب:**", buttons=install_menu_keyboard())

async def delete_install_handler(event):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return
    
    buttons = []
    for account in accounts:
        buttons.append([Button.inline(f"+{account[0]}", data=f"del_install:{account[0]}")])
    
    buttons.append([Button.inline("• رجوع • ↩️", data="install_menu")])
    await event.edit("اختر الحساب لحذفه:", buttons=buttons)

async def delete_install_account(event, data):
    phone = event.data.decode().split(":")[1]
    await delete_stored_account(phone)
    await event.answer(f"✅ تم حذف الحساب +{phone} بنجاح!", alert=True)
    await install_menu_handler(event)

# قسم المزاد
async def auction_menu_handler(event):
    await event.edit("**🏷️︙قائمة المزاد:**", buttons=auction_menu_keyboard())

async def add_auction_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        try:
            await conv.send_message("أرسل رقم الهاتف (مثال: +9647801234567):", 
                                  buttons=cancel_operation_keyboard())
            phone_resp = await conv.get_response(timeout=300)
            phone = phone_resp.text.replace("+", "").replace(" ", "")
            
            # تسجيل الدخول
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                await client.send_code_request(f"+{phone}")
                await conv.send_message("أرسل الكود (5 أرقام):", buttons=cancel_operation_keyboard())
                code_resp = await conv.get_response(timeout=300)
                code = code_resp.text.replace(" ", "")
                
                try:
                    await client.sign_in(f"+{phone}", code)
                    twofa = 'لا يوجد'
                except SessionPasswordNeededError:
                    await conv.send_message("🔐 أرسل كلمة المرور:", buttons=cancel_operation_keyboard())
                    password_resp = await conv.get_response(timeout=300)
                    await client.sign_in(password=password_resp.text)
                    twofa = password_resp.text
                
                session_str = client.session.save()
                
                # طلب أقل سعر للمزاد
                await conv.send_message("أدخل أقل سعر للمزاد (يجب أن يكون أكثر من 0.5$):", 
                                  buttons=cancel_operation_keyboard())
                price_resp = await conv.get_response(timeout=300)
                
                try:
                    min_price = float(price_resp.text)
                    if min_price < 0.5:
                        raise ValueError
                except:
                    await conv.send_message("❌ السعر غير صالح!")
                    return
                
                # إضافة المزاد إلى قاعدة البيانات
                await execute_query(
                    '''INSERT INTO auctions 
                    (phone, session, seller_id, min_price, current_bid, status) 
                    VALUES (?, ?, ?, ?, ?, ?)''',
                    (phone, session_str, event.chat_id, min_price, min_price, 'active'),
                    commit=True
                )
                
                # إرسال إشعار لجميع المستخدمين
                auction_id = await execute_query("SELECT last_insert_rowid()", fetchone=True)
                await broadcast_auction(event, auction_id[0], phone, min_price)
                
                await conv.send_message("✅ تم إضافة المزاد بنجاح!")
            except Exception as e:
                await conv.send_message(f"❌ خطأ: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")

async def broadcast_auction(event, auction_id, phone, min_price):
    users = await execute_query("SELECT user_id FROM users", fetchall=True)
    for user in users:
        try:
            await event.client.send_message(
                user[0],
                f"🏷️ **تم إضافة مزاد جديد!**\n\n"
                f"📱 الرقم: +{phone}\n"
                f"💰 أقل سعر: {min_price}$\n\n"
                "اضغط على الزر أدناه لتقديم عرض:",
                buttons=[[Button.inline("تقديم عرض", data=f"bid:{auction_id}")]]
            )
        except:
            continue

async def place_bid_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    
    if not auction or auction[7] != 'active':  # status
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return
    
    current_bid = auction[5]  # current_bid
    min_increment = max(0.1, current_bid * 0.05)  # زيادة 5% كحد أدنى
    
    async with event.client.conversation(event.chat_id) as conv:
        try:
            await conv.send_message(
                f"💰 أعلى عرض حالياً: {current_bid}$\n"
                f"⬆️ أقل زيادة مسموحة: {min_increment:.2f}$\n\n"
                "أدخل عرضك:",
                buttons=cancel_operation_keyboard()
            )
            bid_resp = await conv.get_response(timeout=300)
            
            try:
                bid_amount = float(bid_resp.text)
                if bid_amount < current_bid + min_increment:
                    await conv.send_message(f"❌ يجب أن يكون العرض أعلى من {current_bid + min_increment:.2f}$")
                    return
            except:
                await conv.send_message("❌ قيمة غير صالحة!")
                return
            
            # التحقق من الرصيد
            user_data = await get_user(event.chat_id)
            if not user_data or user_data['coins'] < bid_amount:
                await conv.send_message("❌ رصيدك غير كافي!")
                return
            
            # تحديث المزاد
            await execute_query(
                "UPDATE auctions SET current_bid=?, current_bidder=? WHERE auction_id=?",
                (bid_amount, event.chat_id, auction_id),
                commit=True
            )
            
            # إضافة العرض
            await execute_query(
                "INSERT INTO auction_bids (auction_id, user_id, bid_amount) VALUES (?, ?, ?)",
                (auction_id, event.chat_id, bid_amount),
                commit=True
            )
            
            # إشعار البائع
            await event.client.send_message(
                auction[3],  # seller_id
                f"🏷️ **تم تقديم عرض جديد على مزادك!**\n\n"
                f"📱 الرقم: +{auction[1]}\n"
                f"💰 العرض: {bid_amount}$\n"
                f"👤 المزايد: {event.chat_id}\n\n"
                "اختر الإجراء:",
                buttons=[
                    [Button.inline("بيع", data=f"sell_auction:{auction_id}:{event.chat_id}")],
                    [Button.inline("استمرار المزاد", data=f"continue_auction:{auction_id}")]
                ]
            )
            
            await conv.send_message("✅ تم تقديم عرضك بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")

async def sell_auction_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    buyer_id = int(event.data.decode().split(":")[2])
    
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    buyer_data = await get_user(buyer_id)
    
    # التحقق من رصيد المشتري
    if not buyer_data or buyer_data['coins'] < auction[5]:  # current_bid
        await event.answer("❌ رصيد المشتري غير كافي الآن!", alert=True)
        return
    
    # طلب تسجيل الخروج من البائع
    await event.answer("تم إرسال طلب تسجيل الخروج للبائع", alert=True)
    await event.client.send_message(
        auction[3],  # seller_id
        "🔒 يرجى تسجيل الخروج من جميع الجلسات ما عدا هذه الجلسة، ثم اضغط زر 'تم الخروج'",
        buttons=[[Button.inline("تم الخروج", data=f"auction_logout:{auction_id}:{buyer_id}")]]
    )

async def auction_logout_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    buyer_id = int(event.data.decode().split(":")[2])
    
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    
    # التحقق من عدد الجلسات
    session_count = await count_ses(auction[2])  # session
    
    if session_count > 1:
        await event.answer("❌ لا يزال هناك جلسات نشطة!", alert=True)
        return
    
    # إتمام البيع
    # خصم المبلغ من المشتري
    buyer_data = await get_user(buyer_id)
    await update_user_coins(buyer_id, buyer_data['coins'] - auction[5])
    
    # إضافة الرصيد للبائع
    seller_data = await get_user(auction[3])  # seller_id
    await update_user_coins(auction[3], seller_data['coins'] + auction[5])
    
    # نقل الحساب للمشتري
    await add_stored_account(auction[1], auction[2], buyer_id)  # phone, session
    await execute_query(
        "UPDATE auctions SET status='sold' WHERE auction_id=?",
        (auction_id,),
        commit=True
    )
    
    # إرسال التنبيهات
    await event.answer("✅ تم البيع بنجاح!", alert=True)
    await event.client.send_message(
        buyer_id,
        f"✅ تم شراء الرقم +{auction[1]} بنجاح!\n"
        f"💰 السعر: {auction[5]}$\n\n"
        "تم تخزين الحساب في قسم الحسابات المخزنة"
    )

async def auction_list_handler(event):
    auctions = await execute_query(
        "SELECT * FROM auctions WHERE status='active'",
        fetchall=True
    )
    
    if not auctions:
        await event.answer("❌ لا توجد مزادات نشطة حالياً!", alert=True)
        return
    
    buttons = []
    for auction in auctions:
        buttons.append([
            Button.inline(
                f"+{auction[1]} - {auction[5]}$",  # phone - current_bid
                data=f"view_auction:{auction[0]}"
            )
        ])
    
    buttons.append([Button.inline("• رجوع • ↩️", data="auction_menu")])
    await event.edit("🏷️ المزادات النشطة:", buttons=buttons)

async def view_auction_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    
    if not auction:
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return
    
    seller_info = await get_user(auction[3])
    seller_name = seller_info.get('username', f"المستخدم {auction[3]}")
    
    text = (
        f"🏷️ **مزاد رقم:** +{auction[1]}\n"
        f"👤 البائع: {seller_name}\n"
        f"💰 أقل سعر: {auction[4]}$\n"
        f"🚀 أعلى عرض: {auction[5]}$\n\n"
        "اضغط لتقديم عرض جديد:"
    )
    
    await event.edit(
        text,
        buttons=[
            [Button.inline("تقديم عرض", data=f"bid:{auction_id}")],
            [Button.inline("• رجوع • ↩️", data="auction_list")]
        ]
    )

def register_handlers(client):
    """تسجيل جميع معالجات الأحداث للبوت"""
    # معالجات الأوامر الأساسية
    client.add_event_handler(start_handler, events.NewMessage(pattern='/start', func=lambda e: e.is_private))
    client.add_event_handler(admin_panel, events.NewMessage(pattern='/admin', func=lambda e: e.is_private))
    client.add_event_handler(control_menu, events.NewMessage(pattern='لوحة التحكم', func=lambda e: e.is_private))
    client.add_event_handler(back_button, events.NewMessage(pattern='رجوع', func=lambda e: e.is_private))
    client.add_event_handler(support_request, events.NewMessage(pattern='دعم', func=lambda e: e.is_private))
    client.add_event_handler(show_rules, events.NewMessage(pattern='القوانين', func=lambda e: e.is_private))
    client.add_event_handler(sell_price_handler, events.NewMessage(pattern='/sell_price', func=lambda e: e.is_private))
    
    # معالجات إدارة الحسابات والأرقام
    client.add_event_handler(add_number_menu, events.NewMessage(pattern='إضافة رقم', func=lambda e: e.is_private))
    client.add_event_handler(buy_number, events.NewMessage(pattern='شراء رقم', func=lambda e: e.is_private))
    client.add_event_handler(sell_account, events.NewMessage(pattern='بيع حساب', func=lambda e: e.is_private))
    client.add_event_handler(view_purchased_accounts, events.NewMessage(pattern='الحسابات المشتراة', func=lambda e: e.is_private))
    client.add_event_handler(view_sold_accounts, events.NewMessage(pattern='الحسابات المباعة', func=lambda e: e.is_private))
    client.add_event_handler(view_stored_accounts, events.NewMessage(pattern='الحسابات المخزنة', func=lambda e: e.is_private))
    client.add_event_handler(view_active_accounts, events.NewMessage(pattern='الحسابات النشطة', func=lambda e: e.is_private))
    client.add_event_handler(del_account_menu, events.NewMessage(pattern='حذف حساب', func=lambda e: e.is_private))
    client.add_event_handler(logout_account, events.NewMessage(pattern='تسجيل الخروج', func=lambda e: e.is_private))
    client.add_event_handler(select_account, events.NewMessage(pattern='اختيار حساب', func=lambda e: e.is_private))
    
    # معالجات الإعدادات
    client.add_event_handler(account_settings_menu, events.NewMessage(pattern='إعدادات الحساب', func=lambda e: e.is_private))
    client.add_event_handler(balance_settings, events.NewMessage(pattern='رصيدي', func=lambda e: e.is_private))
    client.add_event_handler(creation_settings_menu, events.NewMessage(pattern='إعدادات الإنشاء', func=lambda e: e.is_private))
    client.add_event_handler(posting_settings_menu, events.NewMessage(pattern='إعدادات النشر', func=lambda e: e.is_private))
    client.add_event_handler(force_settings, events.NewMessage(pattern='القنوات الإجبارية', func=lambda e: e.is_private))
    client.add_event_handler(super_settings_menu, events.NewMessage(pattern='القنوات السوبر', func=lambda e: e.is_private))
    client.add_event_handler(ban_settings, events.NewMessage(pattern='حظر مستخدم', func=lambda e: e.is_private))
    client.add_event_handler(num_settings, events.NewMessage(pattern='إعدادات الأرقام', func=lambda e: e.is_private))
    
    # معالجات الأزرار (Callbacks)
    client.add_event_handler(callback_handler, events.CallbackQuery())
    
    # معالجات العمليات المتقدمة
    client.add_event_handler(get_code_handler, events.NewMessage(pattern='الحصول على كود', func=lambda e: e.is_private))
    client.add_event_handler(add_country_handler, events.NewMessage(pattern='إضافة دولة', func=lambda e: e.is_private))
    client.add_event_handler(del_country_menu, events.NewMessage(pattern='حذف دولة', func=lambda e: e.is_private))
    client.add_event_handler(funding_handler, events.NewMessage(pattern='تمويل', func=lambda e: e.is_private))
    client.add_event_handler(transfer_balance, events.NewMessage(pattern='تحويل رصيد', func=lambda e: e.is_private))
    client.add_event_handler(withdraw_balance, events.NewMessage(pattern='سحب رصيد', func=lambda e: e.is_private))
    client.add_event_handler(confirm_withdraw, events.NewMessage(pattern='تأكيد السحب', func=lambda e: e.is_private))
    client.add_event_handler(cancel_operation, events.NewMessage(pattern='إلغاء العملية', func=lambda e: e.is_private))
    
    # معالجات عمليات الإنشاء
    client.add_event_handler(manual_group_creation_handler, events.NewMessage(pattern='إنشاء يدوي', func=lambda e: e.is_private))
    client.add_event_handler(auto_group_creation_handler, events.NewMessage(pattern='إنشاء تلقائي', func=lambda e: e.is_private))
    client.add_event_handler(stop_manual_creation_handler, events.NewMessage(pattern='إيقاف إنشاء يدوي', func=lambda e: e.is_private))
    client.add_event_handler(stop_auto_creation_handler, events.NewMessage(pattern='إيقاف إنشاء تلقائي', func=lambda e: e.is_private))
    
    # معالجات النشر
    client.add_event_handler(start_posting_handler, events.NewMessage(pattern='بدء النشر', func=lambda e: e.is_private))
    client.add_event_handler(stop_all_posting_handler, events.NewMessage(pattern='إيقاف كل النشرات', func=lambda e: e.is_private))
    client.add_event_handler(stop_posting_group_handler, events.NewMessage(pattern='إيقاف النشر في مجموعة', func=lambda e: e.is_private))
    client.add_event_handler(select_account_for_posting, events.NewMessage(pattern='اختيار حساب للنشر', func=lambda e: e.is_private))
    client.add_event_handler(select_channel_for_posting, events.NewMessage(pattern='اختيار قناة للنشر', func=lambda e: e.is_private))
    client.add_event_handler(ask_posting_settings, events.NewMessage(pattern='ضبط إعدادات النشر', func=lambda e: e.is_private))
    client.add_event_handler(add_posting_template_handler, events.NewMessage(pattern='إضافة قالب نشر', func=lambda e: e.is_private))
    client.add_event_handler(edit_posting_template_handler, events.NewMessage(pattern='تعديل قالب نشر', func=lambda e: e.is_private))
    client.add_event_handler(del_posting_template_handler, events.NewMessage(pattern='حذف قالب نشر', func=lambda e: e.is_private))
    
    # معالجات إدارة القنوات
    client.add_event_handler(add_force_channel_handler, events.NewMessage(pattern='إضافة قناة إجبارية', func=lambda e: e.is_private))
    client.add_event_handler(del_force_channel_handler, events.NewMessage(pattern='حذف قناة إجبارية', func=lambda e: e.is_private))
    client.add_event_handler(add_super_channel, events.NewMessage(pattern='إضافة قناة سوبر', func=lambda e: e.is_private))
    client.add_event_handler(delete_super_channel, events.NewMessage(pattern='حذف قناة سوبر', func=lambda e: e.is_private))
    
    # معالجات VIP والإدارة
    client.add_event_handler(add_vip_user, events.NewMessage(pattern='إضافة عضو VIP', func=lambda e: e.is_private))
    client.add_event_handler(remove_vip_user, events.NewMessage(pattern='إزالة عضو VIP', func=lambda e: e.is_private))
    client.add_event_handler(add_admin, events.NewMessage(pattern='إضافة مشرف', func=lambda e: e.is_private))
    client.add_event_handler(del_admin, events.NewMessage(pattern='حذف مشرف', func=lambda e: e.is_private))
    client.add_event_handler(ban_user, events.NewMessage(pattern='حظر مستخدم', func=lambda e: e.is_private))
    client.add_event_handler(unban_user, events.NewMessage(pattern='رفع حظر مستخدم', func=lambda e: e.is_private))
    
    # معالجات إضافية
    client.add_event_handler(store_account_handler, events.NewMessage(pattern='تخزين حساب', func=lambda e: e.is_private))
    client.add_event_handler(install_session_handler, events.NewMessage(pattern='تثبيت جلسة', func=lambda e: e.is_private))
    client.add_event_handler(sell_price_handler, events.NewMessage(pattern='تحديد سعر البيع', func=lambda e: e.is_private))
    client.add_event_handler(toggle_timed_name, events.NewMessage(pattern='تفعيل/تعطيل الاسم المؤقت', func=lambda e: e.is_private))
    client.add_event_handler(update_timed_name, events.NewMessage(pattern='تحديث الاسم المؤقت', func=lambda e: e.is_private))
    client.add_event_handler(join_mandatory_channel, events.NewMessage(pattern='الانضمام للقناة الإجبارية', func=lambda e: e.is_private))
    client.add_event_handler(broadcast_start, events.NewMessage(pattern='اذاعة عامة', func=lambda e: e.is_private))
    # في نهاية register_handlers()
    client.add_event_handler(install_menu_handler, events.NewMessage(pattern='قسم التنصيب', func=lambda e: e.is_private))
    client.add_event_handler(auction_menu_handler, events.NewMessage(pattern='قسم المزاد', func=lambda e: e.is_private))
    client.add_event_handler(add_auction_handler, events.NewMessage(pattern='إضافة مزاد', func=lambda e: e.is_private))
    client.add_event_handler(auction_list_handler, events.NewMessage(pattern='قائمة المزاد', func=lambda e: e.is_private))
    client.add_event_handler(view_auction_handler, events.NewMessage(pattern='عرض مزاد', func=lambda e: e.is_private))
    client.add_event_handler(place_bid_handler, events.NewMessage(pattern='تقديم عرض', func=lambda e: e.is_private))
    client.add_event_handler(sell_auction_handler, events.NewMessage(pattern='بيع بالمزاد', func=lambda e: e.is_private))
    client.add_event_handler(auction_logout_handler, events.NewMessage(pattern='تسجيل خروج المزاد', func=lambda e: e.is_private))
    print("✅ تم تسجيل جميع معالجات الأحداث بنجاح")