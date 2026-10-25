"""Country catalog, favorites and persistent stock/price subscriptions."""
from decimal import Decimal

from postgres import connect
from utils.asyncio_compat import to_thread
from workflow import enqueue, query

CATALOG_SQL = """SELECT c.country_id,c.name,c.calling_code,c.price,
    COUNT(a.phone) FROM countries c LEFT JOIN accounts a ON
    (a.country_id=c.country_id OR (a.country_id IS NULL AND a.calling_code=c.calling_code))
    AND a.is_active AND NOT a.is_sold
    AND NOT EXISTS(SELECT 1 FROM account_health h WHERE h.phone=a.phone
        AND h.inventory_session=a.session AND h.status IN ('revoked','banned','deleted','restricted','invalid','mismatch'))
    AND NOT EXISTS(SELECT 1 FROM account_reservations r WHERE r.phone=a.phone AND r.reserved_until>CURRENT_TIMESTAMP)
    WHERE c.is_active GROUP BY c.country_id,c.name,c.calling_code,c.price ORDER BY c.name"""


async def catalog(user):
    from number_catalog import JOINS,ELIGIBLE,FREE
    rows=await query(f'''SELECT c.country_id,c.name,c.calling_code,p.bot_sell_price,n.number_type,COUNT(*)
        {JOINS} WHERE {ELIGIBLE} AND {FREE} GROUP BY c.country_id,c.name,c.calling_code,p.bot_sell_price,n.number_type
        ORDER BY c.name,n.number_type''',fetchall=True) or []
    watches=await query('SELECT country_id,service,favorite,stock_alert,price_alert FROM country_watches WHERE user_id=?',(user,),fetchall=True) or []
    indexed={(r[0],r[1]):dict(zip(('favorite','stock_alert','price_alert'),r[2:])) for r in watches}
    return [{'id':r[0],'name':r[1],'code':r[2],'price':r[3],'number_type':r[4],'available':r[5],
        'purchase':indexed.get((r[0],'purchase'),{}),'rental':indexed.get((r[0],'rental'),{})} for r in rows]


async def country_inventory(country_id, *, number_type=None, offset=0, limit=20):
    from number_catalog import validate_type,JOINS,ELIGIBLE,FREE
    validate_type(number_type)
    if offset<0 or not 1<=limit<=50:
        raise ValueError('Invalid page')
    return await query(f'''SELECT a.phone {JOINS} WHERE {ELIGIBLE} AND {FREE}
        AND n.country_id=? AND n.number_type=? ORDER BY a.phone LIMIT ? OFFSET ?''',
        (country_id,number_type,limit,offset),fetchall=True) or []


def watch_sync(user, country_id, service, field):
    if service not in {'purchase', 'rental'} or field not in {'favorite', 'stock_alert', 'price_alert'}:
        raise ValueError('اشتراك غير صالح.')
    with connect() as db:
        countries = {r[0]: r for r in db.execute(CATALOG_SQL).fetchall()}
        country = countries.get(country_id)
        if not country:
            raise ValueError('الدولة غير موجودة.')
        rate = db.execute("SELECT value FROM settings WHERE key='rental_daily_price'").fetchone()
        price = country[3] if service == 'purchase' else Decimal(str(rate[0] if rate else 0))
        db.execute('''INSERT INTO country_watches(user_id,country_id,service,available,last_price,last_count)
            VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING''', (user, country_id, service, country[4] > 0, price, country[4]))
        row = db.execute(f'''UPDATE country_watches SET {field}=NOT {field},available=?,last_price=?,last_count=?
            WHERE user_id=? AND country_id=? AND service=? RETURNING {field}''',
            (country[4] > 0, price, country[4], user, country_id, service)).fetchone()
        return row[0]


async def toggle_watch(*args):
    return await to_thread(watch_sync, *args)


def monitor_watches_sync():
    with connect() as db:
        countries = {r[0]: r for r in db.execute(CATALOG_SQL).fetchall()}
        rate = db.execute("SELECT value FROM settings WHERE key='rental_daily_price'").fetchone()
        rental_price = Decimal(str(rate[0] if rate else 0))
        rows = db.execute('''SELECT w.user_id,w.country_id,w.service,w.stock_alert,w.price_alert,
            w.last_price,w.last_count,w.version FROM country_watches w
            WHERE (w.stock_alert OR w.price_alert)
            AND NOT EXISTS(SELECT 1 FROM bad_guys b WHERE b.user_id=w.user_id)
            FOR UPDATE OF w SKIP LOCKED''').fetchall()
        for user, cid, service, stock, price_alert, old_price, old_count, version in rows:
            country = countries.get(cid)
            if not country:
                continue
            price = country[3] if service == 'purchase' else rental_price
            count = country[4] if price is not None and price > 0 else 0
            if old_count == count and old_price == price:
                continue
            events = []
            if stock and count > old_count:
                events.append(('stock',f'توفر مخزون جديد: {count} رقم متاح.'))
            if price_alert and old_price is not None and price is not None and 0 < price < old_price:
                events.append(('price',f'انخفض السعر من {old_price}$ إلى {price}$.'))
            db.execute('''UPDATE country_watches SET available=?,last_count=?,last_price=?,version=version+1
                WHERE user_id=? AND country_id=? AND service=?''', (count > 0, count, price, user, cid, service))
            if events:
                title = 'للشراء' if service == 'purchase' else 'للتأجير / يوم'
                for category,message in events:
                    enqueue(db, user, f'watch:{user}:{cid}:{service}:{version+1}:{category}',
                            f"{country[1]} {title}\n{message}\nافتح «المتجر والمفضلة» لعرض التفاصيل.")


async def monitor_watches():
    await to_thread(monitor_watches_sync)
