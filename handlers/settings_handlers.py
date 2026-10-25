import asyncio
from decimal import Decimal, InvalidOperation

from telethon import Button

from helpers import (
    add_force_channel,
    add_super_channel,
    delete_super_channel,
    execute_query,
    get_countries,
    get_country,
    get_force_channels,
    get_setting,
    get_stored_accounts,
    get_super_channels,
    join_super_target_with_stored_account,
    normalize_super_target,
    remove_force_channel,
    save_super_account_target,
    set_setting,
)
from keyboards import (
    arrange_buttons,
    cancel_operation_keyboard,
    force_settings_keyboard,
    super_settings_keyboard,
)
from messages import msgs

from .base import (
    active_conversations,
    ensure_admin,
    get_user_lock,
    logger,
    parse_positive_amount,
    pending_super_additions,
    price_country_pages,
)
from .common import edit_or_respond


async def change_price_country_menu(event, mode):
    from .number_handlers import pricing_menu
    return await pricing_menu(event)


async def change_buy_price_menu(event):
    from .number_handlers import pricing_menu
    return await pricing_menu(event)


async def change_sell_price_menu(event):
    from .number_handlers import pricing_menu
    return await pricing_menu(event)


async def next_price_countries_page(event, mode):
    user_id = event.chat_id
    countries = await get_countries()
    if not countries:
        await event.answer("⭕️ لا توجد دول متاحة حالياً", alert=True)
        return

    page_key = (user_id, mode)
    current_page = price_country_pages.get(page_key, 0)
    max_page = max(0, (len(countries) - 1) // 20)
    if current_page >= max_page:
        await event.answer("❗️ هذه آخر صفحة", alert=True)
        return

    price_country_pages[page_key] = current_page + 1
    await change_price_country_menu(event, mode)


async def prev_price_countries_page(event, mode):
    user_id = event.chat_id
    countries = await get_countries()
    if not countries:
        await event.answer("⭕️ لا توجد دول متاحة حالياً", alert=True)
        return

    page_key = (user_id, mode)
    current_page = price_country_pages.get(page_key, 0)
    if current_page <= 0:
        await event.answer("❗️ هذه أول صفحة", alert=True)
        return

    price_country_pages[page_key] = current_page - 1
    await change_price_country_menu(event, mode)


async def change_buy_price(event, data):
    from .number_handlers import pricing_menu
    return await pricing_menu(event)


async def change_sell_price(event, data):
    from .number_handlers import pricing_menu
    return await pricing_menu(event)


async def force_settings(event):
    if not await ensure_admin(event):
        return
    channels = await get_force_channels()
    channels_text = "\n".join(f"• @{channel.lstrip('@')}" for channel in channels) or "• لا توجد قنوات مضافة حالياً."
    await event.edit(
        f"{msgs['ADMIN_FORCE_SETTINGS']}\n\n• القنوات الحالية ({len(channels)}):\n{channels_text}",
        buttons=force_settings_keyboard()
    )


async def super_settings_menu(event):
    await event.edit(msgs['SUPER_MENU'], buttons=super_settings_keyboard())


async def add_super_channel_handler(event):
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                "أرسل رابط أو معرف المجموعة/القناة السوبر:\n"
                "• @username\n"
                "• https://t.me/username\n"
                "• https://t.me/+invite\n"
                "• ID رقمي",
                buttons=cancel_operation_keyboard()
            )
            channel_resp = await conv.get_response(timeout=300)
            channel_id = normalize_super_target(channel_resp.text)

            await conv.send_message("أرسل عنوان القناة:", buttons=cancel_operation_keyboard())
            title_resp = await conv.get_response(timeout=300)
            title = title_resp.text.strip()

            stored_accounts = await get_stored_accounts(user_id)
            if not stored_accounts:
                await conv.send_message("❌ لا توجد حسابات مخزنة للتحقق من السوبر والانضمام إليه!")
                return
            pending_super_additions[user_id] = {"target": channel_id, "title": title}
            buttons = [
                [Button.inline(f"+{account[0]}", data=f"super_join_account:{index}")]
                for index, account in enumerate(stored_accounts)
            ]
            buttons.append([Button.inline("• جميع الحسابات •", data="super_join_all")])
            buttons.append(cancel_operation_keyboard()[0])
            await conv.send_message(
                "اختر الحساب الذي تريد أن ينضم إلى السوبر، أو اختر جميع الحسابات:",
                buttons=buttons,
            )
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


async def join_selected_accounts_to_super(event, data):
    user_id = event.chat_id
    async with get_user_lock(("super_join", user_id)):
        pending = pending_super_additions.pop(user_id, None)
        if not pending:
            await event.answer("انتهت عملية الإضافة. أرسل رابط السوبر من جديد.", alert=True)
            return
        accounts = await get_stored_accounts(user_id)
        if data == "super_join_all":
            selected = accounts
        else:
            try:
                selected = [accounts[int(data.split(":", 1)[1])]]
            except (ValueError, IndexError):
                await event.answer("الحساب المحدد لم يعد متاحًا.", alert=True)
                return
        if not selected:
            await event.answer("لا توجد حسابات مخزنة.", alert=True)
            return

        joined = []
        failed = []
        for account in selected:
            try:
                entity = await join_super_target_with_stored_account(account[1], pending["target"])
                await save_super_account_target(
                    user_id, account[0], pending["target"], entity,
                )
                joined.append(str(account[0]))
            except Exception:
                logger.exception("Account %s could not join super target", account[0])
                failed.append(str(account[0]))

        if joined:
            await add_super_channel(user_id, pending["target"], pending["title"])
        result = [f"تم انضمام {len(joined)} من أصل {len(selected)} حساب."]
        if joined:
            result.append("الحسابات الناجحة: " + ", ".join(f"+{phone}" for phone in joined))
        if failed:
            result.append("الحسابات التي فشلت: " + ", ".join(f"+{phone}" for phone in failed))
        await event.edit("\n".join(result), buttons=super_settings_keyboard())


async def show_super_channels(event):
    channels = await get_super_channels(event.chat_id)
    if not channels:
        await event.answer("❌ لا توجد قنوات سوبر مضافّة!", alert=True)
        return
    
    text = "📢 القنوات السوبر المضافّة:\n\n"
    for i, channel in enumerate(channels, 1):
        text += f"{i}. {channel[1]} (ID: {channel[0]})\n"
    
    await event.answer(text, alert=True)


async def clear_super_channels(event):
    await execute_query("DELETE FROM user_super_channels WHERE user_id=?", (event.chat_id,), commit=True)
    await event.answer("✅ تم حذف جميع القنوات السوبر!", alert=True)


async def del_super_channel_handler(event):
    channels = await get_super_channels(event.chat_id)
    if not channels:
        await event.answer("❌ لا توجد قنوات سوبر!", alert=True)
        return
    
    item_buttons = []
    for index, channel in enumerate(channels):
        item_buttons.append(Button.inline(channel[1], data=f"del_super_channel:{index}"))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_super")])
    await event.edit("اختر القناة لحذفها:", buttons=buttons)


async def handle_delete_super_channel(event, data):
    index_text = data.split(':', 1)[1]
    channels = await get_super_channels(event.chat_id)
    try:
        channel = channels[int(index_text)]
    except (TypeError, ValueError, IndexError):
        await event.answer("❌ بيانات السوبر غير صالحة!", alert=True)
        return
    channel_id = channel[0]
    await delete_super_channel(event.chat_id, channel_id)
    await event.answer("✅ تم حذف السوبر بنجاح!", alert=True)
    await super_settings_menu(event)


async def add_mandatory_channel_handler(event):
    if not await ensure_admin(event):
        return
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


async def add_force_channel_handler(event):
    if not await ensure_admin(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل معرف القناة لإضافتها (مثال: @channel أو channel):", buttons=cancel_operation_keyboard())
            channel = await conv.get_response(timeout=300)
            channel_id = channel.text.strip().replace("https://t.me/", "").replace("http://t.me/", "").strip("@/")
            if not channel_id:
                await conv.send_message("❌ معرف القناة غير صالح!")
                return
            await add_force_channel(channel_id)
            await conv.send_message("✅ تم إضافة القناة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def del_force_channel_handler(event):
    if not await ensure_admin(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل معرف القناة المراد حذفها:", buttons=cancel_operation_keyboard())
            channel = await conv.get_response(timeout=300)
            channel_id = channel.text.strip().replace("https://t.me/", "").replace("http://t.me/", "").strip("@/")
            if not channel_id:
                await conv.send_message("❌ معرف القناة غير صالح!")
                return
            await remove_force_channel(channel_id)
            await conv.send_message("✅ تم حذف القناة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def set_referral_reward_handler(event):
    """Update the purchase-only balance awarded for each successful referral."""
    if not await ensure_admin(event):
        return
    current_reward = await get_setting("referral_reward_amount") or "0"
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(
                f"أرسل قيمة مكافأة الإحالة الجديدة بالدولار (القيمة الحالية: {current_reward}$).\n"
                "أرسل 0 لإيقاف المكافأة.",
                buttons=cancel_operation_keyboard(),
            )
            response = await conv.get_response(timeout=300)
            reward = Decimal(response.text.strip())
            if not reward.is_finite() or reward < 0:
                raise ValueError
            reward = reward.quantize(Decimal("0.01"))
            await set_setting("referral_reward_amount", str(reward))
            await conv.send_message(f"✅ تم تحديث مكافأة الإحالة إلى {reward}$ لكل إحالة مكتملة.")
        except (InvalidOperation, ValueError, TypeError):
            await conv.send_message("❌ أدخل قيمة رقمية صحيحة تساوي أو أكبر من 0.")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            active_conversations.pop(event.chat_id, None)


async def set_trust_channel(event):
    if not await ensure_admin(event):
        return
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
            except Exception:
                logger.exception("Failed to validate trust channel")
                await conv.send_message("❌ فشل في تعيين القناة! تأكد من رفع البوت كمسؤول")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def edit_rules(event):
    if not await ensure_admin(event):
        return
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
