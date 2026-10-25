"""User-scoped statement built from committed ledger entries and payment orders.

Balances before a ledger movement are exact: balance_after - signed amount.
Uncredited orders have no balance snapshot. Historic fee NULL stays unknown.
"""
from decimal import Decimal
import re

from helpers import execute_query

PAGE_SIZE = 8
OPERATIONS = {
    'payment_completed': 'شحن الرصيد', 'payment_order': 'فاتورة شحن',
    'number_purchase': 'شراء رقم', 'number_sale': 'بيع رقم', 'account_sale_credit': 'بيع حساب',
    'transfer_sent': 'تحويل صادر', 'transfer_received': 'تحويل وارد',
    'withdrawal_requested': 'سحب رصيد', 'number_rental': 'إيجار أرقام', 'rental_renewal': 'تجديد إيجار',
    'auction_purchase': 'شراء في مزاد', 'auction_sale': 'بيع في مزاد',
    'username_auction_purchase': 'شراء يوزر في مزاد',
    'username_auction_sale': 'بيع يوزر في مزاد',
    'admin_credit': 'إضافة إدارية', 'admin_debit': 'خصم إداري',
    'balance_set': 'تعديل إداري للرصيد', 'referral_reward': 'مكافأة إحالة',
    'withdrawal_refund': 'استرداد سحب', 'refund': 'استرداد مبلغ',
    'promotion_purchase': 'طلب رشق', 'promotion_refund': 'استرداد رشق',
    'api_subscription': 'اشتراك API',
}
GROUPS = {
    'all': ('الكل', None),
    'topup': ('الشحن', ('payment_completed', 'payment_order')),
    'trade': ('الشراء والبيع', ('number_purchase', 'number_sale', 'account_sale_credit')),
    'transfer': ('التحويلات', ('transfer_sent', 'transfer_received')),
    'withdraw': ('السحب', ('withdrawal_requested', 'withdrawal_refund')),
    'auction': ('المزادات', ('auction_purchase', 'auction_sale', 'username_auction_purchase', 'username_auction_sale')),
    'rental': ('الإيجارات', ('number_rental', 'rental_renewal')),
    'promotion': ('الرشق', ('promotion_purchase', 'promotion_refund')),
    'other': ('أخرى', ('admin_credit', 'admin_debit', 'balance_set', 'referral_reward', 'refund', 'api_subscription')),
}
PERIODS = {0: 'كل الفترات', 7: 'آخر 7 أيام', 30: 'آخر 30 يومًا', 90: 'آخر 90 يومًا'}
STATUS = {
    'completed': 'مكتملة ✅', 'paid': 'مكتملة ✅', 'pending': 'معلقة ⏳',
    'rejected': 'مرفوضة ❌', 'cancelled': 'ملغاة', 'canceled': 'ملغاة',
    'expired': 'منتهية', 'failed': 'فاشلة', 'refunded': 'مستردة',
    'unknown': 'غير متحققة — بيانات الحالة غير متوفرة',
}

# Both branches are ownership-scoped before pagination/search. A credited
# payment appears once, as its immutable ledger entry, never twice.
SOURCE = '''WITH movements AS (
    SELECT 'L' || l.entry_id::text AS id, l.operation, l.amount,
           l.balance_after-l.amount AS balance_before, l.balance_after,
           l.fee, l.reference, l.created_at,
           CASE WHEN l.operation='withdrawal_requested'
                THEN COALESCE(w.status, 'unknown') ELSE 'completed' END AS status,
           ABS(l.amount) AS requested_amount,l.number_snapshot
    FROM balance_ledger l
    LEFT JOIN withdrawal_requests w ON l.operation='withdrawal_requested'
         AND w.request_id::text=l.reference AND w.user_id=l.user_id
    WHERE l.user_id=?
    UNION ALL
    SELECT 'P' || p.order_id, 'payment_order', NULL::numeric,
           NULL::numeric, NULL::numeric, NULL::numeric, p.order_id,
           p.created_at, p.status, p.credits,NULL::text
    FROM payment_orders p WHERE p.user_id=? AND NOT EXISTS (
        SELECT 1 FROM balance_ledger l WHERE l.user_id=p.user_id
        AND l.operation='payment_completed' AND l.reference=p.order_id
    )
)'''
FIELDS = ('id', 'operation', 'amount', 'balance_before', 'balance_after',
          'fee', 'reference', 'created_at', 'status', 'requested_amount','number_snapshot')


def validate_id(value):
    value = str(value or '').strip().lstrip('#')
    if value.isascii() and value.isdigit():
        value = 'L' + value
    if not re.fullmatch(r'(?:L[0-9]{1,19}|P[A-Za-z0-9_-]{1,48})', value):
        raise ValueError('invalid statement identifier')
    return value


async def list_entries(user_id, group='all', days=0, page=0):
    if group not in GROUPS or days not in PERIODS or not 0 <= page <= 10000:
        raise ValueError('invalid statement filter')
    predicates = []
    args = [user_id, user_id]
    if GROUPS[group][1]:
        predicates.append('operation=ANY(?)')
        args.append(list(GROUPS[group][1]))
    if days:
        predicates.append("created_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 day')")
        args.append(days)
    where = ' WHERE ' + ' AND '.join(predicates) if predicates else ''
    rows = await execute_query(
        SOURCE + ' SELECT * FROM movements' + where +
        ' ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?',
        tuple(args + [PAGE_SIZE + 1, page * PAGE_SIZE]), fetchall=True,
    ) or []
    return [dict(zip(FIELDS, row)) for row in rows[:PAGE_SIZE]], len(rows) > PAGE_SIZE


async def get_entry(user_id, identifier):
    identifier = validate_id(identifier)
    # Old invoice buttons/tickets remain valid after payment creates a ledger
    # entry and the uncredited-order row disappears from the list.
    payment_reference = identifier[1:] if identifier.startswith('P') else None
    row = await execute_query(SOURCE + ''' SELECT * FROM movements
        WHERE id=? OR (operation='payment_completed' AND reference=?)''',
        (user_id, user_id, identifier, payment_reference), fetchone=True)
    return dict(zip(FIELDS, row)) if row else None


def money(amount):
    return 'غير مسجل' if amount is None else f'{Decimal(str(amount)):,.2f}$'


def format_entry(entry):
    operation = entry['operation']
    delta, fee = entry['amount'], entry['fee']
    lines = [
        f"🧾 العملية #{entry['id']}",
        f"النوع: {OPERATIONS.get(operation, 'حركة رصيد أخرى')}",
        f"التاريخ: {entry['created_at'].strftime('%Y-%m-%d %H:%M:%S')} (توقيت قاعدة البيانات)",
        f"الحالة: {STATUS.get(entry['status'], 'غير معروفة')}",
    ]
    if entry.get('number_snapshot'):
        import json
        from number_catalog import TYPES,policy_text
        saved=json.loads(entry['number_snapshot'])
        lines += [f"نوع الرقم: {TYPES.get(saved.get('number_type'),'غير مصنف')}",
                  f"إصدار التسعير: {saved.get('pricing_version','غير مسجل')}"]
        if saved.get('warranty'):
            lines.append(f"الضمان المسجل سابقًا: {policy_text(saved['warranty'])}")
    if delta is None:
        lines += [f"قيمة الشحن المطلوبة: {money(entry['requested_amount'])}",
                  'لا توجد حركة رصيد مسجلة لهذه الفاتورة؛ لا تُحتسب كإضافة إلى الرصيد.',
                  'الرصيد قبل/بعد: غير مسجل']
    else:
        # All amounts are wallet dollars, not TON or Stars provider units.
        principal = None if fee is None else abs(delta) + (fee if delta >= 0 else -fee)
        lines += [f"المبلغ قبل العمولة: {money(principal)}",
                  f"العمولة: {money(fee)}",
                  f"صافي حركة الرصيد: {'+' if delta > 0 else '−'}{money(abs(delta))}",
                  f"الرصيد قبل: {money(entry['balance_before'])}",
                  f"الرصيد بعد: {money(entry['balance_after'])}"]
        if fee is None:
            lines.append('السجل القديم لا يفصل العمولة؛ لم تُقدَّر بأثر رجعي.')
        if operation == 'withdrawal_requested':
            lines.append('خُصم هذا المبلغ عند إنشاء طلب السحب؛ اكتمال الصرف يعتمد على حالة الطلب.')
        if 'refund' in operation:
            lines.append('هذه حركة استرداد مستقلة عن العملية الأصلية.')
    if operation in {'transfer_sent', 'transfer_received'}:
        lines.append(f"الطرف الآخر: {entry['reference']}")
    elif operation in {'withdrawal_requested', 'payment_order', 'payment_completed'} or 'auction' in operation:
        lines.append(f"مرجع الطلب: {entry['reference'] or '—'}")
    return '\n'.join(lines)
