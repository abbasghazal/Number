from telethon import Button

from helpers import (
    get_joined_channels_for_session,
    get_stored_accounts,
    leave_all_channels_with_session,
    leave_all_groups_with_session,
    leave_channel_with_session,
)
from keyboards import arrange_buttons

from .base import user_leave_channel_accounts, user_leave_channel_targets, user_leave_group_accounts
from .common import get_owned_stored_account


async def leave_groups_account_menu(event):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    item_buttons = [
        Button.inline(f"+{account[0]}", data=f"leave_groups_account:{account[0]}")
        for account in accounts
    ]
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_account")])
    await event.edit("📱 اختر الحساب الذي تريد مغادرة الكروبات منه:", buttons=buttons)


async def leave_groups_account_selected(event, data):
    user_id = event.chat_id
    phone = data.split(":", 1)[1]
    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    user_leave_group_accounts[user_id] = phone
    await event.edit(
        f"⚠️ تأكيد مغادرة جميع الكروبات للحساب +{phone}؟",
        buttons=[
            [Button.inline("• تأكيد مغادرة الكروبات • ✅", data="leave_all_groups_execute")],
            [Button.inline("• إلغاء • ↩️", data="control_settings_account")]
        ]
    )


async def leave_all_groups_execute(event):
    user_id = event.chat_id
    phone = user_leave_group_accounts.get(user_id)
    if not phone:
        await event.answer("❌ اختر الحساب أولاً!", alert=True)
        return

    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ الحساب المحدد لم يعد متاحاً!", alert=True)
        return

    await event.edit(f"⏳ جاري مغادرة جميع الكروبات للحساب +{phone} ...")
    success_count, failed_count = await leave_all_groups_with_session(account[1])
    user_leave_group_accounts.pop(user_id, None)
    await event.edit(
        f"✅ انتهت عملية مغادرة الكروبات للحساب +{phone}\n\n"
        f"• تم مغادرة: {success_count}\n"
        f"• فشل: {failed_count}",
        buttons=[[Button.inline("• رجوع • ↩️", data="control_settings_account")]]
    )


async def leave_channels_account_menu(event):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    item_buttons = [
        Button.inline(f"+{account[0]}", data=f"leave_channels_account:{account[0]}")
        for account in accounts
    ]
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_account")])
    await event.edit("📱 اختر الحساب الذي تريد تنفيذ مغادرة القنوات عليه:", buttons=buttons)


async def leave_channels_account_selected(event, data):
    user_id = event.chat_id
    phone = data.split(":", 1)[1]
    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    user_leave_channel_accounts[user_id] = phone
    user_leave_channel_targets.pop(user_id, None)
    await event.edit(
        f"🚪 الحساب المحدد: +{phone}\n\nاختر العملية:",
        buttons=[
            [Button.inline("• مغادرة قناة معيّنة •", data="leave_specific_channel_menu")],
            [Button.inline("• مغادرة جميع القنوات •", data="leave_all_channels_confirm")],
            [Button.inline("• رجوع • ↩️", data="leave_channels_menu")]
        ]
    )


async def leave_specific_channel_menu(event):
    user_id = event.chat_id
    phone = user_leave_channel_accounts.get(user_id)
    if not phone:
        await event.answer("❌ اختر الحساب أولاً!", alert=True)
        return

    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ الحساب المحدد لم يعد متاحاً!", alert=True)
        return

    await event.edit(f"⏳ جاري جلب القنوات للحساب +{phone} ...")
    channels = await get_joined_channels_for_session(account[1])
    if not channels:
        await event.edit(
            f"❌ لا توجد قنوات أو مجموعات سوبر في الحساب +{phone}.",
            buttons=[[Button.inline("• رجوع • ↩️", data=f"leave_channels_account:{phone}")]]
        )
        return

    user_leave_channel_targets[user_id] = channels
    item_buttons = []
    for index, channel in enumerate(channels):
        title = channel["title"]
        if len(title) > 32:
            title = title[:29] + "..."
        item_buttons.append(Button.inline(title, data=f"leave_specific_channel:{index}"))

    buttons = arrange_buttons(item_buttons, pattern=(1,))
    buttons.append([Button.inline("• رجوع • ↩️", data=f"leave_channels_account:{phone}")])
    await event.edit(
        f"📢 اختر القناة التي تريد مغادرتها من الحساب +{phone}:",
        buttons=buttons
    )


async def leave_specific_channel_selected(event, data):
    user_id = event.chat_id
    phone = user_leave_channel_accounts.get(user_id)
    if not phone:
        await event.answer("❌ اختر الحساب أولاً!", alert=True)
        return

    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ الحساب المحدد لم يعد متاحاً!", alert=True)
        return

    try:
        index = int(data.split(":", 1)[1])
        channel = user_leave_channel_targets.get(user_id, [])[index]
    except (ValueError, IndexError):
        await event.answer("❌ بيانات القناة غير صالحة!", alert=True)
        return

    await event.edit(f"⏳ جاري مغادرة: {channel['title']}")
    success = await leave_channel_with_session(account[1], channel["entity"])
    if success:
        user_leave_channel_targets.pop(user_id, None)
        await event.edit(
            f"✅ غادر الحساب +{phone} القناة:\n{channel['title']}",
            buttons=[[Button.inline("• رجوع • ↩️", data=f"leave_channels_account:{phone}")]]
        )
    else:
        await event.edit(
            "❌ فشلت مغادرة القناة. تأكد أن الجلسة صالحة وأن الحساب عضو في القناة.",
            buttons=[[Button.inline("• رجوع • ↩️", data=f"leave_channels_account:{phone}")]]
        )


async def leave_all_channels_confirm(event):
    user_id = event.chat_id
    phone = user_leave_channel_accounts.get(user_id)
    if not phone:
        await event.answer("❌ اختر الحساب أولاً!", alert=True)
        return

    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ الحساب المحدد لم يعد متاحاً!", alert=True)
        return

    await event.edit(
        f"⚠️ تأكيد مغادرة جميع القنوات والمجموعات السوبر للحساب +{phone}؟",
        buttons=[
            [Button.inline("• تأكيد المغادرة • ✅", data="leave_all_channels_execute")],
            [Button.inline("• إلغاء • ↩️", data=f"leave_channels_account:{phone}")]
        ]
    )


async def leave_all_channels_execute(event):
    user_id = event.chat_id
    phone = user_leave_channel_accounts.get(user_id)
    if not phone:
        await event.answer("❌ اختر الحساب أولاً!", alert=True)
        return

    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ الحساب المحدد لم يعد متاحاً!", alert=True)
        return

    await event.edit(f"⏳ جاري مغادرة جميع القنوات للحساب +{phone} ...")
    success_count, failed_count = await leave_all_channels_with_session(account[1])
    user_leave_channel_targets.pop(user_id, None)
    await event.edit(
        f"✅ انتهت العملية للحساب +{phone}\n\n"
        f"• تم مغادرة: {success_count}\n"
        f"• فشل: {failed_count}",
        buttons=[[Button.inline("• رجوع • ↩️", data="control_settings_account")]]
    )
