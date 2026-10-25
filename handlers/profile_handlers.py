import asyncio

from telethon import Button, functions
from session_guard import TelegramClient
from telethon.sessions import StringSession

from config import API_HASH, API_ID
from helpers import (
    add_active_account,
    get_stored_accounts,
    remove_active_account,
    remove_timed_name,
    set_timed_name_active,
    update_timed_name,
)
from keyboards import account_settings_keyboard, arrange_buttons, cancel_operation_keyboard
from messages import msgs
from utils.privacy import mask_phone
from display_names import random_bios, random_display_names

from .base import active_conversations, enforce_rate_limit, logger
from .common import choose_control_accounts, get_owned_stored_account


async def toggle_timed_name(event, active, selected_accounts=None):
    user_id = event.chat_id
    # Account updates can outlive Telegram's callback-query window.  Clear the
    # button spinner up front and report the result by editing the message.
    try:
        await event.answer()
    except Exception as exc:
        logger.debug("Could not acknowledge timed-name callback: %s", exc)
    
    # الحصول على الحسابات المخزنة
    stored_accounts = selected_accounts
    if stored_accounts is None:
        stored_accounts = await choose_control_accounts(
            event, f"timed_name_{'on' if active else 'off'}",
            "اختر الحسابات لتطبيق الاسم الوقتي عليها", "control_settings_account"
        )
        if stored_accounts is None:
            return
    if not stored_accounts:
        return await event.edit("❌ لا توجد حسابات مخزنة!", buttons=account_settings_keyboard())
    
    if active:
        # تفعيل الاسم الوقتي
        await set_timed_name_active(user_id, True)
        for account in stored_accounts:
            await update_timed_name(account[1])
            await add_active_account(account[0], account[1], user_id, "تحديث الاسم الوقتي")
        await event.edit(msgs['TIMED_NAME_ACTIVATED'], buttons=account_settings_keyboard())
    else:
        # إيقاف الاسم الوقتي
        await set_timed_name_active(user_id, False)
        for account in stored_accounts:
            await remove_timed_name(account[1])
            await remove_active_account(account[0])
        await event.edit(msgs['TIMED_NAME_DEACTIVATED'], buttons=account_settings_keyboard())


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
                    client = TelegramClient(StringSession(session), API_ID, API_HASH, actor_id=event.chat_id, account_phone=account[0], exclusive_session=True)
                    await client.connect()
                    await client.upload_profile_photo(await photo.download_media())
                    await client.disconnect()
                except Exception:
                    logger.warning("Failed to update profile photo for %s", mask_phone(account[0]))
            
            await conv.send_message("✅ تم تحديث الصورة الشخصية لجميع الحسابات المخزنة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def change_random_names_for_all_accounts(event):
    """Assign a different random display name to every account the user owns."""
    user_id = event.chat_id
    if not await enforce_rate_limit(event, "change_random_name", 30):
        return
    # A callback query expires quickly.  Acknowledge it before connecting to
    # every account, otherwise Telethon's delayed implicit answer raises
    # QueryIdInvalidError after the updates have already started.
    try:
        await event.answer()
    except Exception as exc:
        logger.debug("Could not acknowledge name-change callback: %s", exc)
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.edit("❌ لا توجد حسابات مخزنة!", buttons=account_settings_keyboard())
        return

    names = random_display_names(len(accounts))
    success = 0
    failed = []
    await event.edit("⏳ جارٍ تغيير أسماء الحسابات، قد تستغرق العملية بعض الوقت…")
    for account, display_name in zip(accounts, names):
        phone, session = account[0], account[1]
        client = TelegramClient(StringSession(session), API_ID, API_HASH, actor_id=event.chat_id, account_phone=account[0], exclusive_session=True)
        try:
            await client.connect()
            await client(functions.account.UpdateProfileRequest(first_name=display_name, last_name=""))
            success += 1
        except Exception as exc:
            logger.warning("Failed to update random display name for %s: %s", mask_phone(phone), exc)
            failed.append(phone)
        finally:
            if client.is_connected():
                await client.disconnect()

    message = f"✅ تم تغيير اسم {success} من أصل {len(accounts)} حساباً عشوائياً."
    if failed:
        message += f"\n❌ تعذر تحديث {len(failed)} حساباً بسبب قيود تيليجرام أو جلسة غير صالحة."
    await event.edit(message, buttons=account_settings_keyboard())


async def change_random_bios_for_all_accounts(event):
    """Assign a different random bio to every account the user owns."""
    user_id = event.chat_id
    if not await enforce_rate_limit(event, "change_random_bio", 30):
        return
    try:
        await event.answer()
    except Exception as exc:
        logger.debug("Could not acknowledge bio-change callback: %s", exc)
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.edit("❌ لا توجد حسابات مخزنة!", buttons=account_settings_keyboard())
        return
    await event.edit("⏳ جارٍ تغيير بايو الحسابات، قد تستغرق العملية بعض الوقت…")
    success = 0
    failed = []
    for account, bio in zip(accounts, random_bios(len(accounts))):
        phone, session = account[0], account[1]
        client = TelegramClient(StringSession(session), API_ID, API_HASH, actor_id=event.chat_id, account_phone=account[0], exclusive_session=True)
        try:
            await client.connect()
            await client(functions.account.UpdateProfileRequest(about=bio))
            success += 1
        except Exception as exc:
            logger.warning("Failed to update random bio for %s: %s", mask_phone(phone), exc)
            failed.append(phone)
        finally:
            if client.is_connected():
                await client.disconnect()
    message = f"✅ تم تغيير بايو {success} من أصل {len(accounts)} حساباً عشوائياً."
    if failed:
        message += f"\n❌ تعذر تحديث {len(failed)} حساباً بسبب قيود تيليجرام أو جلسة غير صالحة."
    await event.edit(message, buttons=account_settings_keyboard())


def profile_edit_action_text(action):
    labels = {
        "bio": "تغيير البايو",
        "username": "تغيير اسم المستخدم",
    }
    return labels.get(action, "تعديل الحساب")


async def profile_edit_account_menu(event, action):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    item_buttons = [
        Button.inline(f"+{account[0]}", data=f"profile_edit_account:{action}:{account[0]}")
        for account in accounts
    ]
    buttons = arrange_buttons(item_buttons)
    if len(accounts) > 1:
        buttons.append([Button.inline("• جميع الحسابات • 👥", data=f"profile_edit_account:{action}:all")])
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_account")])
    await event.edit(
        f"📱 اختر الحساب لتنفيذ: {profile_edit_action_text(action)}",
        buttons=buttons
    )


async def profile_edit_account_selected(event, data):
    parts = data.split(":", 2)
    if len(parts) != 3:
        await event.answer("❌ بيانات العملية غير صالحة!", alert=True)
        return

    action, phone = parts[1], parts[2]
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id) if phone == "all" else [await get_owned_stored_account(user_id, phone)]
    accounts = [account for account in accounts if account]
    if not accounts:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    if action == "bio":
        await change_bio_for_accounts(event, accounts)
    elif action == "username":
        await change_username_for_accounts(event, accounts)
    else:
        await event.answer("❌ نوع العملية غير معروف!", alert=True)


async def change_bio_for_account(event, account):
    await change_bio_for_accounts(event, [account])


async def change_bio_for_accounts(event, accounts):
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            scope = "جميع الحسابات المحددة" if len(accounts) > 1 else f"الحساب +{accounts[0][0]}"
            await conv.send_message(f"أرسل البايو الجديد لـ {scope}:", buttons=cancel_operation_keyboard())
            bio = await conv.get_response(timeout=300)
            if not bio.text:
                await conv.send_message("❌ البايو غير صالح.")
                return

            success = 0
            for phone, session, *_ in accounts:
                client = TelegramClient(StringSession(session), API_ID, API_HASH, actor_id=event.chat_id, account_phone=phone, exclusive_session=True)
                try:
                    await client.connect()
                    await client(functions.account.UpdateProfileRequest(about=bio.text.strip()))
                    success += 1
                except Exception as e:
                    logger.warning("Failed to update bio for %s: %s", mask_phone(phone), e)
                finally:
                    if client.is_connected():
                        await client.disconnect()
            await conv.send_message(f"✅ تم تحديث البايو لـ {success} من أصل {len(accounts)} حساب.")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


async def change_username_for_account(event, account):
    await change_username_for_accounts(event, [account])


async def change_username_for_accounts(event, accounts):
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            if len(accounts) > 1:
                await conv.send_message("❌ لا يمكن تعيين اسم مستخدم واحد لجميع الحسابات، لأن تيليجرام يفرض أن يكون كل اسم مستخدم فريداً.")
                return
            phone, session = accounts[0][0], accounts[0][1]
            await conv.send_message(f"أرسل اسم المستخدم الجديد للحساب +{phone} بدون @:", buttons=cancel_operation_keyboard())
            username = await conv.get_response(timeout=300)
            new_username = (username.text or "").strip().lstrip("@")
            if not new_username:
                await conv.send_message("❌ اسم المستخدم غير صالح.")
                return

            client = TelegramClient(StringSession(session), API_ID, API_HASH, actor_id=event.chat_id, account_phone=phone, exclusive_session=True)
            try:
                await client.connect()
                await client(functions.account.UpdateUsernameRequest(new_username))
                await conv.send_message(f"✅ تم تحديث اسم المستخدم للحساب +{phone} إلى @{new_username}.")
            except Exception:
                logger.warning("Failed to update username for %s", mask_phone(phone))
                await conv.send_message(f"❌ فشل تحديث اسم المستخدم للحساب +{phone}.")
            finally:
                if client.is_connected():
                    await client.disconnect()
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


async def enable_stealth_mode(event):
    await set_presence(event,True)


async def disable_stealth_mode(event):
    await set_presence(event,False)


async def enable_notifications(event):
    await set_notice_preference(event,True)


async def disable_notifications(event):
    await set_notice_preference(event,False)


async def set_notice_preference(event,enabled):
    from workflow import query
    await query('INSERT INTO platform_preferences(user_id,notifications) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET notifications=excluded.notifications',(event.chat_id,enabled),commit=True)
    await event.answer('فُعلت إشعارات العمليات.' if enabled else 'أُوقفت رسائل إشعارات العمليات؛ يبقى السجل متاحًا في لوحة التحكم.',alert=True)


async def set_presence(event,offline):
    import account_access
    import automation
    from workflow import query
    success=0
    accounts=await account_access.accounts(event.chat_id,owned_only=True)
    for account in accounts:
        try:
            if not await account_access.authorized(event.chat_id,account,owned_only=True):
                continue
            async with automation.connection(account) as client:
                if not await account_access.authorized(event.chat_id,account,owned_only=True):
                    continue
                await client(functions.account.UpdateStatusRequest(offline=offline))
            success+=1
        except Exception:
            logger.warning('Presence update deferred for %s',mask_phone(account[0]))
    await query('INSERT INTO platform_preferences(user_id,stealth) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET stealth=excluded.stealth',(event.chat_id,offline),commit=True)
    await event.answer(f'حُفظ تفضيل الظهور. نجح تحديث الحالة الآن لـ {success}/{len(accounts)} حساب. تيليجرام قد يعيد إظهار الاتصال عند استخدام الحساب.',alert=True)
