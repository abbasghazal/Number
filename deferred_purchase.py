"""Reserve a rented number, settle only after every existing lease ends."""
import logging
from decimal import Decimal

from postgres import connect
from utils.asyncio_compat import to_thread
from workflow import enqueue, query

logger = logging.getLogger(__name__)


async def rental_end(phone):
    row = await query("SELECT MAX(ends_at) FROM rental_leases WHERE phone=? AND status='active' AND ends_at>CURRENT_TIMESTAMP",
                      (phone,), fetchone=True)
    return row[0] if row else None


def reserve_sync(user, phone, quote):
    with connect() as db:
        balance = db.execute('SELECT coins,referral_balance FROM users WHERE user_id=? FOR UPDATE', (user,)).fetchone()
        db.execute('SELECT phone FROM accounts WHERE phone=? FOR UPDATE', (phone,))
        existing = db.execute("SELECT user_id,quote_id,funds_reserved,amount,referral_amount FROM deferred_purchases WHERE phone=? AND status='pending' FOR UPDATE", (phone,)).fetchone()
        if existing:
            if existing[0] == user:
                if not existing[2]:
                    referral = min(Decimal(str(balance[1] or 0)), Decimal(str(existing[3]))) if balance else Decimal(0)
                    held = db.execute("""UPDATE users SET coins=coins-?,
                        referral_balance=GREATEST(0,referral_balance-?)
                        WHERE user_id=? AND coins>=? RETURNING coins""",
                        (existing[3], referral, user, existing[3])).fetchone()
                    if not held:
                        raise ValueError('رصيدك غير كافٍ لحجز هذا الرقم.')
                    db.execute("UPDATE deferred_purchases SET funds_reserved=TRUE,referral_amount=? WHERE quote_id=?",
                               (referral, existing[1]))
                    db.execute("""INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,fee)
                        VALUES(?,?,?,?,?,0)""",
                        (user, -existing[3], held[0], 'deferred_purchase_reserved', existing[1]))
                return True
            raise ValueError('الرقم محجوز لمشترٍ آخر.')
        end = db.execute("SELECT MAX(ends_at) FROM rental_leases WHERE phone=? AND status='active' AND ends_at>CURRENT_TIMESTAMP", (phone,)).fetchone()[0]
        if not end:
            return False
        pending = db.execute('SELECT quote_id,price FROM pending_purchases WHERE user_id=? AND phone=? FOR UPDATE', (user, phone)).fetchone()
        valid = db.execute("SELECT 1 FROM commerce_quotes WHERE quote_id=? AND user_id=? AND status='pending' AND expires_at>CURRENT_TIMESTAMP", (quote, user)).fetchone()
        holder = db.execute('SELECT user_id,star_order_id,reserved_until>CURRENT_TIMESTAMP FROM account_reservations WHERE phone=? FOR UPDATE', (phone,)).fetchone()
        if not pending or pending[0] != quote or not valid or not holder or holder[0] != user or holder[1] or not holder[2]:
            raise ValueError('انتهى عرض الشراء؛ اختر الرقم مجددًا.')
        if not balance or balance[0] < pending[1]:
            raise ValueError('رصيدك لا يكفي لحجز هذا الرقم.')
        referral = min(Decimal(str(balance[1] or 0)), Decimal(str(pending[1])))
        held = db.execute("""UPDATE users SET coins=coins-?,
            referral_balance=GREATEST(0,referral_balance-?) WHERE user_id=? AND coins>=? RETURNING coins""",
            (pending[1], referral, user, pending[1])).fetchone()
        if not held:
            raise ValueError('رصيدك لا يكفي لحجز هذا الرقم.')
        db.execute('''INSERT INTO deferred_purchases
            (quote_id,user_id,phone,amount,ready_at,funds_reserved,referral_amount)
            VALUES(?,?,?,?,?,TRUE,?)''', (quote, user, phone, pending[1], end, referral))
        db.execute("""INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,fee)
            VALUES(?,?,?,?,?,0)""", (user, -pending[1], held[0], 'deferred_purchase_reserved', quote))
        db.execute("UPDATE account_reservations SET reserved_until='infinity'::timestamp WHERE phone=? AND user_id=?", (phone, user))
        db.execute("UPDATE commerce_quotes SET expires_at='infinity'::timestamp WHERE quote_id=?", (quote,))
        enqueue(db, user, f'deferred:reserved:{quote}',
                f'⏳ حُجز الرقم +{phone} لك حتى انتهاء جميع الإيجارات في {end:%Y-%m-%d %H:%M}. '
                f'حُجز {pending[1]}$ من رصيدك الآن ولن يُخصم ثانية عند التسليم. إذا أُلغي الحجز سيُعاد المبلغ إلى مصدره.')
        return True


async def reserve(user, purchase):
    row = await query('SELECT quote_id FROM pending_purchases WHERE user_id=? AND phone=?', (user, purchase[2]), fetchone=True)
    if not row or not row[0]:
        return False
    return await to_thread(reserve_sync, user, purchase[2], row[0])


def cancel_sync(user, quote, reason='cancelled'):
    with connect() as db:
        db.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE', (user,))
        row = db.execute("SELECT phone,amount,funds_reserved,referral_amount FROM deferred_purchases WHERE user_id=? AND quote_id=? AND status='pending' FOR UPDATE", (user, quote)).fetchone()
        if not row:
            return False
        phone, amount, funds_reserved, referral = row
        db.execute("UPDATE deferred_purchases SET status='cancelled',last_error=? WHERE quote_id=?", (reason, quote))
        db.execute('DELETE FROM account_reservations WHERE phone=? AND user_id=? AND star_order_id IS NULL', (phone, user))
        db.execute('DELETE FROM pending_purchases WHERE phone=? AND user_id=? AND quote_id=?', (phone, user, quote))
        db.execute("UPDATE commerce_quotes SET status='cancelled' WHERE quote_id=? AND status='pending'", (quote,))
        if funds_reserved:
            balance = db.execute("""UPDATE users SET coins=coins+?,
                referral_balance=referral_balance+? WHERE user_id=? RETURNING coins""",
                (amount, referral or 0, user)).fetchone()
            db.execute("""INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,fee)
                VALUES(?,?,?,?,?,0)""",
                (user, amount, balance[0], 'deferred_purchase_refund', quote))
        enqueue(db, user, f'deferred:cancel:{quote}',
                f'أُلغي حجز شراء الرقم +{phone} وتم إرجاع المبلغ المحتجز. ' +
                ('رصيدك غير كافٍ وقت التسليم.' if reason == 'insufficient_funds' else
                 'لم يعد الكوبون صالحًا؛ اطلب عرض سعر جديدًا وراجع المبلغ قبل التأكيد.' if reason == 'coupon_invalid' else
                 'يمكنك مراجعة الدعم لمعرفة التفاصيل.'))
        return True


async def monitor():
    from helpers import get_pending_purchase, settle_purchase_atomically
    rows = await query("""SELECT quote_id,user_id,phone FROM deferred_purchases d WHERE status='pending'
        AND NOT EXISTS(SELECT 1 FROM rental_leases l WHERE l.phone=d.phone AND l.status='active' AND l.ends_at>CURRENT_TIMESTAMP)
        ORDER BY ready_at LIMIT 30""", fetchall=True)
    for quote, user, phone in rows or []:
        try:
            purchase = await get_pending_purchase(user, phone)
            if not purchase:
                await to_thread(cancel_sync, user, quote, 'missing_purchase')
                continue
            success, reason = await settle_purchase_atomically(user, purchase, store_account=True, expected_quote=quote)
            if not success and reason in {'insufficient_funds', 'unavailable', 'inventory_changed', 'reservation_expired', 'quote_invalid', 'coupon_invalid'}:
                await to_thread(cancel_sync, user, quote, reason)
        except Exception:
            logger.exception('Deferred purchase retry: quote=%s', quote)


async def menu(event, action='list', value='0'):
    from keyboards import Button, arrange_buttons
    if event.sender_id != event.chat_id:
        return
    if action == 'cancel':
        await to_thread(cancel_sync, event.sender_id, value)
    rows = await query("SELECT quote_id,phone,amount,ready_at FROM deferred_purchases WHERE user_id=? AND status='pending' ORDER BY ready_at LIMIT 30",
                       (event.sender_id,), fetchall=True)
    text = '⏳ حجوزات الشراء المؤجل\n\n'
    text += '\n'.join(f'+{phone} · {amount}$ · {end:%Y-%m-%d %H:%M}' for _, phone, amount, end in rows) or 'لا توجد حجوزات شراء مؤجل.'
    buttons = [Button.inline(f'إلغاء حجز +{phone}', data=f'deferred:cancel:{key}') for key, phone, _, _ in rows]
    buttons.append(Button.inline('رجوع', data='numbers_menu'))
    await event.edit(text, parse_mode=None, buttons=arrange_buttons(buttons))
