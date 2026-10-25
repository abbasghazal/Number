"""Paid external API access for reusing the bot backend from other clients."""

import hashlib
import json
import os
import re
import secrets
from decimal import Decimal
from urllib.parse import urlsplit

from aiohttp import web

from postgres import connect
from utils.asyncio_compat import to_thread
from workflow import query

PLANS = {
    "monthly": (30, "شهري"),
    "yearly": (365, "سنوي"),
}
DEFAULT_PRICES = {
    "monthly": Decimal("10.00"),
    "yearly": Decimal("100.00"),
}
KEY_PREFIX = "sk_live_"


def api_base_url():
    explicit = os.getenv("API_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    render = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render:
        return render + "/external/v1"
    mini = os.getenv("MINI_APP_URL", "").strip()
    if mini:
        parsed = urlsplit(mini)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/external/v1"
    return "/external/v1"


def migrate(c):
    c.execute(
        """CREATE TABLE IF NOT EXISTS api_subscriptions(
        user_id BIGINT PRIMARY KEY,plan TEXT NOT NULL CHECK(plan IN ('monthly','yearly')),
        starts_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS api_keys(
        key_id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,token_hash TEXT UNIQUE NOT NULL,key_prefix TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,last_used_at TIMESTAMPTZ,revoked_at TIMESTAMPTZ)"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS api_keys_owner_idx ON api_keys(user_id,created_at DESC)")
    c.execute(
        """CREATE TABLE IF NOT EXISTS api_usage(
        request_id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,key_id TEXT NOT NULL,path TEXT NOT NULL,
        method TEXT NOT NULL,status INTEGER,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS api_usage_rate_idx ON api_usage(key_id,created_at DESC)")
    for plan, amount in DEFAULT_PRICES.items():
        c.execute(
            """INSERT INTO settings(key,value,description) VALUES(?,?,?) ON CONFLICT(key) DO NOTHING""",
            (f"api_{plan}_price", str(amount), f"سعر اشتراك API {PLANS[plan][1]}"),
        )


def _money(value):
    try:
        result = Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception as exc:
        raise ValueError("سعر اشتراك API غير صالح.") from exc
    if not result.is_finite() or result <= 0 or result > Decimal("1000000.00"):
        raise ValueError("سعر اشتراك API غير صالح.")
    return result


def _price(db, plan):
    if plan not in PLANS:
        raise ValueError("خطة API غير صالحة.")
    row = db.execute("SELECT value FROM settings WHERE key=?", (f"api_{plan}_price",)).fetchone()
    return _money(row[0] if row and row[0] else DEFAULT_PRICES[plan])


def subscribe_sync(user, plan):
    days, label = PLANS.get(plan, (None, None))
    if not days:
        raise ValueError("خطة API غير صالحة.")
    with connect() as db:
        account = db.execute("SELECT coins FROM users WHERE user_id=? FOR UPDATE", (user,)).fetchone()
        if not account:
            raise ValueError("حساب المستخدم غير موجود.")
        price = _price(db, plan)
        if Decimal(str(account[0])) < price:
            raise ValueError("رصيدك غير كافٍ للاشتراك في API.")
        updated = db.execute(
            "UPDATE users SET coins=coins-? WHERE user_id=? AND coins>=? RETURNING coins",
            (price, user, price),
        ).fetchone()
        if not updated:
            raise ValueError("رصيدك غير كافٍ للاشتراك في API.")
        sub = db.execute(
            "SELECT expires_at FROM api_subscriptions WHERE user_id=? FOR UPDATE", (user,)
        ).fetchone()
        row = db.execute(
            """INSERT INTO api_subscriptions(user_id,plan,starts_at,expires_at,updated_at)
            VALUES(?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP + ? * INTERVAL '1 day',CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan,
            starts_at=CASE WHEN api_subscriptions.expires_at<=CURRENT_TIMESTAMP THEN CURRENT_TIMESTAMP ELSE api_subscriptions.starts_at END,
            expires_at=GREATEST(api_subscriptions.expires_at,CURRENT_TIMESTAMP) + ? * INTERVAL '1 day',
            updated_at=CURRENT_TIMESTAMP RETURNING expires_at""",
            (user, plan, days, days),
        ).fetchone()
        db.execute(
            """INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,fee)
            VALUES(?,?,?,'api_subscription',?,0)""",
            (user, -price, updated[0], f"{plan}:{row[0]}"),
        )
        db.execute(
            "INSERT INTO audit_log(user_id,action) VALUES(?,?)",
            (user, f"api_subscription:{plan}:{price}"),
        )
        return {"plan": plan, "label": label, "price": price, "expires_at": row[0], "balance": updated[0], "extended": bool(sub)}


async def subscribe(user, plan):
    return await to_thread(subscribe_sync, user, plan)


def set_price_sync(actor, plan, amount):
    from config import DEVELOPER_ID

    if not DEVELOPER_ID or int(actor) != int(DEVELOPER_ID):
        raise PermissionError("هذه العملية متاحة للمطور فقط.")
    if plan not in PLANS:
        raise ValueError("خطة API غير صالحة.")
    price = _money(amount)
    with connect() as db:
        db.execute(
            """INSERT INTO settings(key,value,description,last_updated) VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,description=excluded.description,last_updated=CURRENT_TIMESTAMP""",
            (f"api_{plan}_price", str(price), f"سعر اشتراك API {PLANS[plan][1]}"),
        )
        db.execute(
            "INSERT INTO audit_log(user_id,action) VALUES(?,?)",
            (actor, f"api_price:{plan}:{price}"),
        )
    return price


async def set_price(actor, plan, amount):
    return await to_thread(set_price_sync, actor, plan, amount)


def issue_key_sync(user, *, rotate=False):
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    key_id = secrets.token_hex(12)
    prefix = raw[:18]
    with connect() as db:
        sub = db.execute(
            "SELECT expires_at>CURRENT_TIMESTAMP FROM api_subscriptions WHERE user_id=? FOR UPDATE", (user,)
        ).fetchone()
        if not sub or not sub[0]:
            raise ValueError("تحتاج إلى اشتراك API فعال أولًا.")
        active = db.execute(
            "SELECT key_id FROM api_keys WHERE user_id=? AND revoked_at IS NULL ORDER BY created_at DESC FOR UPDATE",
            (user,),
        ).fetchall()
        if active and not rotate:
            raise ValueError("لديك مفتاح API فعال. استخدم تدوير المفتاح إذا أردت استبداله.")
        if active:
            db.execute("UPDATE api_keys SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND revoked_at IS NULL", (user,))
        db.execute(
            "INSERT INTO api_keys(key_id,user_id,token_hash,key_prefix) VALUES(?,?,?,?)",
            (key_id, user, digest, prefix),
        )
        db.execute("INSERT INTO audit_log(user_id,action) VALUES(?,?)", (user, "api_key_rotated" if active else "api_key_created"))
    return raw


async def issue_key(user, *, rotate=False):
    return await to_thread(issue_key_sync, user, rotate=rotate)


def revoke_key_sync(user):
    with connect() as db:
        row = db.execute(
            "UPDATE api_keys SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND revoked_at IS NULL RETURNING key_id",
            (user,),
        ).fetchone()
        if row:
            db.execute("INSERT INTO audit_log(user_id,action) VALUES(?,?)", (user, "api_key_revoked"))
        return bool(row)


async def revoke_key(user):
    return await to_thread(revoke_key_sync, user)


async def status(user):
    sub = await query(
        "SELECT plan,starts_at,expires_at,expires_at>CURRENT_TIMESTAMP FROM api_subscriptions WHERE user_id=?",
        (user,), fetchone=True,
    )
    key = await query(
        "SELECT key_prefix,created_at,last_used_at FROM api_keys WHERE user_id=? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (user,), fetchone=True,
    )
    with connect() as db:
        prices = {plan: str(_price(db, plan)) for plan in PLANS}
    return {
        "active": bool(sub and sub[3]),
        "plan": sub[0] if sub else None,
        "starts_at": sub[1] if sub else None,
        "expires_at": sub[2] if sub else None,
        "key_prefix": key[0] if key else None,
        "key_created_at": key[1] if key else None,
        "last_used_at": key[2] if key else None,
        "prices": prices,
    }


async def authenticate(request):
    auth = request.headers.get("Authorization", "")
    if not re.fullmatch(r"Bearer\s+sk_live_[A-Za-z0-9_-]{20,128}", auth):
        raise web.HTTPUnauthorized(text="مفتاح API مفقود أو غير صالح.")
    raw = auth.split(None, 1)[1]
    digest = hashlib.sha256(raw.encode()).hexdigest()
    row = await query(
        """SELECT k.key_id,k.user_id FROM api_keys k JOIN api_subscriptions s ON s.user_id=k.user_id
        WHERE k.token_hash=? AND k.revoked_at IS NULL AND s.expires_at>CURRENT_TIMESTAMP""",
        (digest,), fetchone=True,
    )
    if not row:
        raise web.HTTPUnauthorized(text="مفتاح API غير فعال أو الاشتراك منتهٍ.")
    count = await query(
        "SELECT COUNT(*) FROM api_usage WHERE key_id=? AND created_at>CURRENT_TIMESTAMP-INTERVAL '1 minute'",
        (row[0],), fetchone=True,
    )
    if count and count[0] >= 120:
        raise web.HTTPTooManyRequests(text="تم تجاوز حد 120 طلبًا في الدقيقة.")
    from helpers import is_banned, get_user
    if await is_banned(row[1]) or not await get_user(row[1]):
        raise web.HTTPForbidden(text="حساب مالك المفتاح غير متاح.")
    await query("UPDATE api_keys SET last_used_at=CURRENT_TIMESTAMP WHERE key_id=?", (row[0],), commit=True)
    return row[1], row[0]


async def _log(key_id, user, request, status_code):
    path = request.path[:300]
    await query(
        "INSERT INTO api_usage(user_id,key_id,path,method,status) VALUES(?,?,?,?,?)",
        (user, key_id, path, request.method, status_code), commit=True,
    )


async def public_status(request):
    from miniapp_server import response
    user, key_id = await authenticate(request)
    result = response(await status(user))
    await _log(key_id, user, request, result.status)
    return result


async def services(request):
    from miniapp_server import response
    from service_catalog import available
    user, key_id = await authenticate(request)
    result = response({"services": await available(user), "platform_base": "/external/v1/platform/"})
    await _log(key_id, user, request, result.status)
    return result


async def platform_gateway(request):
    """Expose the same native platform service layer to external clients.

    POST requests use the same durable idempotency store as the Mini App.  A
    rejected request is persisted with its exact 4xx response, while a
    server-side failure is marked retryable.
    """
    from miniapp_server import response
    import platform_api

    user, key_id = await authenticate(request)
    request["user"] = user
    request["external_api"] = True
    status_code = 500
    claim = None
    result = None
    try:
        if request.method == "POST":
            idem = request.headers.get("X-Idempotency-Key", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idem):
                raise web.HTTPBadRequest(text="X-Idempotency-Key مطلوب لطلبات POST.")
            try:
                payload = await request.json()
            except (json.JSONDecodeError, ValueError):
                raise web.HTTPBadRequest(text="أرسل الطلب بصيغة JSON object.") from None
            if not isinstance(payload, dict):
                raise web.HTTPBadRequest(text="أرسل الطلب بصيغة JSON object.")
            fingerprint = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            route = request.path
            inserted = await query(
                """INSERT INTO platform_requests(user_id,request_key,route,body_hash)
                VALUES(?,?,?,?) ON CONFLICT DO NOTHING RETURNING request_key""",
                (user, idem, route, fingerprint), commit=True, fetchone=True,
            )
            if not inserted:
                old = await query(
                    """SELECT route,body_hash,status,result,http_status,updated_at
                    FROM platform_requests WHERE user_id=? AND request_key=?""",
                    (user, idem), fetchone=True,
                )
                if not old or old[:2] != (route, fingerprint):
                    raise web.HTTPConflict(text="معرف الطلب مستخدم لعملية مختلفة.")
                if old[2] == "completed":
                    result = response(json.loads(old[3]), old[4])
                    status_code = result.status
                    return result
                retryable = old[2] in {"retryable", "failed"}
                if old[2] == "running":
                    reclaimed = await query(
                        """UPDATE platform_requests SET status='retryable',updated_at=CURRENT_TIMESTAMP
                        WHERE user_id=? AND request_key=? AND status='running'
                        AND updated_at<CURRENT_TIMESTAMP-INTERVAL '15 minutes' RETURNING request_key""",
                        (user, idem), commit=True, fetchone=True,
                    )
                    retryable = bool(reclaimed)
                if not retryable:
                    raise web.HTTPConflict(text="الطلب نفسه قيد التنفيذ؛ أعد المحاولة لاحقًا.")
                claimed = await query(
                    """UPDATE platform_requests SET status='running',result=NULL,http_status=NULL,
                    updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND request_key=?
                    AND status IN ('retryable','failed') RETURNING request_key""",
                    (user, idem), commit=True, fetchone=True,
                )
                if not claimed:
                    raise web.HTTPConflict(text="الطلب نفسه قيد التنفيذ؛ أعد المحاولة لاحقًا.")
            claim = (user, idem)

        try:
            result = await platform_api.dispatch(request)
        except web.HTTPException as exc:
            result = response({"error": exc.text if exc.status < 500 else "الخدمة غير متاحة."}, exc.status)
        except PermissionError as exc:
            result = response({"error": str(exc)[:200]}, 403)
        except (ValueError, TypeError, KeyError, ArithmeticError, json.JSONDecodeError) as exc:
            result = response({"error": str(exc)[:200] or "راجع بيانات الطلب."}, 400)

        status_code = result.status
        return result
    except web.HTTPException as exc:
        status_code = exc.status
        raise
    finally:
        if claim:
            if isinstance(result, web.Response):
                await query(
                    """UPDATE platform_requests SET status=CASE WHEN ?<500 THEN 'completed' ELSE 'retryable' END,
                    result=?,http_status=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND request_key=?""",
                    (result.status, result.text, result.status, *claim), commit=True,
                )
            else:
                await query(
                    """UPDATE platform_requests SET status='retryable',updated_at=CURRENT_TIMESTAMP
                    WHERE user_id=? AND request_key=?""",
                    claim, commit=True,
                )
        await _log(key_id, user, request, status_code)


async def core_gateway(request):
    """Core commerce/account/automation endpoints for external clients."""
    from miniapp_server import response, body, CLIENT_KEY
    import commerce
    import storefront
    import account_controls
    import support_center
    import automation

    user, key_id = await authenticate(request)
    path = request.match_info["tail"].strip("/")
    status_code = 500
    try:
        if request.method == "GET":
            if path == "catalog":
                result = response(await storefront.catalog(user))
            elif path == "accounts":
                import account_access
                rows = await account_access.accounts(user, owned_only=True)
                result = response({"items": [{"phone": r[0], "owner_id": r[2], "source": r[4]} for r in rows]})
            elif path.startswith("account/") and path.endswith("/status"):
                phone = path.split("/")[1]
                result = response(await account_controls.account_status(user, phone))
            elif path == "rentals":
                result = response(await account_controls.rentals(user, int(request.query.get("page", "0"))))
            elif path == "activity":
                rows = await query(
                    "SELECT entry_id,created_at,operation,amount,balance_after FROM balance_ledger WHERE user_id=? ORDER BY entry_id DESC LIMIT 100",
                    (user,), fetchall=True,
                ) or []
                result = response({"items": [{"id": r[0], "date": r[1], "operation": r[2], "amount": r[3], "balance": r[4]} for r in rows]})
            elif path == "tickets":
                rows = await support_center.listing(user, int(request.query.get("page", "0")))
                result = response({"items": rows})
            elif re.fullmatch(r"ticket/\d+", path):
                ticket_id = int(path.split("/")[1])
                result = response(await support_center.detail(user, ticket_id, int(request.query.get("page", "0"))))
            elif path == "automation/rules":
                rows = await query(
                    "SELECT rule_id,phone,kind,payload,enabled,created_at FROM automation_rules WHERE user_id=? ORDER BY rule_id DESC",
                    (user,), fetchall=True,
                ) or []
                result = response({"items": [{"id": r[0], "phone": r[1], "kind": r[2], "payload": json.loads(r[3]), "enabled": r[4], "created_at": r[5]} for r in rows]})
            elif path == "automation/jobs":
                rows = await query(
                    "SELECT job_id,kind,payload,due_at,ends_at,timezone,interval_minutes,status,run_count FROM automation_jobs WHERE user_id=? ORDER BY job_id DESC",
                    (user,), fetchall=True,
                ) or []
                result = response({"items": [{"id": r[0], "kind": r[1], "payload": json.loads(r[2]), "due_at": r[3], "ends_at": r[4], "timezone": r[5], "interval_minutes": r[6], "status": r[7], "run_count": r[8]} for r in rows]})
            else:
                raise web.HTTPNotFound()
        elif request.method == "POST":
            data = await body(request)
            if path == "purchase/quote":
                result = response(await commerce.purchase_quote(user, str(data["phone"])))
            elif path == "purchase/confirm":
                result = response(await commerce.complete_purchase(user, str(data["quote_id"])))
            elif path == "rental/quote":
                result = response(await commerce.rental_quote(user, int(data["count"]), int(data["days"])))
            elif path == "rental/confirm":
                result = response(await commerce.complete_rental(user, str(data["quote_id"])))
            elif path == "renewal/quote":
                from platform_services import renewal_quote
                key, amount, end = await renewal_quote(user, int(data["lease_id"]), int(data["days"]))
                result = response({"quote_id": key, "amount": amount, "expected_end": end})
            elif path == "renewal/confirm":
                from platform_services import renew_lease
                ok, message = await renew_lease(user, str(data["quote_id"]))
                if not ok:
                    raise ValueError(message)
                result = response({"completed": True, "message": message})
            elif path == "coupon":
                result = response({"coupon": await commerce.select_coupon(user, data.get("code", "-"))})
            elif re.fullmatch(r"account/[^/]+/labels", path):
                phone = path.split("/")[1]
                await account_controls.save_labels(user, phone, data.get("label", ""), data.get("folder", ""), data.get("tags", ""))
                result = response({"ok": True})
            elif re.fullmatch(r"account/[^/]+/health", path):
                phone = path.split("/")[1]
                await account_controls.request_check(request.app[CLIENT_KEY], user, phone)
                result = response({"ok": True})
            elif path == "watch/toggle":
                result = response({"enabled": await storefront.toggle_watch(user, int(data["country_id"]), data["service"], data["field"])})
            elif path == "tickets":
                result = response({"id": await support_center.create(user, data["message"])}, 201)
            elif re.fullmatch(r"ticket/\d+/message", path):
                ticket_id = int(path.split("/")[1])
                result = response({"id": await support_center.reply(user, ticket_id, data["message"])}, 201)
            elif path == "automation/rule/save":
                ids = await automation.save_rule(user, [str(p) for p in data["phones"]], data["kind"], data["payload"], data.get("rule_id"))
                result = response({"ids": ids})
            elif path == "automation/rule/toggle":
                row = await query(
                    "UPDATE automation_rules SET enabled=NOT enabled WHERE rule_id=? AND user_id=? RETURNING enabled",
                    (int(data["rule_id"]), user), commit=True, fetchone=True,
                )
                if not row:
                    raise ValueError("قاعدة الأتمتة غير موجودة.")
                result = response({"enabled": row[0]})
            elif path == "automation/rule/delete":
                await query("DELETE FROM automation_rules WHERE rule_id=? AND user_id=?", (int(data["rule_id"]), user), commit=True)
                result = response({"ok": True})
            elif path == "automation/job/save":
                job = await automation.save_job(
                    user,
                    [str(p) for p in data["phones"]],
                    data["kind"], data["payload"], data["due"], data.get("timezone", "Asia/Baghdad"),
                    data.get("ends"), data.get("interval"), data.get("job_id"),
                )
                result = response({"job_id": job})
            elif path == "automation/job/start":
                job_id = int(data["job_id"])
                row = await query(
                    "UPDATE automation_jobs SET status='pending',cancel_requested=FALSE WHERE job_id=? AND user_id=? AND status='paused' RETURNING job_id",
                    (job_id, user), commit=True, fetchone=True,
                )
                if not row:
                    raise ValueError("يمكن تشغيل مهمة متوقفة فقط.")
                result = response({"ok": True})
            elif path == "automation/job/pause":
                await query(
                    "UPDATE automation_jobs SET cancel_requested=TRUE,status=CASE WHEN status='running' THEN status ELSE 'paused' END WHERE job_id=? AND user_id=?",
                    (int(data["job_id"]), user), commit=True,
                )
                result = response({"ok": True})
            elif path == "automation/job/delete":
                row = await query(
                    "DELETE FROM automation_jobs WHERE job_id=? AND user_id=? AND status<>'running' RETURNING job_id",
                    (int(data["job_id"]), user), commit=True, fetchone=True,
                )
                if not row:
                    raise ValueError("أوقف المهمة قبل حذفها.")
                result = response({"ok": True})
            else:
                raise web.HTTPNotFound()
        else:
            raise web.HTTPMethodNotAllowed(request.method, ("GET", "POST"))
        status_code = result.status
        return result
    except web.HTTPException as exc:
        status_code = exc.status
        raise
    except (ValueError, TypeError, KeyError, ArithmeticError, json.JSONDecodeError):
        status_code = 400
        raise
    finally:
        await _log(key_id, user, request, status_code)

