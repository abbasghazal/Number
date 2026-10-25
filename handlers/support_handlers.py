import asyncio

from keyboards import cancel_operation_keyboard

from .base import active_conversations, ensure_admin


async def reply_to_user(event, data):
    if not await ensure_admin(event):
        return
    try:
        parts = data.split('_')
        user_id = int(parts[1])
        ticket_id = int(parts[2]) if len(parts) > 2 else 0
    except (IndexError, ValueError):
        await event.answer("بيانات الرد غير صالحة.", alert=True)
        return
    if not ticket_id:
        return await event.answer('افتح التذكرة من لوحة الدعم للرد مع حفظ المحادثة.', alert=True)
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ردك:", buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            if ticket_id:
                from support_center import detail, reply
                item = await detail(event.sender_id, ticket_id)
                if item['owner'] != user_id:
                    raise ValueError('صاحب التذكرة لا يطابق الزر.')
                await reply(event.sender_id, ticket_id, response.text)
            await conv.send_message("✅ تم إرسال الرد!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def close_ticket(event, data):
    if not await ensure_admin(event):
        return
    try:
        ticket_id = int(data.rsplit('_', 1)[1])
    except (IndexError, ValueError):
        await event.answer("بيانات التذكرة غير صالحة.", alert=True)
        return
    if not ticket_id:
        await event.answer("تعذر تحديد التذكرة.", alert=True)
        return
    from operations import close_support_ticket
    await close_support_ticket(ticket_id, event.chat_id)
    await event.edit(f"✅ أُغلقت تذكرة الدعم #{ticket_id}.")
