"""Panels for user-paid mandatory channel subscriptions."""
import asyncio
from decimal import Decimal, InvalidOperation

from telethon import Button
from telethon.tl import functions, types

import mandatory_subscription as service
from config import DEVELOPER_ID
from keyboards import arrange_buttons, cancel_operation_keyboard

from .base import active_conversations


def _money(value):
    return f"{Decimal(str(value)).quantize(Decimal('0.01'))}$"


async def _owned_channel(event, raw_channel):
    channel_id = str(raw_channel or "").strip().replace("https://t.me/", "").replace("http://t.me/", "").strip("@/").lower()
    if not channel_id or "/" in channel_id or " " in channel_id:
        raise ValueError("أرسل معرّف قناة عاماً صحيحاً مثل @mychannel.")
    try:
        entity = await event.client.get_entity(channel_id)
        if not isinstance(entity, types.Channel) or getattr(entity, "megagroup", False):
            raise ValueError
        me = await event.client.get_me()
        bot_participant = await event.client(functions.channels.GetParticipantRequest(entity, me.id))
        owner_participant = await event.client(functions.channels.GetParticipantRequest(entity, event.sender_id))
        accepted = {"ChannelParticipantAdmin", "ChannelParticipantCreator"}
        if bot_participant.participant.__class__.__name__ not in accepted:
            return None, None, "اجعل البوت مشرفاً في القناة مع صلاحية النشر أولاً."
        if owner_participant.participant.__class__.__name__ not in accepted:
            return None, None, "يجب أن تكون مشرفاً أو مالكاً للقناة التي تضيفها."
        return channel_id, getattr(entity, "title", None) or channel_id, None
    except ValueError:
        raise ValueError("أرسل معرّف قناة عاماً صحيحاً مثل @mychannel.") from None
    except Exception:
        return None, None, "تعذر التحقق من القناة. تأكد أنها عامة وأنك والبوت مشرفان فيها."


async def _choose_channel(event, package, *, count_mode):
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            if hasattr(event, "mark_input_started"):
                event.mark_input_started()
            await conv.send_message(
                "أرسل معرف القناة العامة الآن، مثل: @mychannel\n\n"
                "يجب أن تكون مشرفاً فيها، وأن يكون البوت مشرفاً بصلاحية النشر.",
                buttons=cancel_operation_keyboard(),
            )
            response = await conv.get_response(timeout=300)
            channel_id, title, error = await _owned_channel(event, response.text)
            if error:
                await conv.send_message(f"❌ {error}")
                return
            ok, result, _subscription_id = await service.purchase(
                user_id, channel_id, title, package, count_mode=count_mode,
            )
            if not ok:
                await conv.send_message(f"❌ {result}")
                return
            if count_mode:
                service.start_count_delivery(event.client, _subscription_id)
                details = f"لباقة {int(package)} مشترك"
            else:
                label = service.TIME_PACKAGES[package][0]
                details = f"لمدة {label} فقط، دون ضمان عدد معيّن من الأعضاء"
            await conv.send_message(
                f"✅ تم تفعيل الاشتراك الإجباري لقناة @{channel_id} {details}.\n"
                f"الرصيد المتبقي: {_money(result)}"
            )
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى وقت إدخال القناة.")
        finally:
            if active_conversations.get(user_id) is conv:
                active_conversations.pop(user_id, None)


async def _user_menu(event):
    channels = await service.active_user_channels(event.chat_id)
    text = (
        "📢 **✦ تـمـويـل اشـتـراك قـنـاتـك ✦**\n\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "أضف قناتك إلى خدمة الاشتراك الإجباري المتاحة للمستخدمين.\n\n"
        "📌 **الخيارات:**\n\n"
        "👥 الاشتراك حسب العدد.\n"
        "🕒 الاشتراك حسب المدة.\n\n"
        "💰 سيظهر السعر المطلوب قبل تأكيد الاشتراك.\n"
        f"📋 قنواتك النشطة حالياً: `{len(channels)}`\n\n"
        "━━━━━━━━━━━━━━━━\n👇 اختر نوع الطلب للمتابعة."
    )
    buttons = [
        Button.inline("اشتراك بالوقت ⏱", data="mandatory:time"),
        Button.inline("اشتراك بالعدد 👥", data="mandatory:count"),
        Button.inline("قنواتي النشطة 📋", data="mandatory:mine"),
        Button.inline("رجوع ↩️", data="main"),
    ]
    await event.edit(text, buttons=arrange_buttons(buttons, pattern=(2, 1, 1)), parse_mode='markdown')


async def _time_menu(event):
    prices = await service.time_prices()
    buttons = [
        Button.inline(f"{label} — {_money(prices[key])}", data=f"mandatory:buytime:{key}")
        for key, (label, _hours, _default) in service.TIME_PACKAGES.items()
    ]
    buttons.append(Button.inline("رجوع ↩️", data="mandatory:menu"))
    await event.edit(
        "⏱ اختر مدة الاشتراك الإجباري لقناتك:\n\n"
        "تنبيه: باقة الوقت لا تضمن عدداً محدداً من الأعضاء؛ هي تفعيل للقناة خلال المدة فقط.",
        buttons=arrange_buttons(buttons, pattern=(2, 2, 1)),
    )


async def _count_menu(event):
    prices = await service.count_prices()
    if not prices:
        await event.edit(
            "👥 لا توجد باقات عدد متاحة حالياً. سيضيف المطور الأسعار قريباً.",
            buttons=arrange_buttons([Button.inline("رجوع ↩️", data="mandatory:menu")]),
        )
        return
    buttons = [
        Button.inline(f"{int(count)} مشترك — {_money(price)}", data=f"mandatory:buycount:{int(count)}")
        for count, price in prices
    ]
    buttons.append(Button.inline("رجوع ↩️", data="mandatory:menu"))
    await event.edit(
        "👥 اختر باقة العدد المطلوبة لقناتك. يمكنك اختيار أي باقة من الأسعار التي أضافها المطور.\n"
        "سيبدأ تنفيذ الباقة تلقائياً حتى يكتمل العدد.",
        buttons=arrange_buttons(buttons, pattern=(2,) * ((len(prices) + 1) // 2) + ((1,) if len(prices) % 2 == 0 else ())),
    )


async def _my_channels(event):
    channels = await service.active_user_channels(event.chat_id)
    if not channels:
        await event.edit("📋 لا توجد لديك قنوات اشتراك إجباري نشطة.", buttons=arrange_buttons([Button.inline("رجوع ↩️", data="mandatory:menu")]))
        return
    lines = ["📋 قنواتي النشطة:\n"]
    buttons = []
    for subscription_id, channel, title, mode, price, expires_at, limit, used in channels:
        if mode == "time":
            status = f"حتى {expires_at:%Y-%m-%d %H:%M}" if expires_at else "نشطة"
        else:
            status = f"{int(used)}/{int(limit)} مشترك"
        lines.append(f"• @{channel} — {status} — {_money(price)}")
        buttons.append(Button.inline(f"إيقاف @{channel}", data=f"mandatory:stop:{int(subscription_id)}"))
    buttons.append(Button.inline("رجوع ↩️", data="mandatory:menu"))
    await event.edit("\n".join(lines), buttons=arrange_buttons(buttons, pattern=(1,) * len(buttons)))


async def _prices_menu(event):
    if event.sender_id != DEVELOPER_ID:
        await event.answer("هذه الإعدادات للمطور فقط.", alert=True)
        return
    prices = await service.time_prices()
    counts = await service.count_prices()
    text = "💳 أسعار الاشتراك الإجباري المدفوع\n\nأسعار الوقت:\n"
    text += "\n".join(f"• {label}: {_money(prices[key])}" for key, (label, _hours, _default) in service.TIME_PACKAGES.items())
    text += "\n\nأسعار العدد (يمكن إضافة أكثر من باقة):\n" + ("\n".join(f"• {int(count)} مشترك: {_money(price)}" for count, price in counts) or "• لا توجد باقات")
    buttons = [
        Button.inline(label, data=f"mandatory:settime:{key}")
        for key, (label, _hours, _default) in service.TIME_PACKAGES.items()
    ]
    buttons.append(Button.inline("إضافة سعر عدد ➕", data="mandatory:addcount"))
    for count, _price_value in counts:
        buttons.append(Button.inline(f"تعديل {int(count)} مشترك", data=f"mandatory:setcount:{int(count)}"))
        buttons.append(Button.inline(f"حذف {int(count)} مشترك", data=f"mandatory:delcount:{int(count)}"))
    buttons.append(Button.inline("رجوع ↩️", data="ajxkho"))
    await event.edit(text, buttons=arrange_buttons(buttons, pattern=(2, 2, 1) + (2,) * len(counts) + (1,)))


async def _ask_time_price(event, package):
    if event.sender_id != DEVELOPER_ID or package not in service.TIME_PACKAGES:
        return
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            if hasattr(event, "mark_input_started"):
                event.mark_input_started()
            await conv.send_message(f"أرسل السعر الجديد لباقـة {service.TIME_PACKAGES[package][0]} بالدولار:", buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            value = await service.set_time_price(package, response.text.strip())
            await conv.send_message(f"✅ تم تحديث السعر إلى {_money(value)}.")
        except (ValueError, InvalidOperation):
            await conv.send_message("❌ أرسل سعراً موجباً صحيحاً.")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى وقت الإدخال.")
        finally:
            if active_conversations.get(user_id) is conv:
                active_conversations.pop(user_id, None)


async def _ask_count_price(event, count=None):
    if event.sender_id != DEVELOPER_ID:
        return
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            if hasattr(event, "mark_input_started"):
                event.mark_input_started()
            prompt = "أرسل العدد ثم السعر بهذا الشكل: 100 15" if count is None else f"أرسل السعر الجديد لباقة {count} مشترك:"
            await conv.send_message(prompt, buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            parts = response.text.split()
            if count is None:
                if len(parts) != 2:
                    raise ValueError
                count, value = parts
            else:
                value = response.text.strip()
            count, value = await service.set_count_price(count, value)
            await conv.send_message(f"✅ تم حفظ باقة {count} مشترك بسعر {_money(value)}.")
        except (ValueError, InvalidOperation):
            await conv.send_message("❌ البيانات غير صحيحة. استخدم عدداً موجباً وسعراً موجباً.")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى وقت الإدخال.")
        finally:
            if active_conversations.get(user_id) is conv:
                active_conversations.pop(user_id, None)


async def menu(event, data):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"
    if action == "menu":
        return await _user_menu(event)
    if action == "time":
        return await _time_menu(event)
    if action == "count":
        return await _count_menu(event)
    if action == "mine":
        return await _my_channels(event)
    if action == "buytime" and len(parts) == 3:
        if parts[2] not in service.TIME_PACKAGES:
            raise ValueError("باقة الوقت غير صالحة.")
        return await _choose_channel(event, parts[2], count_mode=False)
    if action == "buycount" and len(parts) == 3:
        return await _choose_channel(event, int(parts[2]), count_mode=True)
    if action == "stop" and len(parts) == 3:
        removed = await service.remove_owned_subscription(event.chat_id, int(parts[2]))
        await event.answer("✅ تم إيقاف القناة." if removed else "القناة غير متاحة.", alert=True)
        return await _my_channels(event)
    if action == "prices":
        return await _prices_menu(event)
    if action == "settime" and len(parts) == 3:
        return await _ask_time_price(event, parts[2])
    if action == "addcount":
        return await _ask_count_price(event)
    if action == "setcount" and len(parts) == 3:
        return await _ask_count_price(event, int(parts[2]))
    if action == "delcount" and len(parts) == 3:
        if event.sender_id != DEVELOPER_ID:
            return
        await service.delete_count_price(int(parts[2]))
        await event.answer("✅ تم حذف باقة العدد.", alert=True)
        return await _prices_menu(event)
    raise ValueError("خيار الاشتراك الإجباري غير صالح.")
