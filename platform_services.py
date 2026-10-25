"""Atomic withdrawal refunds, rental renewals, and ownership-scoped controls."""
import secrets
from decimal import Decimal

from postgres import connect
from utils.asyncio_compat import to_thread
from utils.privacy import mask_phone
from workflow import enqueue, query


def reject_withdrawal_sync(request_id, admin_id, reason):
    reason = str(reason).strip()
    if not reason or len(reason) > 500:
        raise ValueError('سبب الرفض مطلوب وبحد أقصى 500 حرف.')
    with connect() as db:
        # Competes with the existing pending->paid transition on the same row.
        row = db.execute('''UPDATE withdrawal_requests SET status='rejected',rejection_reason=?,
            processed_by=?,processed_at=CURRENT_TIMESTAMP WHERE request_id=? AND status='pending'
            RETURNING user_id,amount''', (reason, admin_id, request_id)).fetchone()
        if not row:
            return None
        user_id, amount = row
        balance = db.execute('UPDATE users SET coins=coins+? WHERE user_id=? RETURNING coins',
                             (amount, user_id)).fetchone()
        if balance is None:
            raise ValueError('تعذر إيجاد صاحب الطلب؛ لم يتم الرفض أو الاسترداد.')
        db.execute('''INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,fee)
                      VALUES(?,?,?,?,?,0)''', (user_id, amount, balance[0], 'withdrawal_refund', str(request_id)))
        enqueue(db, user_id, f'withdrawal-rejected:{request_id}',
                f'رُفض طلب السحب #{request_id}. أُعيد {amount}$ إلى رصيدك.\nالسبب: {reason}')
        db.commit()
        return user_id, amount


async def reject_withdrawal(request_id, admin_id, reason):
    return await to_thread(reject_withdrawal_sync, request_id, admin_id, reason)


async def renewal_quote(user_id, lease_id, days):
    from helpers import get_setting
    maximum = int(await get_setting('rental_max_days') or '30')
    rate = Decimal(str(await get_setting('rental_daily_price') or '0')).quantize(Decimal('.01'))
    if not 1 <= days <= maximum or not rate.is_finite() or rate <= 0:
        raise ValueError('المدة أو سعر التأجير غير صالح.')
    key = secrets.token_hex(12)
    row = await query('''INSERT INTO rental_renewal_quotes(quote_id,lease_id,user_id,days,amount,expected_end)
        SELECT ?,lease_id,renter_id,?,?,ends_at FROM rental_leases
        WHERE lease_id=? AND renter_id=? AND status='active' AND ends_at>CURRENT_TIMESTAMP
        AND NOT EXISTS(SELECT 1 FROM deferred_purchases d WHERE d.phone=rental_leases.phone AND d.status='pending')
        RETURNING quote_id,amount,expected_end''',
        (key, days, rate * days, lease_id, user_id), commit=True, fetchone=True)
    if not row:
        raise ValueError('التجديد غير متاح: الإيجار منتهٍ أو الرقم محجوز لمشترٍ.')
    from commerce import quote
    promotion = await quote(user_id, 'renewal', key, rate * days)
    await query('''UPDATE rental_renewal_quotes SET amount=?,promotion_quote=?,
        expires_at=CURRENT_TIMESTAMP+INTERVAL '5 minutes' WHERE quote_id=? AND user_id=?''',
        (promotion['amount'], promotion['quote_id'], key, user_id), commit=True)
    return row[0], promotion['amount'], row[2]


def renew_lease_sync(user_id, quote_id):
    with connect() as db:
        q = db.execute('''SELECT lease_id,days,amount,expected_end,status,expires_at>CURRENT_TIMESTAMP,promotion_quote
            FROM rental_renewal_quotes WHERE quote_id=? AND user_id=? FOR UPDATE''', (quote_id, user_id)).fetchone()
        if not q:
            return False, 'عرض التجديد غير موجود.'
        lease_id, days, amount, expected_end, status, valid, promotion_quote = q
        if status == 'completed':
            return True, 'تم تنفيذ هذا التجديد سابقًا؛ لم يُخصم مبلغ جديد.'
        if status != 'pending' or not valid:
            return False, 'انتهت صلاحية العرض. أنشئ عرض تجديد جديدًا.'
        db.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE', (user_id,))
        lease = db.execute('''SELECT phone FROM rental_leases WHERE lease_id=? AND renter_id=?
            AND status='active' AND ends_at=? AND ends_at>CURRENT_TIMESTAMP FOR UPDATE''',
            (lease_id, user_id, expected_end)).fetchone()
        if not lease:
            return False, 'انتهى الإيجار أو تغيرت مدته؛ اطلب عرضًا جديدًا.'
        inventory = db.execute('SELECT session,is_sold FROM accounts WHERE phone=? FOR UPDATE', (lease[0],)).fetchone()
        if not inventory or inventory[1]:
            return False, 'الحساب لم يعد متاحًا للتجديد.'
        if db.execute("SELECT 1 FROM deferred_purchases WHERE phone=? AND status='pending'", (lease[0],)).fetchone():
            return False, 'الرقم محجوز للشراء؛ ينتهي إيجارك الحالي دون تمديد.'
        blocked = db.execute('''SELECT 1 FROM account_health WHERE phone=? AND inventory_session=?
            AND status IN ('revoked','banned','deleted','restricted','invalid','mismatch')''',
            (lease[0], inventory[0])).fetchone()
        if blocked:
            return False, 'الحساب مستبعد بسبب عطل مؤكد؛ تعذر التجديد.'
        from commerce import consume_quote, award_spend
        if promotion_quote:
            amount = consume_quote(db, user_id, promotion_quote, 'renewal', quote_id)
        else:
            award_spend(db, user_id, amount)
        balance = db.execute('''UPDATE users SET coins=coins-?,
            referral_balance=GREATEST(0,referral_balance-?) WHERE user_id=? AND coins>=?
            RETURNING coins''', (amount, amount, user_id, amount)).fetchone()
        if not balance:
            db.rollback()
            return False, 'الرصيد غير كافٍ.'
        end = db.execute("UPDATE rental_leases SET ends_at=ends_at+(? * INTERVAL '1 day'),price=price+? WHERE lease_id=? RETURNING ends_at",
                         (days, amount, lease_id)).fetchone()[0]
        db.execute('''INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,fee)
            VALUES(?,?,?,?,?,0)''', (user_id, -amount, balance[0], 'rental_renewal', quote_id))
        db.execute("UPDATE rental_renewal_quotes SET status='completed' WHERE quote_id=?", (quote_id,))
        enqueue(db, user_id, f'renewal:{quote_id}', f'تم تجديد الإيجار #{lease_id} مقابل {amount}$. ينتهي في {end}.')
        db.commit()
        return True, f'تم التجديد مقابل {amount}$ حتى {end}.'


async def renew_lease(user_id, quote_id):
    return await to_thread(renew_lease_sync, user_id, quote_id)


async def rental_reminders():
    # Re-evaluate before sending and discard obsolete unsent reminders after
    # renewal. Dedupe includes the end date, so a renewed term gets fresh notices.
    await query('''UPDATE operation_notices n SET sent_at=CURRENT_TIMESTAMP
        WHERE n.sent_at IS NULL AND left(n.dedupe_key,6)='lease:'
        AND NOT EXISTS(SELECT 1 FROM rental_leases l
            WHERE strpos(n.dedupe_key,'lease:' || l.lease_id::text || ':' || l.ends_at::text || ':')=1)''', commit=True)
    rows = await query('''SELECT lease_id,renter_id,phone,ends_at::text,
        CASE WHEN ends_at<=CURRENT_TIMESTAMP THEN 'expired'
             WHEN ends_at<=CURRENT_TIMESTAMP+INTERVAL '1 hour' THEN '1h' ELSE '1d' END
        FROM rental_leases WHERE status IN ('active','expired')
        AND ends_at>CURRENT_TIMESTAMP-INTERVAL '1 day'
        AND ends_at<=CURRENT_TIMESTAMP+INTERVAL '1 day' ''', fetchall=True)
    for lease_id, owner, phone, end, stage in rows or []:
        from workflow import queue_notice
        title = 'انتهى الإيجار' if stage == 'expired' else ('باقي ساعة أو أقل على الإيجار' if stage == '1h' else 'باقي يوم أو أقل على الإيجار')
        await queue_notice(owner, f'lease:{lease_id}:{end}:{stage}',
                           f'{title} #{lease_id} للحساب {mask_phone(phone)}.\nموعد الانتهاء: {end}\nراجع «إيجاراتي والتجديد».')


async def owned_password(user_id, phone):
    # Renters, global admins, and former owners have no bypass here.
    from helpers import decrypt_session
    row = await query('''WITH owners AS (
        SELECT user_id,twofa FROM stored_accounts WHERE phone=?
        UNION ALL SELECT user_id,twofa FROM purchased_accounts WHERE phone=?
    ) SELECT twofa FROM owners WHERE user_id=?
      AND NOT EXISTS(SELECT 1 FROM owners WHERE user_id<>?)''',
        (phone, phone, user_id, user_id), fetchone=True)
    return decrypt_session(row[0]) if row and row[0] else None


def record_twofa_outcome(db, phone, status, error=None):
    owners = db.execute('''SELECT user_id FROM stored_accounts WHERE phone=?
        UNION SELECT user_id FROM purchased_accounts WHERE phone=?
        UNION SELECT owner_id FROM scheduled_twofa_changes WHERE phone=? AND owner_id IS NOT NULL''',
        (phone, phone, phone)).fetchall()
    for (user_id,) in owners:
        stamp = db.execute('''INSERT INTO twofa_history(phone,user_id,status,error) VALUES(?,?,?,?)
            ON CONFLICT(phone,user_id) DO UPDATE SET status=excluded.status,error=excluded.error,
            updated_at=CURRENT_TIMESTAMP RETURNING updated_at''', (phone, user_id, status, error)).fetchone()[0]
        title = 'نجح تغيير كلمة التحقق بخطوتين' if status == 'completed' else 'تعذر تغيير كلمة التحقق بخطوتين'
        enqueue(db, user_id, f'twofa:{phone}:{user_id}:{stamp}',
                f'{title} للحساب {mask_phone(phone)}.\n{error or "راجع إدارة 2FA للتفاصيل."}')
