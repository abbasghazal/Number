"""Payment order UI and safe, idempotent order completion helpers."""
import asyncio
from utils.asyncio_compat import to_thread
import json
import hashlib
import logging
import secrets
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from urllib.parse import quote
from urllib.request import Request, urlopen
from telethon import functions, types

from config import (
    PAYMENT_STARS_PER_CREDIT, PAYMENT_TON_PER_CREDIT,
    TON_API_KEY, TON_API_PROVIDER, TON_WALLET_ADDRESS,
)
from helpers import create_user, execute_query, get_setting, set_setting
from keyboards import Button, cancel_operation_keyboard, payment_amount_keyboard, payment_methods_keyboard, payment_settings_keyboard
from postgres import DatabaseError, connect

logger = logging.getLogger(__name__)
pending_credit_choices = {}


async def _payment_setting(name, fallback):
    value = await get_setting(name)
    return value if value not in (None, "") else fallback


async def payment_settings_menu(event):
    from .base import ensure_admin
    if not await ensure_admin(event, 'finance'):
        return
    stars_rate = await _payment_setting("payment_stars_per_credit", str(PAYMENT_STARS_PER_CREDIT))
    ton_wallet = await _payment_setting("payment_ton_wallet", TON_WALLET_ADDRESS)
    ton_rate = await _payment_setting("payment_ton_per_credit", PAYMENT_TON_PER_CREDIT)
    await event.edit(
        "• إعدادات الدفع\n\n"
        f"- 1$ = {stars_rate} نجمة\n"
        f"- محفظة TON: {ton_wallet or 'غير محددة'}\n"
        f"- 1$ = {ton_rate} TON\n\n"
        "مفتاح TON API يحفظ في ملف البيئة فقط.",
        buttons=payment_settings_keyboard(),
    )


async def edit_payment_setting(event, key):
    from .base import ensure_admin
    if not await ensure_admin(event, 'finance'):
        return
    prompts = {
        "payment_stars_per_credit": "أرسل عدد النجوم مقابل 1$ (مثال: 1):",
        "payment_ton_wallet": "أرسل عنوان محفظة TON المستلمة:",
        "payment_ton_per_credit": "أرسل سعر 1$ بعملة TON (مثال: 0.5):",
    }
    if key not in prompts:
        return
    async with event.client.conversation(event.chat_id) as conv:
        try:
            await conv.send_message(prompts[key], buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            value = response.raw_text.strip()
            if key == "payment_stars_per_credit":
                if not value.isdigit() or int(value) < 1 or int(value) > 10_000_000:
                    raise ValueError
            elif key == "payment_ton_per_credit":
                if Decimal(value) <= 0 or Decimal(value) > Decimal("100000"):
                    raise ValueError
            elif not value or len(value) > 200:
                raise ValueError
            await set_setting(key, value)
            await conv.send_message("✅ تم حفظ الإعداد. يطبق على الفواتير الجديدة مباشرة.")
        except ValueError:
            await conv.send_message("❌ القيمة غير صحيحة.")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى وقت الإدخال.")


def _amount(value):
    normalized = str(value).strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,"))
    normalized = normalized.replace(",", ".").replace("$", "").strip()
    try:
        parsed = Decimal(normalized)
    except (InvalidOperation, ValueError):
        raise ValueError("invalid amount")
    if not parsed.is_finite() or parsed <= 0 or parsed > Decimal("100000"):
        raise ValueError("invalid amount")
    return parsed.quantize(Decimal("0.01"))


def _stars_amount(credits, stars_per_credit):
    """Return an invoice amount that never rounds a fractional charge down."""
    try:
        rate = int(str(stars_per_credit))
    except (TypeError, ValueError):
        raise ValueError("invalid stars rate")
    if rate < 1 or rate > 10_000_000:
        raise ValueError("invalid stars rate")
    return int((Decimal(credits) * rate).to_integral_value(rounding=ROUND_CEILING))


async def topup_menu(event):
    await event.edit(
        "• شحن الرصيد\n\nاختر مبلغًا جاهزًا أو أدخل مبلغًا آخر بالدولار.",
        buttons=payment_amount_keyboard(),
    )


async def set_topup_amount(event, raw_amount):
    try:
        credits = _amount(raw_amount)
    except ValueError:
        await event.answer("المبلغ غير صحيح.", alert=True)
        return
    pending_credit_choices[event.chat_id] = credits
    await event.edit(f"المبلغ: {credits}$\nاختر وسيلة الدفع:", buttons=payment_methods_keyboard())


async def ask_custom_topup(event):
    try:
        async with event.client.conversation(event.chat_id) as conv:
            await conv.send_message("أرسل المبلغ بالدولار، مثال: 1 أو 1.5", buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            credits = _amount(response.raw_text)
    except asyncio.TimeoutError:
        await event.client.send_message(event.chat_id, "⏱ انتهى وقت إدخال المبلغ.")
        return
    except ValueError:
        await event.client.send_message(event.chat_id, "❌ المبلغ غير صحيح. استخدم رقمًا مثل 1 أو 1.5.")
        return
    pending_credit_choices[event.chat_id] = credits
    await event.client.send_message(event.chat_id, f"المبلغ: {credits}$\nاختر وسيلة الدفع:", buttons=payment_methods_keyboard())


async def _new_order(user_id, method, credits, amount, currency, wallet=None):
    order_id = secrets.token_urlsafe(12)
    await create_user(user_id)
    await execute_query(
        "INSERT INTO payment_orders (order_id, user_id, method, credits, amount, currency, wallet) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (order_id, user_id, method, credits, amount, currency, wallet), commit=True,
    )
    return order_id


async def choose_payment_method(event, method):
    if method not in {"stars", "ton"}:
        await event.answer("وسيلة الدفع غير متاحة.", alert=True)
        return

    credits = pending_credit_choices.pop(event.chat_id, None)
    if credits is None:
        await event.answer("أدخل مبلغ الشحن أولاً.", alert=True)
        return

    if method == "stars":
        try:
            stars = _stars_amount(
                credits,
                await _payment_setting("payment_stars_per_credit", str(PAYMENT_STARS_PER_CREDIT)),
            )
        except ValueError:
            await event.answer("إعداد سعر Stars غير صحيح. راجع الإدارة.", alert=True)
            return
        order_id = await _new_order(event.chat_id, "stars", credits, stars, "XTR")
        invoice = types.InputMediaInvoice(
            title="شحن رصيد البوت",
            description=f"إضافة {credits}$ إلى رصيدك",
            invoice=types.Invoice(currency="XTR", prices=[types.LabeledPrice("شحن رصيد", stars)]),
            payload=order_id.encode(), provider_data=types.DataJSON(data="{}"), provider="",
            start_param="topup",
        )
        try:
            await event.client(functions.messages.SendMediaRequest(peer=event.chat_id, media=invoice, message=""))
        except Exception as exc:
            # Telegram RPC errors (for example an invalid Stars invoice or a
            # payment restriction on the bot) otherwise escape the callback
            # handler without a useful production log entry.
            logger.exception(
                "Telegram Stars invoice creation failed: user_id=%s order_id=%s credits=%s stars=%s error=%s",
                event.chat_id,
                order_id,
                credits,
                stars,
                exc,
            )
            await event.answer("تعذر إنشاء فاتورة Stars. تم تسجيل الخطأ للمشرف.", alert=True)
            return
        await event.edit("تم إرسال فاتورة Stars. يُضاف الرصيد تلقائياً بعد الدفع.")
        return

    if method == "ton":
        ton_wallet = await _payment_setting("payment_ton_wallet", TON_WALLET_ADDRESS)
        if not ton_wallet:
            await event.edit("محفظة TON غير مهيأة بعد.")
            return
        try:
            rate = Decimal(await _payment_setting("payment_ton_per_credit", PAYMENT_TON_PER_CREDIT))
            nanoton = int(
                (credits * rate * Decimal("1000000000")).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            if nanoton < 1:
                raise ValueError
        except (InvalidOperation, ValueError):
            await event.edit("سعر TON غير صحيح. عدّله من إعدادات الدفع.")
            return
        order_id = await _new_order(event.chat_id, "ton", credits, nanoton, "TON", ton_wallet)
        # `ton://` is a wallet URI scheme, not a website. Telegram Desktop and
        # browsers may try to resolve it as a DNS name; Tonkeeper's HTTPS
        # universal link works in browsers and then opens a compatible wallet.
        url = (
            f"https://app.tonkeeper.com/transfer/{quote(ton_wallet, safe='')}"
            f"?amount={nanoton}&text={quote(order_id, safe='')}"
        )
        await event.edit(
            f"فاتورة TON: `{order_id}`\nالمبلغ: `{Decimal(nanoton) / Decimal('1000000000')} TON`\n"
            f"المحفظة: `{ton_wallet}`\n\nادفع من محفظتك وسيُشحن الرصيد تلقائياً بعد التأكيد.",
            parse_mode="markdown", buttons=[[Button.url("دفع عبر محفظة TON", url)]],
        )


def _toncenter_transactions(wallet, api_key, cursor=None):
    url = "https://toncenter.com/api/v2/getTransactions?address=" + quote(wallet, safe="") + "&limit=50&archival=true"
    if cursor:
        url += "&lt=" + quote(str(cursor['lt']), safe='') + "&hash=" + quote(cursor['hash'], safe='')
    request = Request(url, headers={"X-API-Key": api_key, "Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok", True):
        raise RuntimeError('TON provider rejected transaction lookup')
    result = payload.get("result", [])
    # getTransactionsStd returns an object on some TON Center API versions
    # and a direct list on others.
    if isinstance(result, dict):
        result = result.get("transactions", [])
    return result if isinstance(result, list) else []


def _ton_payment_parts(transaction):
    if not isinstance(transaction, dict):
        return "", 0, ""
    message = transaction.get("in_msg") or {}
    data = message.get("msg_data") or {}
    comment = message.get("message") or message.get("comment") or data.get("text") or ""
    value = message.get("value") or message.get("amount")
    tx_id = (transaction.get("transaction_id") or {}).get("hash") or transaction.get("hash")
    try:
        return str(comment).strip(), int(value), str(tx_id)
    except (TypeError, ValueError):
        return "", 0, ""


async def monitor_ton_payments(client):
    """Resume historical pagination after downtime; never age out unpaid orders."""
    current_wallet = await _payment_setting("payment_ton_wallet", TON_WALLET_ADDRESS)
    if not TON_API_KEY or TON_API_PROVIDER != "toncenter":
        return
    orders = await execute_query(
        "SELECT order_id, wallet, EXTRACT(EPOCH FROM created_at) FROM payment_orders WHERE method='ton' AND status='pending'",
        fetchall=True,
    ) or []
    wallets = {}
    for order_id, wallet, created in orders:
        wallet = wallet or current_wallet
        if wallet:
            wallets.setdefault(wallet, []).append((order_id, created))
    for wallet, pending in wallets.items():
        from service_health import tick
        tick('payments')
        key = "ton_scan:" + hashlib.sha256(wallet.encode()).hexdigest()
        raw = await get_setting(key)
        cursor = json.loads(raw) if raw else None
        transactions = await to_thread(_toncenter_transactions, wallet, TON_API_KEY, cursor)
        order_ids = {row[0] for row in pending}
        for transaction in transactions:
            order_id, amount, tx_id = _ton_payment_parts(transaction)
            if order_id not in order_ids or not tx_id:
                continue
            ok, user_id = await complete_payment(order_id, tx_id, amount=amount, currency="TON")
            if not ok:
                if user_id == 'order mismatch':
                    continue
                # Do not advance past a payment whose database write failed.
                raise RuntimeError("Could not reconcile TON payment")
            if user_id != "already completed":
                try:
                    await client.send_message(user_id, "✅ تم تأكيد دفعة TON وشحن رصيدك تلقائياً.")
                except Exception:
                    logger.warning("TON credited but notification failed")
        next_cursor = None
        if transactions:
            last = transactions[-1]
            tx = last.get("transaction_id") or {}
            oldest_order = min(float(created) for _, created in pending)
            if len(transactions) >= 50 and float(last.get("utime", 0)) >= oldest_order:
                if not tx.get('lt') or not tx.get('hash'):
                    raise RuntimeError("TON pagination cursor missing")
                next_cursor = {'lt': str(tx['lt']), 'hash': tx['hash']}
                if next_cursor == cursor:
                    raise RuntimeError("TON pagination did not advance")
        await set_setting(key, json.dumps(next_cursor) if next_cursor else "")


def _complete_payment_sync(order_id, transaction_id, amount=None, currency=None, payer_id=None):
    if not order_id or not transaction_id:
        return False, "missing identifiers"
    with connect() as db:
            row = db.execute(
                "SELECT user_id, credits, amount, currency, status FROM payment_orders WHERE order_id=? FOR UPDATE",
                (order_id,),
            ).fetchone()
            if not row:
                return False, "unknown order"
            user_id, credits, expected_amount, expected_currency, status = row
            if payer_id is not None and int(user_id) != int(payer_id):
                return False, "payer mismatch"
            if status == "completed":
                return True, "already completed"
            if status != "pending" or (amount is not None and int(amount) != expected_amount) or (currency and currency != expected_currency):
                return False, "order mismatch"
            if db.execute("UPDATE payment_orders SET status='completed', provider_transaction_id=?, completed_at=CURRENT_TIMESTAMP WHERE order_id=? AND status='pending'", (transaction_id, order_id)).rowcount != 1:
                return False, "already processed"
            if db.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (credits, user_id)).rowcount != 1:
                db.rollback()
                return False, "user missing"
            balance = db.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
            db.execute(
                "INSERT INTO balance_ledger (user_id, amount, balance_after, operation, reference) VALUES (?, ?, ?, ?, ?)",
                (user_id, credits, balance, "payment_completed", order_id),
            )
            db.execute("INSERT INTO audit_log (user_id, action) VALUES (?, ?)", (user_id, f"payment_completed:{order_id}"))
            db.commit()
            return True, user_id


async def complete_payment(order_id, transaction_id, *, amount=None, currency=None, payer_id=None):
    """Credit an order exactly once after a trusted provider confirmation."""
    try:
        return await to_thread(_complete_payment_sync, order_id, transaction_id, amount, currency, payer_id)
    except DatabaseError:
        logger.exception("Payment completion failed")
        return False, "database error"


async def handle_star_precheckout(client, update):
    """Approve only a valid pending Stars order before Telegram's deadline."""
    if not isinstance(update, types.UpdateBotPrecheckoutQuery):
        return

    success = False
    error = "الفاتورة غير صالحة أو انتهت صلاحيتها. أنشئ فاتورة جديدة."
    try:
        order_id = update.payload.decode("utf-8")
        from stars_purchase import PREFIX, precheckout_sync
        if order_id.startswith(PREFIX):
            success = await asyncio.wait_for(to_thread(
                precheckout_sync, order_id[len(PREFIX):], update.user_id, update.total_amount, update.currency), timeout=7)
            await client(functions.messages.SetBotPrecheckoutResultsRequest(
                query_id=update.query_id, success=success,
                error=None if success else 'انتهى الحجز أو لم يعد الرقم متاحاً. اختر رقمًا مجدداً.'))
            return
        row = await execute_query(
            "SELECT amount, currency, status FROM payment_orders "
            "WHERE order_id=? AND user_id=? AND method='stars'",
            (order_id, update.user_id),
            fetchone=True,
        )
        if row and row[1] == "XTR" and row[2] == "pending" and int(row[0]) == update.total_amount and update.currency == "XTR":
            success = True
        else:
            logger.warning(
                "Rejected Telegram Stars pre-checkout: user_id=%s order_id=%s currency=%s amount=%s",
                update.user_id, order_id, update.currency, update.total_amount,
            )
    except Exception:
        logger.exception("Failed to validate Telegram Stars pre-checkout: query_id=%s", update.query_id)
        error = "تعذر التحقق من الفاتورة. حاول مرة أخرى لاحقاً."

    try:
        await client(functions.messages.SetBotPrecheckoutResultsRequest(
            query_id=update.query_id,
            success=success,
            error=None if success else error,
        ))
    except Exception:
        logger.exception("Failed to answer Telegram Stars pre-checkout: query_id=%s", update.query_id)


async def handle_star_payment_update(client, update):
    """Process Telegram's MessageService payment confirmation update.

    Telethon deliberately excludes MessageService objects from NewMessage
    events, so successful Stars payments must be received through Raw events.
    """
    if not isinstance(update, types.UpdateNewMessage):
        return
    action = getattr(update.message, "action", None)
    if not isinstance(action, types.MessageActionPaymentSentMe) or action.currency != "XTR":
        return
    try:
        order_id = action.payload.decode("utf-8")
        transaction_id = action.charge.id
    except Exception:
        logger.exception("Received an unreadable Telegram Stars payment confirmation")
        return
    from stars_purchase import PREFIX, receive
    payer = getattr(update.message, 'from_id', None)
    # Incoming private service messages may omit from_id; Telegram then
    # identifies the sender via peer_id. Never infer the payer from the order.
    if payer is None:
        payer = getattr(update.message, 'peer_id', None)
    if getattr(update.message, 'out', False) or not isinstance(payer, types.PeerUser):
        logger.warning('Stars payer unavailable; deferring to transaction reconciliation: order=%s', order_id)
        return
    if order_id.startswith(PREFIX):
        try:
            await receive(client, order_id[len(PREFIX):], payer.user_id, transaction_id, action.total_amount)
        except Exception:
            logger.exception('Direct Stars payment deferred for reconciliation: order=%s', order_id)
        return
    ok, result = await complete_payment(order_id, transaction_id, amount=action.total_amount,
                                        currency="XTR", payer_id=payer.user_id)
    if ok and result != "already completed":
        await client.send_message(result, "✅ تم شحن رصيدك تلقائياً بنجاح.")
    elif not ok:
        logger.error(
            "Telegram Stars payment was received but not credited: order_id=%s transaction_id=%s reason=%s",
            order_id, transaction_id, result,
        )
