import asyncio

from telethon import Button, functions

from config import ADMIN_ID
from helpers import (
    award_referral_after_subscription,
    create_user,
    get_force_channels,
    get_referral_leaderboard,
    get_referral_stats,
    get_setting,
    get_user,
    is_admin,
    is_banned,
    record_referral_click,
    release_user_reservations,
    touch_user_activity,
)
from keyboards import (
    accounts_view_keyboard,
    balance_menu_keyboard,
    cancel_operation_keyboard,
    control_menu_keyboard,
    numbers_menu_keyboard,
    start_keyboard,
)
from messages import msgs

from .base import (
    active_conversations,
    enforce_rate_limit,
    pending_super_additions,
)


async def format_start_message(user_id):
    user_data = await get_user(user_id)
    dollar_balance = user_data['coins'] if user_data else 0
    points = user_data.get('referral_points', 0) if user_data else 0
    display_name = (user_data or {}).get('first_name') or (user_data or {}).get('username') or 'ضيفنا الكريم'
    from ui_config import refresh
    interface = await refresh()
    from html import escape
    return (
        interface['title']
        + f'\n\n👋 <b>أهلاً وسهلاً بك، {escape(str(display_name))}</b>\n\n'
        'مرحباً بك في منصة شهم، وجهتك لإدارة الحسابات والأرقام والاستفادة من الخدمات الرقمية بكل سهولة.\n\n'
        '👤 <b>معلومات حسابك:</b>\n'
        f'🆔 المعرّف: <code>{user_id}</code>\n'
        f'💰 رصيدك: <code>{escape(str(dollar_balance))} $</code>\n'
        f'⭐ نقاطك: <code>{escape(str(points))}</code>\n\n'
        '━━━━━━━━━━━━━━━━'
    )


async def start_handler(event):
    user_id = event.chat_id
    conversation = active_conversations.pop(user_id, None)
    if conversation:
        conversation.cancel()
    await release_user_reservations(user_id)
    if await is_banned(user_id):
        await event.respond(msgs['BANNED_MESSAGE'])
        return

    # Hide keyboards left by older bot versions before opening inline menus.
    await event.respond('تم فتح لوحة الأزرار داخل الرسائل.', buttons=Button.clear())

    existing_user = await get_user(user_id)

    start_arg = ""
    if event.raw_text:
        parts = event.raw_text.strip().split(maxsplit=1)
        start_arg = parts[1].strip() if len(parts) > 1 else ""

    if start_arg.startswith('web_'):
        from web_console import resume_handoff
        if await resume_handoff(event,start_arg):
            return

    # Record the first inviter before channel verification so the referral code
    # is retained when Telegram asks the user to send /start again afterwards.
    if not existing_user and start_arg.startswith("ref_"):
        try:
            await record_referral_click(user_id, int(start_arg[4:]))
        except ValueError:
            pass
    
    # التحقق من الاشتراك في القنوات الإجبارية
    from mandatory_subscription import verify_required_subscriptions
    subscribed, missing_channel = await verify_required_subscriptions(event.client, user_id)
    if not subscribed:
        await event.respond(f"**⚠️︙عذراً عزيزي يجب عليك الاشتراك بقناة البوت**\n🚀︙القناه: @{str(missing_channel).lstrip('@')}\n\n• اشترك في القناه ثم أرسل: /start")
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
    await touch_user_activity(user_id)

    # A reward is intentionally impossible unless at least one mandatory
    # channel was configured and all of its membership checks passed above.
    if await get_force_channels():
        referral_reward = await award_referral_after_subscription(user_id)
        if referral_reward:
            referrer_id, reward = referral_reward
            await event.client.send_message(
                referrer_id,
                f"🎉 أُضيف {reward}$ إلى رصيدك بعد انضمام المستخدم {user_id} للقناة الإلزامية. هذا الرصيد مخصص للشراء فقط.",
            )

    # تحضير الأزرار
    if start_arg == 'topup':
        from .payment_handlers import topup_menu
        return await topup_menu(event)
    if start_arg == 'balance':
        return await balance_menu(event)
    await event.respond(
        await format_start_message(user_id),
        buttons=start_keyboard(user_id, await is_admin(user_id)), parse_mode='html'
    )


async def numbers_menu(event):
    await event.edit(msgs['NUMBERS_MENU'], buttons=numbers_menu_keyboard(), parse_mode='markdown')


async def balance_menu(event):
    await event.edit(msgs['BALANCE_MENU'], buttons=balance_menu_keyboard(), parse_mode='markdown')


async def control_menu(event):
    await event.edit(
        msgs['CONTROL_MENU'],
        buttons=control_menu_keyboard(await is_admin(event.chat_id) or event.chat_id == ADMIN_ID),
        parse_mode='markdown',
    )


async def support_request(event, operation_reference=None):
    if operation_reference:
        from financial_statement import get_entry
        if event.chat_id != event.sender_id or not await get_entry(event.sender_id, operation_reference):
            return await event.answer("العملية غير موجودة ضمن حسابك.", alert=True)
    if not await enforce_rate_limit(event, "support_request", 10):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            prompt = f"أرسل استفسارك عن العملية #{operation_reference}:" if operation_reference else "أرسل رسالتك للدعم:"
            await conv.send_message(prompt, buttons=cancel_operation_keyboard())
            message = await conv.get_response(timeout=300)
            support_text = (f"العملية #{operation_reference}\n" if operation_reference else "") + (message.text or "")
            
            from operations import create_support_ticket
            ticket_id = await create_support_ticket(event.chat_id, support_text)
            await conv.send_message(f'حُفظت تذكرة الدعم #{ticket_id}. يمكنك متابعة الرسائل وحالتها من تذاكرك.',
                buttons=[[Button.inline('متابعة التذكرة', data=f'feature:tickets:view:{ticket_id}')]])
        except ValueError as exc:
            await conv.send_message(str(exc))
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def show_rules(event):
    rules = await get_setting("rules_message") or msgs['RULES_MESSAGE']
    await event.edit(rules, buttons=[[Button.inline("• رجوع • ↩️", data="main")]])


async def my_activity(event):
    from .statement_handlers import statement_menu
    await statement_menu(event)


async def audit_activity(event):
    from operations import get_user_activity
    activity = await get_user_activity(event.chat_id)
    if not activity:
        text = "🧾 لا يوجد نشاط مسجل بعد."
    else:
        lines = [f"• {created_at}: `{action}`" for action, created_at in activity]
        text = "🧾 **آخر نشاطاتك**\n\n" + "\n".join(lines)
    await event.edit(text, parse_mode="markdown", buttons=[[Button.inline("• رجوع • ↩️", data="my_activity")]])


async def referral_menu(event):
    user_id = event.chat_id
    bot_info = await event.client.get_me()
    if not getattr(bot_info, "username", None):
        await event.answer("تعذر إنشاء الرابط: لا يوجد معرف عام للبوت.", alert=True)
        return
    user_data = await get_user(user_id) or {}
    stats = await get_referral_stats(user_id)
    leaderboard = await get_referral_leaderboard(3)
    reward = await get_setting("referral_reward_amount") or "0"
    link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    leaderboard_text = "\n".join(
        f"{index}. ID: `{referrer_id}` — المشاركات: `{count}`"
        for index, (referrer_id, count) in enumerate(leaderboard, start=1)
    ) or "لا توجد مشاركات مسجلة حتى الآن."
    text = (
        "🎁 **نظام الإحالة**\n\n"
        f"رابطك الخاص:\n`{link}`\n\n"
        f"• رصيد الإحالات المتاح للشراء: `{user_data.get('referral_balance', 0)}$`\n"
        f"• الدعوات المسجلة: `{stats['invited']}`\n"
        f"• الدعوات المؤهلة للمكافأة: `{stats['rewarded']}`\n"
        f"• مكافأة الدعوة الواحدة: `{reward}$`\n\n"
        "🏆 **أكثر 3 مشاركات لرابط الإحالة**\n"
        f"{leaderboard_text}\n\n"
        "ملاحظة: المكافآت تُضاف تلقائياً عند تحقق الشروط، ويمكنك استخدامها للشراء من البوت."
    )
    await event.edit(text, parse_mode="markdown", buttons=[[Button.inline("• رجوع • ↩️", data="main")]])


async def cancel_operation(event):
    user_id = event.chat_id
    from .input_choices import clear_country
    clear_country(user_id)
    pending_super_additions.pop(user_id, None)
    conversation = active_conversations.pop(user_id, None)
    if conversation:
        conversation.cancel()
    await event.answer(msgs['OPERATION_CANCELLED'], alert=True)
    await main_menu(event)


async def main_menu(event):
    user_id = event.chat_id
    await event.edit(
        await format_start_message(user_id),
        buttons=start_keyboard(user_id, await is_admin(user_id)), parse_mode='html'
    )


async def accounts_view_menu(event):
    await event.edit(
        '📂 **✦ مـركـز حـسـابـاتـي ✦**\n\n'
        '━━━━━━━━━━━━━━━━\n\n'
        'كل ما يتعلق بحساباتك في مكان واحد. اختر القسم الذي تريد استعراضه.\n\n'
        '━━━━━━━━━━━━━━━━',
        buttons=accounts_view_keyboard(), parse_mode='markdown',
    )
