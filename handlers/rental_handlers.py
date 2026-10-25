import asyncio
from decimal import Decimal

from telethon import Button

from helpers import (
    get_available_rental_inventory_count,
    get_setting,
    rent_available_accounts,
    set_setting,
)
from keyboards import cancel_operation_keyboard

from .base import active_conversations, enforce_rate_limit, ensure_admin


async def rent_numbers_handler(event):
    user_id = event.chat_id
    if not await enforce_rate_limit(event, "rent_numbers", 10):
        return
    try:
        daily_price = Decimal(str(await get_setting("rental_daily_price") or "0"))
        max_days = int(await get_setting("rental_max_days") or "30")
    except (ValueError, ArithmeticError):
        return await event.answer("❌ إعدادات التأجير غير صالحة. راجع المطور.", alert=True)
    if daily_price <= 0:
        return await event.answer("❌ خدمة التأجير غير مفعّلة حالياً.", alert=True)
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                f"سعر الرقم: {daily_price}$ يومياً. أرسل عدد الأرقام الذي تريد تأجيره (عدد موجب).",
                buttons=cancel_operation_keyboard(),
            )
            count = int((await conv.get_response(timeout=300)).text.strip())
            await conv.send_message(f"أرسل مدة التأجير بالأيام (1–{max_days}):", buttons=cancel_operation_keyboard())
            days = int((await conv.get_response(timeout=300)).text.strip())
            if count < 1 or not 1 <= days <= max_days:
                await conv.send_message("❌ العدد يجب أن يكون موجباً أو المدة خارج الحدود المسموحة.")
                return
            # There is deliberately no configured per-user cap.  Availability
            # is the only practical bound and the transaction rechecks it.
            available = await get_available_rental_inventory_count(user_id)
            if count > available:
                await conv.send_message(f"❌ العدد المطلوب غير متاح حالياً. المتاح الآن: {available} رقم.")
                return
            from commerce import rental_quote, price_text
            quote = await rental_quote(user_id, count, days)
            await conv.send_message(price_text(quote) + '\nاكتب نعم لتأكيد التأجير، أو إلغاء للعودة.',
                                    buttons=cancel_operation_keyboard())
            if (await conv.get_response(timeout=300)).text.strip() != 'نعم':
                return await conv.send_message('تم إلغاء التأجير.')
            daily_price = quote['base'] / count / days
            phones, result = await rent_available_accounts(user_id, count, days, daily_price, quote['quote_id'])
            if not phones:
                message = "رصيدك غير كافٍ." if result == "insufficient_balance" else "لا توجد أرقام كافية متاحة حالياً."
                await conv.send_message(f"❌ {message}")
                return
            await conv.send_message(
                f"✅ تم تأجير {len(phones)} رقم لمدة {days} يوم/أيام مقابل {result}$ .\n"
                "الأرقام مشتركة بين المستأجرين ومتاحة للرشق والانضمام فقط؛ لا يمكن جلب الكود أو التخزين أو الدخول لإعداداتها.",
                buttons=[[Button.inline("• عرض الحسابات المستأجرة • ⏳", data="view_rented")]],
            )
        except ValueError as exc:
            await conv.send_message(str(exc))
        except asyncio.TimeoutError:
            await conv.send_message("❌ انتهى الوقت أو أُرسلت قيمة غير صحيحة.")
        finally:
            active_conversations.pop(user_id, None)


async def rental_settings_handler(event):
    if not await ensure_admin(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            current = await get_setting("rental_daily_price") or "1"
            await conv.send_message(f"السعر الحالي {current}$ للرقم/اليوم. أرسل السعر الجديد:", buttons=cancel_operation_keyboard())
            value = Decimal((await conv.get_response(timeout=300)).text.strip())
            if value <= 0 or value > Decimal("1000"):
                raise ValueError
            await set_setting("rental_daily_price", str(value.quantize(Decimal("0.01"))))
            await conv.send_message("✅ تم تحديث سعر التأجير اليومي.")
        except (ValueError, ArithmeticError, asyncio.TimeoutError):
            await conv.send_message("❌ قيمة السعر غير صالحة.")
        finally:
            active_conversations.pop(event.chat_id, None)
