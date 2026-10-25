"""Period comparisons, recognized inventory costs and safe CSV exports."""
import csv
import io
from decimal import Decimal

from commerce import money
from postgres import connect
from utils.asyncio_compat import to_thread
from workflow import query

PERIODS = {'day': 1, 'week': 7, 'month': 30, 'quarter': 90}


def csv_file(name, headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(headers)
    for row in rows:
        # Spreadsheet formula injection protection, including leading whitespace.
        writer.writerow(["'" + str(v) if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@'))
                         else ('' if v is None else v) for v in row])
    result = io.BytesIO(('\ufeff' + stream.getvalue()).encode('utf-8'))
    result.name = name
    return result


def period_stats(db, days, offset):
    args = (offset, offset+days)
    period = "created_at<CURRENT_TIMESTAMP-(? * INTERVAL '1 day') AND created_at>=CURRENT_TIMESTAMP-(? * INTERVAL '1 day')"
    row = db.execute(f'''SELECT
        COALESCE(-SUM(amount) FILTER(WHERE operation='number_purchase'),0),
        COALESCE(-SUM(amount) FILTER(WHERE operation IN ('number_rental','rental_renewal')),0),
        COALESCE(SUM(fee),0),COUNT(*) FILTER(WHERE operation='number_purchase')
        FROM balance_ledger WHERE {period}''', args).fetchone()
    costs = db.execute(f'SELECT COALESCE(SUM(cost),0),COUNT(cost) FROM commerce_sales WHERE {period}', args).fetchone()
    expenses = db.execute(f'SELECT COALESCE(SUM(amount),0) FROM business_expenses WHERE {period}', args).fetchone()[0]
    revenue = sum((Decimal(str(v)) for v in row[:3]), Decimal('0'))
    unknown = max(0, int(row[3])-int(costs[1]))
    return {'sales': money(row[0]), 'rentals': money(row[1]), 'fees': money(row[2]),
            'revenue': money(revenue), 'costs': money(costs[0]), 'expenses': money(expenses),
            'profit': revenue-Decimal(str(costs[0]))-Decimal(str(expenses)), 'unknown_costs': unknown,
            'sale_count': row[3]}


def report_sync(period):
    if period not in PERIODS:
        raise ValueError('فترة غير صالحة.')
    days = PERIODS[period]
    with connect() as db:
        # Both periods are read from one consistent snapshot.
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        current, previous = period_stats(db, days, 0), period_stats(db, days, days)
        return {'days': days, 'current': current, 'previous': previous,
                'change': current['revenue']-previous['revenue']}


async def report(period):
    return await to_thread(report_sync, period)


async def statement_csv(user, period):
    if period not in PERIODS | {'all': 0}:
        raise ValueError('فترة غير صالحة.')
    condition = '' if period == 'all' else "AND created_at>=CURRENT_TIMESTAMP-(? * INTERVAL '1 day')"
    args = (user,) if period == 'all' else (user, PERIODS[period])
    rows = await query(f'''SELECT entry_id,created_at,operation,reference,amount,fee,
        balance_after-amount AS balance_before,balance_after,number_snapshot FROM balance_ledger
        WHERE user_id=? {condition} ORDER BY entry_id DESC LIMIT 10001''', args, fetchall=True) or []
    if len(rows) > 10000:
        raise ValueError('عدد الحركات يتجاوز 10000؛ اختر فترة أقصر.')
    return csv_file('wallet-statement.csv', ['id', 'date', 'operation', 'reference', 'amount', 'fee', 'before', 'after','number_snapshot'], rows)


def report_csv(data):
    keys = ['sales', 'rentals', 'fees', 'revenue', 'costs', 'expenses', 'profit', 'unknown_costs', 'sale_count']
    return csv_file('business-report.csv', ['metric', 'current', 'previous'],
                    [(k, data['current'][k], data['previous'][k]) for k in keys])


def record_cost_sync(actor, phone, amount):
    amount = money(amount)
    with connect() as db:
        from staff_security import permitted
        if not permitted(db, actor, 'finance'):
            raise ValueError('صلاحية المالية مطلوبة.')
        old = db.execute('SELECT acquisition_cost FROM accounts WHERE phone=? FOR UPDATE', (phone,)).fetchone()
        if not db.execute('UPDATE accounts SET acquisition_cost=? WHERE phone=? RETURNING phone', (amount, phone)).fetchone():
            raise ValueError('الرقم غير موجود في المخزون.')
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (actor, f'inventory_cost:{phone}:{old[0] if old else None}->{amount}'))


async def record_cost(*args):
    await to_thread(record_cost_sync, *args)


def expense_sync(actor, amount, description):
    amount = money(amount, positive=True)
    description = str(description).strip()
    if not description or len(description) > 500:
        raise ValueError('الوصف مطلوب وبحد أقصى 500 حرف.')
    with connect() as db:
        from staff_security import permitted
        if not permitted(db, actor, 'finance'):
            raise ValueError('صلاحية المالية مطلوبة.')
        eid = db.execute('INSERT INTO business_expenses(amount,description,created_by) VALUES(?,?,?) RETURNING expense_id',
                          (amount, description, actor)).fetchone()[0]
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (actor, f'expense_created:{eid}:{amount}'))
        return eid


async def record_expense(*args):
    return await to_thread(expense_sync, *args)
