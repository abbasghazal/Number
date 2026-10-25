import asyncio
from decimal import Decimal

from telethon import Button

from config import ADMIN_ID
from helpers import (
    create_withdrawal_atomically,
    get_setting,
    get_user,
    log_audit_event,
    mark_withdrawal_paid_atomically,
    transfer_coins_atomically,
    withdrawable_balance,
)
from keyboards import balance_settings_keyboard, cancel_operation_keyboard
from messages import msgs

from .base import (
    active_conversations,
    ensure_admin,
    get_user_lock,
    parse_positive_amount,
)


async def balance_settings(event):
    await event.edit(
        msgs['ADMIN_BALANCE_SETTINGS'],
        buttons=balance_settings_keyboard()
    )


async def withdraw_balance(event):
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            user_data = await get_user(user_id)
            available_for_cash = withdrawable_balance(user_data)
            if available_for_cash < 1:
                await event.answer("الحد الأدنى للسحب هو 1$", alert=True)
                return

            await conv.send_message("أرسل رقم الكاش أو المحفظة:", buttons=cancel_operation_keyboard())
            cash_info = await conv.get_response(timeout=300)
            
            await conv.send_message("أدخل المبلغ المراد سحبه:", buttons=cancel_operation_keyboard())
            amount_info = await conv.get_response(timeout=300)
            
            try:
                amount = parse_positive_amount(amount_info.text)
                async with get_user_lock(user_id):
                    user_data = await get_user(user_id)
                    available_for_cash = withdrawable_balance(user_data)
                    if amount > available_for_cash:
                        await conv.send_message("رصيد الإحالات مخصص للشراء فقط ولا يمكن سحبه. الرصيد القابل للسحب غير كافٍ!")
                        return
                    request_id = await create_withdrawal_atomically(user_id, amount, cash_info.text.strip())
                    if not request_id:
                        await conv.send_message("تعذر إنشاء طلب السحب؛ تحقق من الرصيد وحاول لاحقاً.")
                        return
                    await log_audit_event(user_id, f"withdrawal_requested:{request_id}:{amount}")
                
                await event.client.send_message(
                    ADMIN_ID, 
                    f"• طلب سحب رصيد #{request_id}:\n- العضو: {user_id}\n- المبلغ: {amount}$\n- المحفظة: {cash_info.text}",
                    buttons=[[Button.inline("• مراجعة الطلب والصرف أو الرفض •", data=f"ops:withdraw:view:{request_id}")]]
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
            try:
                target_id = int(user_id_resp.text)
            except (TypeError, ValueError):
                await conv.send_message("ايدي غير صحيح!")
                return
            
            if event.chat_id == target_id:
                await conv.send_message("لا يمكن التحويل لنفسك!")
                return
            if not await get_user(target_id):
                await conv.send_message("العضو غير موجود!")
                return
            
            min_transfer = parse_positive_amount(
                await get_setting("transfer_minimum") or "5"
            )
            await conv.send_message(f"أدخل المبلغ (الحد الأدنى: {min_transfer}$):", buttons=cancel_operation_keyboard())
            amount_resp = await conv.get_response(timeout=300)
            try:
                amount = parse_positive_amount(amount_resp.text)
            except ValueError:
                await conv.send_message("مبلغ غير صحيح!")
                return
            
            if amount < min_transfer:
                await conv.send_message(f"الحد الأدنى: {min_transfer}$")
                return
            # تطبيق العمولة
            fee = (amount * Decimal("0.02")).quantize(Decimal("0.01"))
            total = amount + fee

            user_data = await get_user(event.chat_id)
            if total > withdrawable_balance(user_data):
                await conv.send_message("رصيد الإحالات مخصص للشراء فقط ولا يمكن تحويله. الرصيد القابل للتحويل غير كافٍ!")
                return
            
            first_id, second_id = sorted([event.chat_id, target_id])
            async with get_user_lock(first_id):
                async with get_user_lock(second_id):
                    if not await transfer_coins_atomically(event.chat_id, target_id, amount, fee=fee):
                        await conv.send_message("رصيد الأرقام غير كافٍ أو تعذر إتمام التحويل!")
                        return
            
            await conv.send_message(f"✅ تم تحويل {amount}$ للعضو {target_id}")
            await event.client.send_message(target_id, f"استلمت تحويل بقيمة {amount}$ من {event.chat_id}")
            await event.client.send_message(ADMIN_ID, f"• تحويل رصيد:\n- من: {event.chat_id}\n- إلى: {target_id}\n- المبلغ: {amount}$\n- العمولة: {fee}$")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def confirm_withdraw(event, data):
    if not await ensure_admin(event):
        return
    parts = data.split('_')
    user_id = int(parts[2])
    request_id = int(parts[3]) if len(parts) > 3 else 0
    if request_id:
        paid_user_id = await mark_withdrawal_paid_atomically(request_id, event.chat_id)
        if paid_user_id is None:
            await event.answer("تمت معالجة هذا الطلب سابقاً أو أنه غير صالح.", alert=True)
            return
        await log_audit_event(event.chat_id, f"withdrawal_paid:{request_id}")
        user_id = paid_user_id
    else:
        return await event.answer('هذا الزر القديم لا يحتوي رقم طلب صالحًا. استخدم مراجعة السحب.', alert=True)
    await event.client.send_message(user_id, "✅ تم تحويل الرصيد بنجاح!")
    await event.edit("تم إرسال التأكيد للعضو!")
