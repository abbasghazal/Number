"""Durable, direct Stars purchases. Payment receipt and inventory delivery are separate transactions."""
import json
import logging
import secrets
from decimal import Decimal, ROUND_CEILING

import aiohttp
from telethon import functions, types

from config import PAYMENT_STARS_PER_CREDIT, TOKEN
from postgres import connect
from utils.asyncio_compat import to_thread
from workflow import enqueue, query

logger = logging.getLogger(__name__)
PREFIX = 'number:'
BAD_HEALTH = "('revoked','banned','deleted','restricted','invalid','mismatch')"


class PaymentApiError(RuntimeError):
    def __init__(self, message, cause=None):
        self.message = message
        self.cause = cause
        super().__init__(message)


async def bot_api(method, payload):
    """Use canonical Bot API payment IDs for both reconciliation and refunds; never expose token URLs."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30, sock_connect=8), trust_env=True) as session:
            async with session.post(f'https://api.telegram.org/bot{TOKEN}/{method}', json=payload) as response:
                data = await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise PaymentApiError('TELEGRAM_PAYMENT_API_UNAVAILABLE', type(exc).__name__) from None
    if not data.get('ok'):
        description = data.get('description', '')
        reason = 'CHARGE_ALREADY_REFUNDED' if 'CHARGE_ALREADY_REFUNDED' in description else 'TELEGRAM_PAYMENT_API_ERROR'
        raise PaymentApiError(reason)
    return data['result']


def stars_price(price, rate, fixed=None):
    if fixed is not None:
        amount = Decimal(str(fixed))
    else:
        price, rate = Decimal(str(price)), Decimal(str(rate))
        if not price.is_finite() or not rate.is_finite() or price <= 0 or rate <= 0:
            raise ValueError('سعر النجوم غير صالح.')
        amount = (price * rate).to_integral_value(rounding=ROUND_CEILING)
    if not amount.is_finite() or amount != amount.to_integral_value() or not 1 <= amount <= 10_000_000:
        raise ValueError('السعر يجب أن يكون بين 1 و10000000 نجمة.')
    return int(amount)


def expire(db, phone=None):
    # Only unpaid orders expire. A paid order keeps its reservation until delivery/refund.
    db.execute("""UPDATE star_purchase_orders SET status='expired'
        WHERE status IN ('pending','checkout') AND expires_at<=CURRENT_TIMESTAMP""" +
        (' AND phone=?' if phone else ''), (phone,) if phone else ())
    db.execute("""DELETE FROM account_reservations r USING star_purchase_orders o
        WHERE r.star_order_id=o.order_id AND o.status IN ('expired','cancelled','refunded')""" +
        (' AND r.phone=?' if phone else ''), (phone,) if phone else ())


def available(db, phone):
    return db.execute(f"""SELECT session,twofa,calling_code,country_id,acquisition_cost
        FROM accounts a WHERE phone=? AND is_active AND NOT is_sold
        AND NOT EXISTS(SELECT 1 FROM rental_leases l WHERE l.phone=a.phone AND l.status='active' AND l.ends_at>CURRENT_TIMESTAMP)
        AND NOT EXISTS(SELECT 1 FROM deferred_purchases d WHERE d.phone=a.phone AND d.status='pending')
        AND NOT EXISTS(SELECT 1 FROM account_health h WHERE h.phone=a.phone
            AND h.inventory_session=a.session AND h.status IN {BAD_HEALTH}) FOR UPDATE""", (phone,)).fetchone()


def same_session(current, snapshot):
    from helpers import decrypt_session
    current, snapshot = decrypt_session(current), decrypt_session(snapshot)
    return bool(current) and current == snapshot


def price_for(db, country):
    setting = db.execute("SELECT value FROM settings WHERE key='payment_stars_per_credit'").fetchone()
    return stars_price(country[2], setting[0] if setting else PAYMENT_STARS_PER_CREDIT, country[3])


def identity_valid(db, row):
    from number_catalog import verify_snapshot
    try:
        verify_snapshot(db,row[1],row[10])
        return True
    except ValueError:
        return False


async def display_price(phone):
    def read():
        from number_catalog import snapshot
        with connect() as db:
            data = snapshot(db,phone)
            return price_for(db,('', '',data['base'],data['stars_price'],data['country_id']))
    return await to_thread(read)


def create_sync(user, phone):
    with connect() as db:
        if not db.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE', (user,)).fetchone():
            raise ValueError('ابدأ البوت أولاً.')
        account = available(db, phone)
        if not account:
            raise ValueError('الرقم غير متاح.')
        expire(db, phone)
        old = db.execute("""SELECT order_id,user_id,amount,name,status FROM star_purchase_orders
            WHERE phone=? AND status IN ('pending','checkout','paid') FOR UPDATE""", (phone,)).fetchone()
        if old:
            if old[1] == user and old[4] == 'pending':
                return {'id': old[0], 'amount': old[2], 'name': old[3]}
            raise ValueError('الرقم محجوز لعملية دفع جارية. تابع الطلب من /paysupport.')
        pending = db.execute('SELECT status FROM pending_purchases WHERE user_id=? AND phone=? FOR UPDATE',
                             (user, phone)).fetchone()
        if pending and pending[0] == 'processing':
            raise ValueError('عملية شراء الرقم قيد التنفيذ.')
        # Legacy inventory may have no country_id. Choose a typed comparison
        # instead of an untyped NULL placeholder that PostgreSQL cannot infer.
        country_filter = 'country_id=?' if account[3] is not None else 'calling_code=?'
        country_value = account[3] if account[3] is not None else account[2]
        country = db.execute(f'''SELECT calling_code,name,price,stars_price,country_id FROM countries
            WHERE is_active AND {country_filter}
            ORDER BY country_id LIMIT 1''', (country_value,)).fetchone()
        if not country:
            raise ValueError('الدولة غير متاحة.')
        from number_catalog import snapshot
        identity = snapshot(db, phone)
        country = (country[0],country[1],identity['base'],identity['stars_price'],identity['country_id'])
        amount = price_for(db, country)
        key = secrets.token_hex(12)
        holder = db.execute("""INSERT INTO account_reservations(phone,user_id,reserved_until,star_order_id)
            VALUES(?,?,CURRENT_TIMESTAMP+INTERVAL '5 minutes',?) ON CONFLICT(phone)
            DO UPDATE SET user_id=excluded.user_id,reserved_until=excluded.reserved_until,star_order_id=excluded.star_order_id
            WHERE (account_reservations.user_id=excluded.user_id AND account_reservations.star_order_id IS NULL)
                OR account_reservations.reserved_until<=CURRENT_TIMESTAMP RETURNING phone""", (phone, user, key)).fetchone()
        if not holder:
            raise ValueError('الرقم محجوز لمستخدم آخر.')
        db.execute('''INSERT INTO star_purchase_orders
            (order_id,user_id,phone,country_id,calling_code,name,inventory_session,amount,credit_value,number_snapshot)
            VALUES(?,?,?,?,?,?,?,?,?,?)''', (key, user, phone, country[4], country[0], country[1], account[0], amount, country[2],json.dumps(identity,default=str)))
        return {'id': key, 'amount': amount, 'name': country[1]}


async def create(user, phone):
    from helpers import create_user
    await create_user(user)
    return await to_thread(create_sync, user, phone)


def lock_order(db, key, user=None):
    row = db.execute('SELECT user_id,phone FROM star_purchase_orders WHERE order_id=?', (key,)).fetchone()
    if not row or (user is not None and row[0] != user):
        raise ValueError('الطلب غير موجود ضمن حسابك.')
    # Same order as ordinary purchases: user, account, order, reservation.
    db.execute('SELECT user_id FROM users WHERE user_id=? FOR UPDATE', (row[0],))
    db.execute('SELECT phone FROM accounts WHERE phone=? FOR UPDATE', (row[1],))
    return db.execute('''SELECT user_id,phone,amount,status,inventory_session,charge_id,
        expires_at>CURRENT_TIMESTAMP,calling_code,name,credit_value,number_snapshot FROM star_purchase_orders
        WHERE order_id=? FOR UPDATE''', (key,)).fetchone()


def precheckout_sync(key, user, amount, currency):
    with connect() as db:
        row = lock_order(db, key, user)
        account = available(db, row[1])
        if not identity_valid(db,row):
            account = None
        holder = db.execute('SELECT star_order_id,reserved_until>CURRENT_TIMESTAMP FROM account_reservations WHERE phone=? FOR UPDATE',
                            (row[1],)).fetchone()
        if (currency != 'XTR' or amount != row[2] or row[3] != 'pending' or not row[6]
                or not account or not same_session(account[0], row[4]) or not holder or holder[0] != key or not holder[1]):
            return False
        # One accepted checkout per order, including when the invoice was resent.
        if row[3] == 'pending':
            db.execute("UPDATE star_purchase_orders SET status='checkout',expires_at=CURRENT_TIMESTAMP+INTERVAL '15 minutes' WHERE order_id=?", (key,))
            db.execute("UPDATE account_reservations SET reserved_until=CURRENT_TIMESTAMP+INTERVAL '15 minutes' WHERE star_order_id=?", (key,))
        return True


def receipt_sync(key, user, charge, amount, currency):
    if not charge:
        raise ValueError('معرّف الدفع مفقود.')
    with connect() as db:
        row = lock_order(db, key, user)
        if row[5]:
            if row[5] != charge or row[2] != amount or currency != 'XTR':
                raise ValueError('تأكيد الدفع لا يطابق الطلب.')
            return row[3]
        if currency != 'XTR' or row[2] != amount:
            raise ValueError('مبلغ الدفع لا يطابق الطلب.')
        account = available(db, row[1])
        if not identity_valid(db,row):
            account = None
        holder = db.execute('SELECT star_order_id FROM account_reservations WHERE phone=? FOR UPDATE', (row[1],)).fetchone()
        # Late confirmations are refundable, even if the reservation was already reassigned.
        deliverable = row[3] in {'pending', 'checkout'} and row[6] and account and same_session(account[0], row[4]) and holder and holder[0] == key
        state = 'paid' if deliverable else 'refund_pending'
        db.execute('''UPDATE star_purchase_orders SET charge_id=?,status=?,paid_at=CURRENT_TIMESTAMP,
            next_attempt=CURRENT_TIMESTAMP WHERE order_id=?''', (charge, state, key))
        if deliverable:
            db.execute("UPDATE account_reservations SET reserved_until='infinity'::timestamp WHERE star_order_id=?", (key,))
        else:
            db.execute('DELETE FROM account_reservations WHERE star_order_id=?', (key,))
        enqueue(db, user, f'stars:paid:{key}', '✅ تم تأكيد الدفع بالنجوم. يمكنك متابعة الطلب من /paysupport.')
        return state


def fulfil_sync(key):
    with connect() as db:
        row = lock_order(db, key)
        if row[3] != 'paid':
            return row[3]
        user, phone = row[:2]
        account = available(db, phone)
        if not identity_valid(db,row):
            account = None
        holder = db.execute('SELECT star_order_id FROM account_reservations WHERE phone=? FOR UPDATE', (phone,)).fetchone()
        if (not account or not same_session(account[0], row[4]) or not holder or holder[0] != key
                or db.execute('SELECT 1 FROM purchased_accounts WHERE phone=?', (phone,)).fetchone()
                or db.execute('SELECT 1 FROM stored_accounts WHERE phone=?', (phone,)).fetchone()):
            db.execute("UPDATE star_purchase_orders SET status='refund_pending',last_error='inventory_unavailable' WHERE order_id=?", (key,))
            db.execute('DELETE FROM account_reservations WHERE star_order_id=?', (key,))
            return 'refund_pending'
        db.execute('DELETE FROM account_reservations WHERE star_order_id=?', (key,))
        db.execute('DELETE FROM accounts WHERE phone=?', (phone,))
        db.execute('''INSERT INTO purchased_accounts(phone,session,user_id,calling_code,name,price,twofa)
            VALUES(?,?,?,?,?,?,?)''', (phone, account[0], user, row[7], row[8], row[9], account[1]))
        db.execute('INSERT INTO stored_accounts(phone,session,user_id,twofa) VALUES(?,?,?,?)', (phone, account[0], user, account[1]))
        db.execute('DELETE FROM pending_purchases WHERE phone=?', (phone,))
        db.execute("UPDATE commerce_quotes SET status='replaced' WHERE service='purchase' AND reference=? AND status='pending'", (phone,))
        # Stars stay distinct from internal balances; credit_value is the historical reporting value.
        from commerce import award_spend
        award_spend(db, user, row[9])
        db.execute('INSERT INTO commerce_sales(user_id,phone,revenue,cost,number_snapshot) VALUES(?,?,?,?,?)', (user, phone, row[9], account[4],row[10]))
        db.execute("UPDATE star_purchase_orders SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE order_id=?", (key,))
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (user, f'stars_number_purchase:{key}:{row[2]}'))
        enqueue(db, user, f'stars:delivered:{key}',
                f'✅ اكتمل شراء الرقم +{phone} مقابل {row[2]} نجمة. افتح «الحسابات المشتراة» لإدارته والحصول على الكود.')
        return 'completed'


def action_sync(user, key, action):
    with connect() as db:
        row = lock_order(db, key, user)
        if action == 'cancel' and row[3] == 'pending':
            db.execute("UPDATE star_purchase_orders SET status='cancelled' WHERE order_id=?", (key,))
            db.execute('DELETE FROM account_reservations WHERE star_order_id=?', (key,))
            return
        if action == 'refund' and row[3] == 'paid':
            db.execute("UPDATE star_purchase_orders SET status='refund_pending',next_attempt=CURRENT_TIMESTAMP WHERE order_id=?", (key,))
            db.execute('DELETE FROM account_reservations WHERE star_order_id=?', (key,))
            return
        raise ValueError('لا يمكن تنفيذ هذا الإجراء في حالة الطلب الحالية.')


def claim_refund_sync(key):
    with connect() as db:
        return db.execute("""UPDATE star_purchase_orders SET refund_lease_until=CURRENT_TIMESTAMP+INTERVAL '2 minutes'
            WHERE order_id=? AND status='refund_pending' AND next_attempt<=CURRENT_TIMESTAMP
            AND (refund_lease_until IS NULL OR refund_lease_until<=CURRENT_TIMESTAMP)
            RETURNING user_id,charge_id""", (key,)).fetchone()


async def refund(client, key):
    claim = await to_thread(claim_refund_sync, key)
    if not claim:
        return
    user, charge = claim
    try:
        ok = await bot_api('refundStarPayment', {'user_id': user, 'telegram_payment_charge_id': charge})
        if not ok:
            raise RuntimeError('refund_not_confirmed')
    except Exception as exc:
        # If Telegram processed a refund before a crash, a retry reports it as already refunded.
        if getattr(exc, 'message', '') != 'CHARGE_ALREADY_REFUNDED':
            logger.warning('Stars refund deferred: order=%s error=%s', key, type(exc).__name__)
            await query("""UPDATE star_purchase_orders SET refund_lease_until=NULL,
                next_attempt=CURRENT_TIMESTAMP+INTERVAL '1 minute',last_error=? WHERE order_id=? AND status='refund_pending'""",
                (type(exc).__name__, key), commit=True)
            return
    await to_thread(finish_refund_sync, key, user)


def finish_refund_sync(key, user):
    with connect() as db:
        if db.execute("""UPDATE star_purchase_orders SET status='refunded',refunded_at=CURRENT_TIMESTAMP,
            refund_lease_until=NULL,last_error=NULL WHERE order_id=? AND status='refund_pending' RETURNING order_id""", (key,)).fetchone():
            db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (user, f'stars_refunded:{key}'))
            enqueue(db, user, f'stars:refunded:{key}', f'✅ تمت إعادة نجوم الطلب {key} إلى حسابك في تيليجرام.')


async def process(client, key):
    state = await to_thread(fulfil_sync, key)
    if state == 'refund_pending':
        await refund(client, key)
    return state


async def receive(client, key, user, charge, amount, currency='XTR'):
    await to_thread(receipt_sync, key, user, charge, amount, currency)
    return await process(client, key)


async def reconcile(client):
    """Resume receipt reconciliation, delivery and refunds after downtime, one history page per tick."""
    purchases = await query('SELECT 1 FROM star_purchase_orders LIMIT 1', fetchone=True)
    wallet_orders = await query("SELECT 1 FROM payment_orders WHERE method='stars' LIMIT 1", fetchone=True)
    if not purchases and not wallet_orders:
        return
    # Rescan from the start after the final page so newly inserted transactions are never skipped.
    cursor = await query("SELECT value FROM settings WHERE key='stars_purchase_scan_offset'", fetchone=True)
    offset = int(cursor[0]) if cursor else 0
    try:
        result = await bot_api('getStarTransactions', {'offset': offset, 'limit': 100})
    except PaymentApiError as exc:
        if exc.message != 'TELEGRAM_PAYMENT_API_UNAVAILABLE':
            raise
        logger.warning('Stars HTTP unavailable (%s); using the connected Telegram client', exc.cause or 'network')
        return await reconcile_native(client)
    transactions = result.get('transactions', [])
    for tx in transactions:
        source = tx.get('source') or {}
        payload = source.get('invoice_payload', '')
        if source.get('type') != 'user' or source.get('transaction_type') != 'invoice_payment' or not payload:
            continue
        user = (source.get('user') or {}).get('id')
        amount = tx.get('amount')
        if user is None or amount is None or amount <= 0 or tx.get('nanostar_amount', 0):
            continue
        try:
            if payload.startswith(PREFIX):
                await receive(client, payload[len(PREFIX):], user, tx['id'], amount)
            else:
                from handlers.payment_handlers import complete_payment
                ok, result_user = await complete_payment(payload, tx['id'], amount=amount, currency='XTR', payer_id=user)
                if ok and result_user != 'already completed':
                    await client.send_message(result_user, '✅ تم شحن رصيدك تلقائياً بعد استعادة سجل دفع النجوم.')
        except ValueError:
            logger.warning('Ignored mismatched Stars history transaction: id=%s', tx['id'])
    await query("""INSERT INTO settings(key,value) VALUES('stars_purchase_scan_offset',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (str(offset + len(transactions)) if len(transactions) == 100 else '0',), commit=True)


async def recover(client):
    await to_thread(expire_sync)
    rows = await query("""SELECT order_id FROM star_purchase_orders
        WHERE status IN ('paid','refund_pending') AND next_attempt<=CURRENT_TIMESTAMP ORDER BY paid_at LIMIT 30""", fetchall=True)
    for (key,) in rows or []:
        try:
            await process(client, key)
        except Exception:
            logger.exception('Stars purchase recovery deferred: order=%s', key)


def expire_sync():
    with connect() as db:
        expire(db)


async def reconcile_native(client):
    """Use Telegram's authenticated transaction history when Bot API HTTP is unavailable."""
    cursor = await query("SELECT value FROM settings WHERE key='stars_native_scan_offset'", fetchone=True)
    offset = cursor[0] if cursor else ''
    result = await client(functions.payments.GetStarsTransactionsRequest(
        peer=types.InputPeerSelf(), offset=offset, limit=100, inbound=True))
    for tx in result.history or []:
        payload = getattr(tx, 'bot_payload', None)
        if not payload or getattr(tx, 'refund', False) or getattr(tx, 'failed', False) or getattr(tx, 'pending', False):
            continue
        try:
            payload = payload.decode('utf-8')
        except UnicodeDecodeError:
            continue
        peer = getattr(tx.peer, 'peer', None)
        if not isinstance(peer, types.PeerUser) or not isinstance(tx.amount, types.StarsAmount):
            continue
        if tx.amount.amount <= 0 or tx.amount.nanos:
            continue
        try:
            if payload.startswith(PREFIX):
                await receive(client, payload[len(PREFIX):], peer.user_id, tx.id, tx.amount.amount)
            else:
                from handlers.payment_handlers import complete_payment
                ok, result_user = await complete_payment(payload, tx.id, amount=tx.amount.amount,
                                                         currency='XTR', payer_id=peer.user_id)
                if ok and result_user != 'already completed':
                    await client.send_message(result_user, '✅ تم شحن رصيدك تلقائياً بعد استعادة سجل دفع النجوم.')
        except ValueError:
            logger.warning('Ignored mismatched native Stars receipt: id=%s', tx.id)
    next_offset = result.next_offset or ''
    if next_offset and next_offset == offset:
        raise RuntimeError('Stars pagination did not advance')
    await query("INSERT INTO settings(key,value) VALUES('stars_native_scan_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (next_offset,), commit=True)
