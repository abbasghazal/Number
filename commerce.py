"""Server-priced quotes, atomic coupon redemption and completed-spend loyalty."""
import re
import secrets
import json
from decimal import Decimal, ROUND_HALF_UP

from postgres import connect
from utils.asyncio_compat import to_thread
from workflow import query

CENT = Decimal('.01')
SERVICES = {'purchase', 'rental', 'renewal'}


class CouponUnavailable(ValueError):
    def __init__(self, message, *, clear_preference=True):
        super().__init__(message)
        self.clear_preference = clear_preference


class QuoteRefreshRequired(ValueError):
    pass


def money(value, *, positive=False):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or (positive and amount <= 0) or amount > Decimal('999999999999.99'):
        raise ValueError('قيمة مالية غير صالحة.')
    amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if positive and amount <= 0:
        raise ValueError('المبلغ أقل من أصغر قيمة مسموحة.')
    return amount


def coupon_code(value):
    code = str(value).strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code):
        raise ValueError('الكوبون: من 3 إلى 32 حرفاً إنكليزياً أو رقماً أو _ أو -.')
    return code


def discount_amount(base, kind, value):
    base, value = money(base, positive=True), money(value)
    if kind == 'percent':
        value = base * min(value, Decimal('100')) / 100
    elif kind != 'fixed':
        raise ValueError('نوع خصم غير صالح.')
    return max(Decimal('0'), min(money(value), base - CENT))


def loyalty(db, user):
    member = db.execute('SELECT spent,purchases FROM loyalty_members WHERE user_id=?', (user,)).fetchone() or (0, 0)
    tier = db.execute('SELECT name,threshold,percent FROM loyalty_tiers WHERE threshold<=? ORDER BY threshold DESC LIMIT 1',
                      (member[0],)).fetchone() or ('أساسي', 0, 0)
    return {'spent': money(member[0]), 'purchases': member[1], 'tier': tier[0], 'percent': money(tier[2])}


def loyalty_sync(user):
    with connect() as db:
        return loyalty(db, user)


async def loyalty_status(user):
    return await to_thread(loyalty_sync, user)


def award_spend(db, user, amount):
    db.execute('''INSERT INTO loyalty_members(user_id,spent,purchases) VALUES(?,?,1)
        ON CONFLICT(user_id) DO UPDATE SET spent=loyalty_members.spent+excluded.spent,
        purchases=loyalty_members.purchases+1''', (user, amount))


def validate_coupon(db, code, user, service, *, lock=False):
    row = db.execute('''SELECT kind,value,service,max_uses,per_user,used,enabled,
        expires_at>CURRENT_TIMESTAMP FROM commerce_coupons WHERE code=?''' + (' FOR UPDATE' if lock else ''),
        (code,)).fetchone()
    if not row or not row[6] or not row[7] or row[5] >= row[3]:
        raise CouponUnavailable('الكوبون غير متاح أو انتهت صلاحيته أو استخداماته.')
    if row[2] not in {'all', service}:
        raise CouponUnavailable('الكوبون لا يشمل هذه الخدمة.', clear_preference=False)
    count = db.execute('SELECT COUNT(*) FROM commerce_redemptions WHERE code=? AND user_id=?', (code, user)).fetchone()[0]
    if count >= row[4]:
        raise CouponUnavailable('وصلت إلى حد استخدام هذا الكوبون.')
    return row


def make_quote(db, user, service, reference, base):
    if service not in SERVICES:
        raise ValueError('خدمة غير صالحة.')
    base = money(base, positive=True)
    if not db.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE', (user,)).fetchone():
        raise ValueError('ابدأ البوت أولاً.')
    status = loyalty(db, user)
    loyalty_discount = discount_amount(base, 'percent', status['percent'])
    preference = db.execute('SELECT coupon FROM commerce_preferences WHERE user_id=?', (user,)).fetchone()
    code, coupon_discount, coupon_notice = None, Decimal('0'), None
    if preference and preference[0]:
        candidate = preference[0]
        try:
            row = validate_coupon(db, candidate, user, service)
        except CouponUnavailable as exc:
            if exc.clear_preference:
                db.execute('UPDATE commerce_preferences SET coupon=NULL WHERE user_id=? AND coupon=?', (user, candidate))
            coupon_notice = str(exc) + ' عُرض السعر المتاح دون هذا الكوبون؛ راجع المبلغ قبل التأكيد.'
        else:
            coupon_discount = discount_amount(base, row[0], row[1])
            if coupon_discount > loyalty_discount:
                code = candidate
    discount = max(loyalty_discount, coupon_discount)
    key = secrets.token_hex(12)
    db.execute("UPDATE commerce_quotes SET status='replaced' WHERE user_id=? AND service=? AND reference=? AND status='pending'",
               (user, service, str(reference)))
    db.execute('''INSERT INTO commerce_quotes(quote_id,user_id,service,reference,base,amount,discount,coupon)
        VALUES(?,?,?,?,?,?,?,?)''', (key, user, service, str(reference), base, base-discount, discount, code))
    return {'quote_id': key, 'base': base, 'amount': base-discount, 'discount': discount,
            'coupon': code, 'tier': status['tier'], 'service': service, 'coupon_notice': coupon_notice}


def quote_sync(user, service, reference, base):
    with connect() as db:
        return make_quote(db, user, service, reference, base)


async def quote(user, service, reference, base):
    return await to_thread(quote_sync, user, service, reference, base)


def consume_quote(db, user, key, service, reference):
    # All settlement paths lock the user first; coupon lock serializes global limits.
    db.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE', (user,))
    row = db.execute('''SELECT amount,coupon,status,expires_at>CURRENT_TIMESTAMP,service,reference
        FROM commerce_quotes WHERE quote_id=? AND user_id=? FOR UPDATE''', (key, user)).fetchone()
    if not row or row[2] != 'pending' or not row[3] or row[4] != service or row[5] != str(reference):
        raise ValueError('انتهى عرض السعر أو تم تنفيذه. اطلب عرضاً جديداً.')
    if service == 'purchase':
        from number_catalog import verify_snapshot
        saved = db.execute('SELECT number_snapshot FROM commerce_quotes WHERE quote_id=?', (key,)).fetchone()
        verify_snapshot(db, reference, saved[0] if saved else None)
    if row[1]:
        try:
            validate_coupon(db, row[1], user, service, lock=True)
        except CouponUnavailable:
            raise CouponUnavailable('لم يعد كوبون هذا العرض صالحًا؛ اطلب عرض سعر جديدًا وراجع المبلغ قبل التأكيد.') from None
        db.execute('UPDATE commerce_coupons SET used=used+1 WHERE code=?', (row[1],))
        db.execute('INSERT INTO commerce_redemptions(quote_id,code,user_id) VALUES(?,?,?)', (key, row[1], user))
    db.execute("UPDATE commerce_quotes SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE quote_id=?", (key,))
    award_spend(db, user, row[0])
    return money(row[0])


async def select_coupon(user, value):
    code = None if str(value).strip() == '-' else coupon_code(value)
    if code:
        row = await query('SELECT 1 FROM commerce_coupons WHERE code=? AND enabled AND expires_at>CURRENT_TIMESTAMP AND used<max_uses',
                          (code,), fetchone=True)
        if not row:
            raise ValueError('الكوبون غير متاح.')
    await query('''INSERT INTO commerce_preferences(user_id,coupon) VALUES(?,?)
        ON CONFLICT(user_id) DO UPDATE SET coupon=excluded.coupon''', (user, code), commit=True)
    return code


def create_coupon_sync(actor, code, kind, value, service, days, maximum, per_user):
    code, value = coupon_code(code), money(value, positive=True)
    if kind not in {'fixed', 'percent'} or service not in SERVICES | {'all'} or not 1 <= days <= 365:
        raise ValueError('نوع الخصم أو الخدمة أو المدة غير صالح.')
    if kind == 'percent' and value > 100 or not 1 <= per_user <= maximum <= 1000000:
        raise ValueError('النسبة أو حدود الاستخدام غير صالحة.')
    with connect() as db:
        from staff_security import permitted
        if not permitted(db, actor, 'finance'):
            raise ValueError('صلاحية المالية مطلوبة.')
        inserted = db.execute('''INSERT INTO commerce_coupons(code,kind,value,service,expires_at,max_uses,per_user,created_by)
            VALUES(?,?,?,?,CURRENT_TIMESTAMP+(? * INTERVAL '1 day'),?,?,?)
            ON CONFLICT(code) DO NOTHING RETURNING code''', (code, kind, value, service, days, maximum, per_user, actor)).fetchone()
        if not inserted:
            raise ValueError('اسم الكوبون مستخدم بالفعل.')
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (actor, f'coupon_created:{code}'))
    return code


async def create_coupon(*args):
    return await to_thread(create_coupon_sync, *args)


def disable_coupon_sync(actor, code):
    from staff_security import permitted

    code = coupon_code(code)
    with connect() as db:
        if not permitted(db, actor, 'finance'):
            raise ValueError('صلاحية المالية مطلوبة.')
        db.execute('UPDATE commerce_coupons SET enabled=FALSE WHERE code=?', (code,))
        db.execute('UPDATE commerce_preferences SET coupon=NULL WHERE coupon=?', (code,))
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (actor, f'coupon_disabled:{code}'))


async def disable_coupon(actor, code):
    await to_thread(disable_coupon_sync, actor, code)


def configure_tier_sync(actor, name, threshold, percent):
    name, threshold, percent = str(name).strip(), money(threshold), money(percent)
    if not 1 <= len(name) <= 30 or percent > 50:
        raise ValueError('اسم المستوى أو نسبة الخصم غير صالحة.')
    with connect() as db:
        from staff_security import permitted
        if not permitted(db, actor, 'finance'):
            raise ValueError('صلاحية المالية مطلوبة.')
        old = db.execute('SELECT threshold,percent FROM loyalty_tiers WHERE name=? FOR UPDATE', (name,)).fetchone()
        collision = db.execute('SELECT name FROM loyalty_tiers WHERE threshold=? AND name<>?', (threshold, name)).fetchone()
        if collision:
            raise ValueError('هناك مستوى آخر بنفس حد الإنفاق.')
        if old and old[0] == 0 and threshold != 0:
            raise ValueError('يجب أن يبقى المستوى الأساسي عند حد إنفاق صفر.')
        db.execute('''INSERT INTO loyalty_tiers(name,threshold,percent) VALUES(?,?,?)
            ON CONFLICT(name) DO UPDATE SET threshold=excluded.threshold,percent=excluded.percent''', (name, threshold, percent))
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (actor, f'loyalty_tier:{name}:{old}->{threshold},{percent}'))


async def configure_tier(*args):
    return await to_thread(configure_tier_sync, *args)


def price_text(q):
    from number_catalog import TYPES
    origin = f"الكوبون {q['coupon']}" if q['coupon'] else f"مستوى {q['tier']}"
    identity = (f"النوع: {TYPES[q['number_type']]}\nالدولة: {q['country_name']}\n" if q.get('number_type') else '')
    notice = '\n' + q['coupon_notice'] if q.get('coupon_notice') else ''
    return identity + f"السعر الأصلي: {q['base']}$\nالخصم: {q['discount']}$ ({origin})\nالسعر النهائي: {q['amount']}$\nالعرض صالح لخمس دقائق." + notice


async def rental_quote(user, count, days):
    from helpers import get_setting
    rate = money(await get_setting('rental_daily_price') or 0, positive=True)
    maximum = int(await get_setting('rental_max_days') or 30)
    if not 1 <= count <= 10000 or not 1 <= days <= maximum:
        raise ValueError('عدد الأرقام أو مدة التأجير غير صالح.')
    ref = f'{count}:{days}:{rate}'
    return await quote(user, 'rental', ref, rate*count*days)


def purchase_quote_sync(user, phone):
    with connect() as db:
        db.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE', (user,))
        account = db.execute('''SELECT session,twofa,calling_code,country_id FROM accounts a
            WHERE phone=? AND is_active AND NOT is_sold
            AND NOT EXISTS(SELECT 1 FROM account_health h WHERE h.phone=a.phone AND h.inventory_session=a.session
                AND h.status IN ('revoked','banned','deleted','restricted','invalid','mismatch')) FOR UPDATE''', (phone,)).fetchone()
        if not account:
            raise ValueError('الحساب غير متاح.')
        if db.execute("SELECT 1 FROM deferred_purchases WHERE phone=? AND status='pending'", (phone,)).fetchone():
            raise ValueError('الرقم محجوز للشراء بعد انتهاء الإيجارات.')
        if db.execute("SELECT 1 FROM account_reservations WHERE phone=? AND star_order_id IS NOT NULL AND reserved_until>CURRENT_TIMESTAMP", (phone,)).fetchone():
            raise ValueError('الرقم محجوز للدفع بالنجوم. تابع الطلب من /paysupport.')
        pending = db.execute('SELECT status FROM pending_purchases WHERE user_id=? AND phone=? FOR UPDATE', (user, phone)).fetchone()
        if pending and pending[0] == 'processing':
            raise ValueError('عملية الشراء قيد التنفيذ.')
        reserved = db.execute('''INSERT INTO account_reservations(phone,user_id,reserved_until)
            VALUES(?,?,CURRENT_TIMESTAMP+INTERVAL '5 minutes') ON CONFLICT(phone)
            DO UPDATE SET user_id=excluded.user_id,reserved_until=excluded.reserved_until,star_order_id=NULL
            WHERE (account_reservations.user_id=excluded.user_id AND account_reservations.star_order_id IS NULL)
                OR account_reservations.reserved_until<=CURRENT_TIMESTAMP
            RETURNING phone''', (phone, user)).fetchone()
        if not reserved:
            raise ValueError('الحساب محجوز لمستخدم آخر.')
        from number_catalog import snapshot
        identity = snapshot(db, phone)
        country = db.execute('SELECT calling_code,name FROM countries WHERE country_id=? AND is_active',
                             (identity['country_id'],)).fetchone()
        if not country:
            raise ValueError('الدولة غير متاحة.')
        q = make_quote(db, user, 'purchase', phone, identity['base'])
        db.execute('UPDATE commerce_quotes SET number_snapshot=? WHERE quote_id=?',
                   (json.dumps(identity, default=str, ensure_ascii=False), q['quote_id']))
        q.update(identity)
        q['country_name'] = country[1]
        db.execute('''INSERT INTO pending_purchases(user_id,phone,calling_code,name,price,session,twofa,quote_id)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,phone) DO UPDATE SET
            calling_code=excluded.calling_code,name=excluded.name,price=excluded.price,
            session=excluded.session,twofa=excluded.twofa,quote_id=excluded.quote_id,
            status='pending',processing=FALSE,updated_at=CURRENT_TIMESTAMP''',
            (user, phone, country[0], country[1], q['amount'], account[0], account[1], q['quote_id']))
        return q


async def purchase_quote(user, phone):
    return await to_thread(purchase_quote_sync, user, phone)


def cancel_invalid_purchase_sync(user,key):
    with connect() as db:
        db.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE',(user,))
        quote=db.execute("SELECT reference FROM commerce_quotes WHERE quote_id=? AND user_id=? AND service='purchase'",(key,user)).fetchone()
        if not quote:
            return
        phone=quote[0]
        db.execute('SELECT phone FROM accounts WHERE phone=? FOR UPDATE',(phone,))
        pending=db.execute('SELECT quote_id FROM pending_purchases WHERE user_id=? AND phone=? FOR UPDATE',(user,phone)).fetchone()
        if not pending or pending[0]!=key:
            return
        deferred=db.execute("SELECT amount,funds_reserved,referral_amount FROM deferred_purchases WHERE quote_id=? AND user_id=? AND status='pending' FOR UPDATE",(key,user)).fetchone()
        changed=db.execute("UPDATE commerce_quotes SET status='cancelled' WHERE quote_id=? AND user_id=? AND status='pending' RETURNING quote_id",(key,user)).fetchone()
        if changed:
            db.execute('DELETE FROM pending_purchases WHERE user_id=? AND phone=? AND quote_id=?',(user,phone,key))
            db.execute('DELETE FROM account_reservations WHERE user_id=? AND phone=? AND star_order_id IS NULL',(user,phone))
            db.execute("UPDATE deferred_purchases SET status='cancelled',last_error='quote_invalid' WHERE quote_id=? AND user_id=? AND status='pending'",(key,user))
            if deferred and deferred[1]:
                balance=db.execute("UPDATE users SET coins=coins+?,referral_balance=referral_balance+? WHERE user_id=? RETURNING coins",(deferred[0],deferred[2],user)).fetchone()
                db.execute("INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,fee) VALUES(?,?,?,?,?,0)",(user,deferred[0],balance[0],'deferred_purchase_refund',key))


async def complete_purchase(user, key):
    from helpers import get_pending_purchase, settle_purchase_atomically
    row = await query("SELECT reference,status FROM commerce_quotes WHERE quote_id=? AND user_id=? AND service='purchase'",
                      (key, user), fetchone=True)
    if not row:
        raise ValueError('عرض الشراء غير موجود.')
    if row[1] == 'completed':
        return {'completed': True, 'repeated': True}
    purchase = await get_pending_purchase(user, row[0])
    if not purchase:
        raise ValueError('انتهى الحجز؛ اطلب عرضاً جديداً.')
    from deferred_purchase import reserve_sync
    if await to_thread(reserve_sync, user, row[0], key):
        return {'completed': False, 'deferred': True}
    success, reason = await settle_purchase_atomically(user, purchase, store_account=True, expected_quote=key)
    if not success:
        # A concurrent confirmation may have won while this request waited.
        state = await query('SELECT status FROM commerce_quotes WHERE quote_id=? AND user_id=?', (key, user), fetchone=True)
        if state and state[0] == 'completed':
            return {'completed': True, 'repeated': True}
        if reason in {'quote_invalid', 'coupon_invalid'}:
            await to_thread(cancel_invalid_purchase_sync,user,key)
        if reason == 'coupon_invalid':
            raise QuoteRefreshRequired('لم يعد الكوبون صالحًا؛ حدّث عرض السعر وراجع المبلغ الجديد قبل تأكيد الشراء.')
        reasons={'quote_invalid':'انتهى العرض أو تغيرت أهلية الرقم؛ اطلب عرضًا جديدًا.',
            'reservation_expired':'انتهت مهلة الحجز؛ اختر الرقم مجددًا.',
            'insufficient_funds':'رصيدك غير كافٍ لإتمام الشراء.',
            'inventory_changed':'تغيرت جلسة الرقم؛ اطلب عرضًا جديدًا.',
            'unavailable':'الرقم لم يعد متاحًا.','rental_active':'انتظر انتهاء الإيجار أو استخدم حجز الشراء المؤجل.'}
        raise ValueError(reasons.get(reason,'تعذر إتمام الشراء الآن؛ حاول لاحقًا.'))
    from workflow import queue_notice
    await queue_notice(user, f'miniapp:purchase:{key}', 'اكتمل شراء الحساب وتخزينه. افتح «عرض الحسابات» في البوت لإدارته.')
    return {'completed': True, 'repeated': False}


async def complete_rental(user, key):
    from helpers import rent_available_accounts
    row = await query("SELECT reference,status FROM commerce_quotes WHERE quote_id=? AND user_id=? AND service='rental'",
                      (key, user), fetchone=True)
    if not row:
        raise ValueError('عرض التأجير غير موجود.')
    if row[1] == 'completed':
        return {'completed': True, 'repeated': True}
    count, days, rate = row[0].split(':')
    try:
        phones, result = await rent_available_accounts(user, int(count), int(days), Decimal(rate), key)
    except ValueError:
        state = await query('SELECT status FROM commerce_quotes WHERE quote_id=? AND user_id=?', (key, user), fetchone=True)
        if state and state[0] == 'completed':
            return {'completed': True, 'repeated': True}
        raise
    if not phones:
        raise ValueError('تعذر التأجير؛ الرصيد أو المخزون غير كافٍ.')
    from workflow import queue_notice
    await queue_notice(user, f'miniapp:rental:{key}', f'اكتمل تأجير {len(phones)} رقم مقابل {result}$. افتح «إيجاراتي والتجديد» في البوت.')
    return {'completed': True, 'count': len(phones), 'amount': result, 'repeated': False}
