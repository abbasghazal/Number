"""Canonical commercial identity; metadata survives moves between account tables."""

import json
from contextvars import ContextVar

from postgres import connect
from utils.asyncio_compat import to_thread
from workflow import query

TYPES = {"spam": "سبام", "special": "مميز", "clean": "سليم", "guaranteed": "ضمان"}
DESCRIPTIONS = {kind: "فئة سعرية مستقلة حسب الدولة." for kind in TYPES}
UNAVAILABLE = "هذا النوع غير متاح حاليًا لهذه الدولة"
submission = ContextVar("number_submission", default=None)


def insert_submission_sync(phone, session, calling_code, twofa, seller_id, data, pending=False):
    """Serialize all submissions for a phone across inventory and owner tables."""
    from helpers import encrypt_session
    from commerce import money

    validate_type(data.get("number_type"))
    if not session:
        raise ValueError("الجلسة غير مكتملة.")
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (phone,))
        from session_lifecycle import require_fresh_session
        require_fresh_session(db,session)
        # ``sold_accounts`` is history, not a permanent ownership lock.  A
        # number bought from the bot must be sellable again by its current
        # owner, and a number whose old authorization was retired must be able
        # to return with a fresh StringSession.
        if db.execute("SELECT 1 FROM pending_sales WHERE phone=?", (phone,)).fetchone():
            return False
        if db.execute("SELECT 1 FROM accounts WHERE phone=?", (phone,)).fetchone():
            return False
        if db.execute(
            "SELECT 1 FROM auctions WHERE phone=? AND session IS NOT NULL AND status NOT IN ('cancelled','completed')",
            (phone,),
        ).fetchone():
            return False
        owned = []
        for table in ("stored_accounts", "purchased_accounts"):
            row = db.execute(f"SELECT user_id FROM {table} WHERE phone=? FOR UPDATE", (phone,)).fetchone()
            if row:
                owned.append(row[0])
        if pending:
            # Re-sale is valid only for the current owner.  Both rows can be
            # present after a purchase because purchased accounts are also
            # installed automatically.
            if any(owner != seller_id for owner in owned):
                return False
        elif owned:
            return False
        country = db.execute(
            "SELECT calling_code FROM countries WHERE country_id=? AND is_active", (data["country_id"],)
        ).fetchone()
        if not country or not str(phone).startswith(country[0]):
            raise ValueError("الرقم لا يطابق الدولة المختارة.")
        if not pending:
            from staff_security import permitted

            if not permitted(db, data["actor"], "inventory"):
                raise ValueError("صلاحية المخزون مطلوبة.")
        from inventory_pricing import seed_type_price
        seed_type_price(db,data['country_id'],data['number_type'])
        price = pricing(db, data["country_id"], data["number_type"], buying=True) if pending else None
        warranty = guarantee(data.get("warranty"))
        db.execute(
            """INSERT INTO number_metadata(phone,country_id,number_type,features,warranty,checked_at,
            check_scope,classified_by,classified_at,reviewed) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?)
            ON CONFLICT(phone) DO UPDATE SET country_id=excluded.country_id,number_type=excluded.number_type,
            features=excluded.features,warranty=excluded.warranty,checked_at=excluded.checked_at,
            check_scope=excluded.check_scope,classified_by=excluded.classified_by,
            classified_at=CURRENT_TIMESTAMP,reviewed=excluded.reviewed,revision=number_metadata.revision+1""",
            (
                phone,
                data["country_id"],
                data["number_type"],
                data.get("features", ""),
                json.dumps(warranty),
                data.get("checked_at"),
                data.get("scope") or "",
                data["actor"],
                # A seller chooses the type directly.  The submitted type and
                # its current buy price become the immutable sale offer, so a
                # staff review is not needed before the account is listed.
                True,
            ),
        )
        db.execute(
            "INSERT INTO number_type_history(phone,actor_id,new_type) VALUES(?,?,?)",
            (phone, data["actor"], data["number_type"]),
        )
        if pending:
            sale_snapshot = snapshot(db, phone, buying=True)
            db.execute(
                """INSERT INTO pending_sales(phone,seller_id,price,calling_code,session,twofa,
                number_type,country_id,pricing_version,number_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    phone,
                    seller_id,
                    money(price[1], positive=True),
                    country[0],
                    encrypt_session(session),
                    encrypt_session(twofa),
                    data["number_type"],
                    data["country_id"],
                    price[4],
                    json.dumps(sale_snapshot, default=str),
                ),
            )
        else:
            db.execute(
                "INSERT INTO accounts(phone,session,calling_code,twofa,seller_id,country_id) VALUES(?,?,?,?,?,?)",
                (phone, encrypt_session(session), country[0], encrypt_session(twofa), seller_id, data["country_id"]),
            )
    return True


def validate_type(value):
    if value not in TYPES:
        raise ValueError("اختر نوع الرقم أولًا.")
    return value


def guarantee(value):
    if not value:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if value == {}:
        return {}
    if not isinstance(value, dict) or not 1 <= int(value.get("days", 0)) <= 3650:
        raise ValueError("حدد مدة الضمان بالأيام.")
    if any(not str(value.get(k, "")).strip() or len(str(value[k])) > 1500 for k in ("coverage", "compensation")):
        raise ValueError("حدد الحالات المشمولة وآلية التعويض.")
    return {k: value[k] for k in ("days", "coverage", "compensation")}


def policy_text(value):
    policy = guarantee(value)
    return (
        f"{policy['days']} يوم؛ {policy['coverage']}؛ التعويض: {policy['compensation']}"
        if policy
        else "لا يوجد ضمان معلن"
    )


def pricing(db, country_id, kind, *, buying=False):
    validate_type(kind)
    row = db.execute(
        """SELECT bot_sell_price,bot_buy_price,sell_enabled,buy_enabled,version,warranty,bot_sell_stars
        FROM number_type_prices WHERE country_id=? AND number_type=? FOR SHARE""",
        (country_id, kind),
    ).fetchone()
    if not row or not row[3 if buying else 2] or row[1 if buying else 0] is None:
        raise ValueError(UNAVAILABLE)
    return row


def snapshot(db, phone, *, buying=False):
    row = db.execute(
        """SELECT n.number_type,n.country_id,n.revision,n.warranty,n.features,
        n.checked_at,n.check_scope FROM number_metadata n JOIN countries c ON c.country_id=n.country_id
        WHERE n.phone=? AND n.reviewed AND c.is_active FOR SHARE OF n,c""",
        (phone,),
    ).fetchone()
    if not row or not row[0]:
        raise ValueError("الرقم بانتظار مراجعة النوع من الإدارة.")
    price = pricing(db, row[1], row[0], buying=buying)
    warranty = guarantee(row[3]) or guarantee(price[5])
    return {
        "number_type": row[0],
        "country_id": row[1],
        "type_revision": row[2],
        "warranty": warranty,
        "features": row[4],
        "checked_at": str(row[5] or ""),
        "check_scope": row[6],
        "pricing_version": price[4],
        "base": price[1 if buying else 0],
        "stars_price": price[6],
    }


def verify_snapshot(db, phone, saved):
    saved = json.loads(saved) if isinstance(saved, str) else saved
    if not saved:
        raise ValueError("اطلب عرض سعر جديدًا بعد تصنيف الرقم.")
    current = snapshot(db, phone)
    if any(current[k] != saved[k] for k in ("number_type", "country_id", "type_revision")):
        raise ValueError("تغير نوع الرقم أو أهليته؛ اطلب عرضًا جديدًا.")
    # A price edit does not invalidate a live quote; availability and identity do.
    return saved


def configure_sync(actor, country, kind, sell, buy, sell_enabled, buy_enabled, warranty=None, stars=None):
    from commerce import money
    from staff_security import permitted

    validate_type(kind)
    warranty = guarantee(warranty)
    if stars is not None and (int(stars) != stars or not 1 <= stars <= 10000000):
        raise ValueError("سعر النجوم غير صالح.")
    with connect() as db:
        if not permitted(db, actor, "finance"):
            raise ValueError("صلاحية المالية مطلوبة.")
        db.execute(
            """INSERT INTO number_type_prices(country_id,number_type,bot_sell_price,bot_buy_price,
            sell_enabled,buy_enabled,warranty,updated_by,bot_sell_stars) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(country_id,number_type) DO UPDATE SET bot_sell_price=excluded.bot_sell_price,
            bot_buy_price=excluded.bot_buy_price,sell_enabled=excluded.sell_enabled,buy_enabled=excluded.buy_enabled,
            warranty=excluded.warranty,version=number_type_prices.version+1,updated_by=excluded.updated_by,
            updated_at=CURRENT_TIMESTAMP,bot_sell_stars=excluded.bot_sell_stars""",
            (
                country,
                kind,
                money(sell, positive=True),
                money(buy, positive=True),
                bool(sell_enabled),
                bool(buy_enabled),
                json.dumps(warranty),
                actor,
                stars,
            ),
        )
        db.execute("INSERT INTO audit_log(user_id,action) VALUES(?,?)", (actor, f"type_price:{country}:{kind}"))


async def configure(*args, **kwargs):
    return await to_thread(configure_sync, *args, **kwargs)


def classify_sync(actor, phone, country, kind, features="", warranty=None, evidence=None, reviewed=True):
    from staff_security import permitted

    validate_type(kind)
    warranty = guarantee(warranty)
    with connect() as db:
        if reviewed and not permitted(db, actor, "inventory"):
            raise ValueError("صلاحية المخزون مطلوبة لاعتماد التصنيف.")
        country_row = db.execute("SELECT calling_code FROM countries WHERE country_id=?", (country,)).fetchone()
        if not country_row or not str(phone).startswith(country_row[0]):
            raise ValueError("الرقم لا يطابق الدولة المختارة.")
        if reviewed:
            from inventory_pricing import seed_type_price
            seed_type_price(db,country,kind)
        db.execute("SELECT phone FROM accounts WHERE phone=? FOR UPDATE", (phone,))
        old = db.execute("SELECT number_type FROM number_metadata WHERE phone=? FOR UPDATE", (phone,)).fetchone()
        db.execute(
            """INSERT INTO number_metadata(phone,country_id,number_type,features,warranty,checked_at,
            check_scope,classified_by,reviewed) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET
            country_id=excluded.country_id,number_type=excluded.number_type,features=excluded.features,
            warranty=excluded.warranty,checked_at=excluded.checked_at,check_scope=excluded.check_scope,
            classified_by=excluded.classified_by,classified_at=CURRENT_TIMESTAMP,
            revision=number_metadata.revision+1,reviewed=excluded.reviewed""",
            (
                phone,
                country,
                kind,
                features,
                json.dumps(warranty),
                (evidence or {}).get("checked_at"),
                (evidence or {}).get("scope") or "",
                actor,
                reviewed,
            ),
        )
        db.execute(
            """INSERT INTO number_type_history(phone,actor_id,old_type,new_type)
            VALUES(?,?,?,?)""",
            (phone, actor, old[0] if old else None, kind),
        )


async def classify(*args, **kwargs):
    return await to_thread(classify_sync, *args, **kwargs)


def installed_metadata(db, phone, actor, data):
    """Save a user's proposed type without claiming administrative verification."""
    validate_type(data.get("number_type"))
    country = db.execute("SELECT calling_code FROM countries WHERE country_id=?", (data["country_id"],)).fetchone()
    if not country or not str(phone).startswith(country[0]):
        raise ValueError("الرقم لا يطابق الدولة المختارة.")
    row = db.execute(
        """INSERT INTO number_metadata(phone,country_id,number_type,features,warranty,
        classified_by,classified_at,reviewed) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP,FALSE)
        ON CONFLICT(phone) DO UPDATE SET country_id=excluded.country_id,number_type=excluded.number_type,
        features=excluded.features,warranty=excluded.warranty,classified_by=excluded.classified_by,
        classified_at=CURRENT_TIMESTAMP,revision=number_metadata.revision+1 WHERE NOT number_metadata.reviewed RETURNING phone""",
        (
            phone,
            data["country_id"],
            data["number_type"],
            data.get("features", ""),
            json.dumps(guarantee(data.get("warranty"))),
            actor,
        ),
    ).fetchone()
    if row:
        db.execute(
            "INSERT INTO number_type_history(phone,actor_id,new_type) VALUES(?,?,?)",
            (phone, actor, data["number_type"]),
        )


ELIGIBLE = """a.is_active AND NOT a.is_sold AND n.reviewed AND c.is_active AND p.sell_enabled
    AND p.bot_sell_price IS NOT NULL
    AND NOT EXISTS(SELECT 1 FROM account_health h WHERE h.phone=a.phone AND h.inventory_session=a.session
        AND h.status IN ('revoked','banned','deleted','restricted','invalid','mismatch'))"""
FREE = """NOT EXISTS(SELECT 1 FROM account_reservations r WHERE r.phone=a.phone AND r.reserved_until>CURRENT_TIMESTAMP)
    AND NOT EXISTS(SELECT 1 FROM deferred_purchases d WHERE d.phone=a.phone AND d.status='pending')"""
JOINS = """FROM accounts a JOIN number_metadata n ON n.phone=a.phone
    JOIN countries c ON c.country_id=n.country_id
    JOIN number_type_prices p ON p.country_id=n.country_id AND p.number_type=n.number_type"""


async def countries(kind):
    validate_type(kind)
    return (
        await query(
            f"""SELECT c.country_id,c.name,p.bot_sell_price,COUNT(*) FILTER(WHERE {FREE}),COUNT(*) {JOINS}
        WHERE {ELIGIBLE} AND n.number_type=?
        GROUP BY c.country_id,c.name,p.bot_sell_price ORDER BY c.name""",
            (kind,),
            fetchall=True,
        )
        or []
    )


async def inventory(kind, country, availability="available", page=0):
    validate_type(kind)
    if availability not in {"available", "reserved", "all"} or page < 0:
        raise ValueError("مرشح غير صالح.")
    condition = FREE if availability == "available" else f"NOT ({FREE})" if availability == "reserved" else "TRUE"
    return (
        await query(
            f"""SELECT a.phone,COUNT(*) OVER() {JOINS} WHERE {ELIGIBLE}
        AND n.number_type=? AND n.country_id=? AND ({condition}) ORDER BY a.phone LIMIT 20 OFFSET ?""",
            (kind, country, page * 20),
            fetchall=True,
        )
        or []
    )


async def detail(phone):
    row = await query(
        """SELECT number_type,features,warranty,checked_at,check_scope,reviewed
        FROM number_metadata WHERE phone=?""",
        (phone,),
        fetchone=True,
    )
    if not row:
        return "النوع: بانتظار المراجعة"
    return (
        f"النوع: {TYPES.get(row[0], 'بانتظار المراجعة')}\nاعتماد التصنيف: {'معتمد' if row[5] else 'مبدئي بانتظار المراجعة'}"
    )
