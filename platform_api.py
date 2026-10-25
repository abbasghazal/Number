"""Native platform views over the same business services used by Telegram."""

import asyncio
import hashlib
import json
import secrets

from aiohttp import web

import account_access
import promotion_service as promotions
import promotion_store as store
import funding_service
from config import DEVELOPER_ID
from utils.asyncio_compat import to_thread
from workflow import query

_streams = {}


def obj(row, keys):
    return dict(zip(keys, row))


async def funding_stats(user=None):
    where = " WHERE c.user_id=?" if user is not None else ""
    args = (user,) if user is not None else ()
    totals = await query(
        f"SELECT COUNT(*),COALESCE(SUM(c.quantity*c.cost),0),COALESCE(SUM(c.delivered*c.reward),0),COALESCE(SUM(c.refunded),0),COUNT(*) FILTER(WHERE c.status='active'),COUNT(*) FILTER(WHERE c.status='completed'),COUNT(*) FILTER(WHERE c.status IN ('pausing','cancelled')) FROM funding_campaigns c{where}",
        args,
        fetchone=True,
    )
    pending = await query(
        "SELECT COALESCE(SUM(c.reward),0) FROM funding_tasks t JOIN funding_campaigns c ON c.campaign_id=t.campaign_id WHERE t.status='pending'"
        + (" AND c.user_id=?" if user is not None else ""),
        args,
        fetchone=True,
    )
    margin = await query(
        "SELECT COALESCE(SUM(m.amount),0) FROM funding_margin m JOIN funding_campaigns c ON c.campaign_id=m.campaign_id"
        + where,
        args,
        fetchone=True,
    )
    return dict(
        campaigns=totals[0],
        funded=totals[1],
        rewards=totals[2],
        refunded=totals[3],
        active=totals[4],
        completed=totals[5],
        stopped=totals[6],
        pending_rewards=pending[0],
        margin=margin[0],
    )


async def dashboard(user):
    from helpers import get_user
    from account_health import records, session_key

    account = await get_user(user)
    owned = await account_access.accounts(user, owned_only=True)
    statuses = await records(session_key(a[1]) for a in owned)
    healthy = sum(statuses.get(session_key(a[1]), {}).get("status") == "healthy" for a in owned)
    points = await query("SELECT points FROM funding_wallets WHERE user_id=?", (user,), fetchone=True)
    orders = await query(
        "SELECT COUNT(*) FILTER(WHERE status='running'),COUNT(*) FILTER(WHERE status='review'),COUNT(*) FROM promotion_orders WHERE user_id=?",
        (user,),
        fetchone=True,
    )
    flows = await query(
        "SELECT COALESCE(SUM(amount) FILTER(WHERE amount>0),0),COALESCE(-SUM(amount) FILTER(WHERE amount<0),0) FROM balance_ledger WHERE user_id=? AND created_at>=CURRENT_TIMESTAMP-INTERVAL '30 days'",
        (user,),
        fetchone=True,
    )
    chart = (
        await query(
            "SELECT created_at::date,COALESCE(SUM(amount) FILTER(WHERE amount>0),0),COALESCE(-SUM(amount) FILTER(WHERE amount<0),0) FROM balance_ledger WHERE user_id=? AND created_at>=CURRENT_TIMESTAMP-INTERVAL '14 days' GROUP BY created_at::date ORDER BY created_at::date",
            (user,),
            fetchall=True,
        )
        or []
    )
    activity = (
        await query(
            "SELECT entry_id,operation,amount,balance_after,created_at FROM balance_ledger WHERE user_id=? ORDER BY entry_id DESC LIMIT 8",
            (user,),
            fetchall=True,
        )
        or []
    )
    notices = (
        await query(
            "SELECT notice_id,message,created_at,sent_at FROM operation_notices WHERE user_id=? ORDER BY notice_id DESC LIMIT 8",
            (user,),
            fetchall=True,
        )
        or []
    )
    changes = await query(
        """SELECT
        (SELECT MAX(i.updated_at) FROM promotion_items i JOIN promotion_orders o ON o.order_id=i.order_id WHERE o.user_id=?),
        (SELECT MAX(updated_at) FROM funding_campaigns WHERE user_id=?),
        (SELECT MAX(t.verified_at) FROM funding_tasks t JOIN funding_campaigns c ON c.campaign_id=t.campaign_id WHERE t.user_id=? OR c.user_id=?)""",
        (user, user, user, user),
        fetchone=True,
    )
    return dict(
        user_id=user,
        revision="|".join(str(value or "") for value in changes),
        balance=account["coins"],
        points=points[0] if points else 0,
        accounts=len(owned),
        healthy=healthy,
        orders=dict(running=orders[0], review=orders[1], total=orders[2]),
        funding=await funding_stats(user),
        income=flows[0],
        expense=flows[1],
        chart=[dict(date=r[0], income=r[1], expense=r[2]) for r in chart],
        activity=[obj(r, ("id", "operation", "amount", "balance", "date")) for r in activity],
        notifications=[obj(r, ("id", "message", "date", "sent_at")) for r in notices],
    )


async def campaigns(user, admin=False):
    rows = (
        await query(
            """SELECT c.campaign_id,c.user_id,c.title,c.target,c.target_id,c.quantity,c.delivered,c.reward,c.cost,c.status,
        c.guarantee_seconds,c.refunded,c.created_at,
        (SELECT COUNT(*) FROM funding_tasks t WHERE t.campaign_id=c.campaign_id AND t.status='pending'),
        (SELECT COUNT(*) FROM funding_tasks t WHERE t.campaign_id=c.campaign_id AND t.status='reserved' AND t.expires_at>CURRENT_TIMESTAMP)
        FROM funding_campaigns c"""
            + ("" if admin else " WHERE c.user_id=?")
            + " ORDER BY c.campaign_id DESC LIMIT 100",
            () if admin else (user,),
            fetchall=True,
        )
        or []
    )
    return [
        obj(
            r,
            (
                "id",
                "user_id",
                "title",
                "target",
                "target_id",
                "quantity",
                "delivered",
                "reward",
                "cost",
                "status",
                "guarantee_seconds",
                "refunded",
                "created_at",
                "pending",
                "reserved",
            ),
        )
        for r in rows
    ]


async def order_list(user, admin=False):
    rows = (
        await query(
            """SELECT o.order_id,o.user_id,o.kind,o.title,o.quantity,o.unit_price,o.charge,o.refunded,o.status,o.created_at,o.updated_at,o.cancel_requested,o.next_attempt,
        (SELECT COUNT(*) FROM promotion_items i WHERE i.order_id=o.order_id AND i.status='success'),
        (SELECT COUNT(*) FROM promotion_items i WHERE i.order_id=o.order_id AND i.status='failed'),
        (SELECT COUNT(*) FROM promotion_items i WHERE i.order_id=o.order_id AND i.status='unknown')
        FROM promotion_orders o"""
            + ("" if admin else " WHERE o.user_id=?")
            + " ORDER BY o.created_at DESC LIMIT 100",
            () if admin else (user,),
            fetchall=True,
        )
        or []
    )
    return [
        obj(
            r,
            (
                "id",
                "user_id",
                "kind",
                "title",
                "quantity",
                "unit_price",
                "charge",
                "refunded",
                "status",
                "created_at",
                "updated_at",
                "cancel_requested",
                "next_attempt",
                "success",
                "failed",
                "unknown",
            ),
        )
        for r in rows
    ]


async def config():
    row = await query(
        "SELECT reward,cost,enabled,minimum,maximum,guarantee_seconds FROM funding_config WHERE id=1", fetchone=True
    )
    return obj(row, ("reward", "cost", "enabled", "minimum", "maximum", "guarantee_seconds"))


async def draft(user, kind, payload):
    key = secrets.token_hex(16)
    await query(
        "INSERT INTO platform_drafts(draft_id,user_id,kind,payload) VALUES(?,?,?,?)",
        (key, user, kind, json.dumps(payload, ensure_ascii=False)),
        commit=True,
    )
    return key


async def dispatch(request):
    from miniapp_server import response, body, CLIENT_KEY

    user = request["user"]
    bot = request.app[CLIENT_KEY]
    path = request.match_info["tail"].strip("/")
    if (path == "admin" or path.startswith("admin/")) and (not DEVELOPER_ID or user != DEVELOPER_ID):
        raise web.HTTPForbidden(text="هذه العملية متاحة للمطور فقط.")
    if request.method not in ("GET", "POST"):
        raise web.HTTPMethodNotAllowed(request.method, ("GET", "POST"))
    if request.method == "GET" or path == "funding/task":
        if path == "dashboard":
            return response(await dashboard(user))
        if path == "features":
            from staff_security import is_staff
            from service_catalog import available
            services=await available(user)
            return response(dict(sections=[r['key'] for r in services],services=services,developer=user == DEVELOPER_ID, staff=await is_staff(user)))
        if path=='tasks':
            from task_cleanup import listing
            return response(dict(items=await listing(user)))
        if path == "preferences":
            row = await query(
                "SELECT theme,language,notifications,stealth FROM platform_preferences WHERE user_id=?",
                (user,),
                fetchone=True,
            )
            return response(obj(row or ("dark", "ar", True, False), ("theme", "language", "notifications", "stealth")))
        if path == "promotions":
            prices = (
                await query("SELECT kind,unit_price,enabled FROM promotion_prices ORDER BY kind", fetchall=True) or []
            )
            return response(
                dict(
                    prices=[obj(r, ("kind", "unit_price", "enabled")) for r in prices],
                    orders=await order_list(user),
                    available=len(await promotions.pool()),
                )
            )
        if path == "funding":
            wallet = await query("SELECT points FROM funding_wallets WHERE user_id=?", (user,), fetchone=True)
            ledger = (
                await query(
                    "SELECT entry_id,amount,balance_after,operation,created_at FROM funding_ledger WHERE user_id=? ORDER BY entry_id DESC LIMIT 50",
                    (user,),
                    fetchall=True,
                )
                or []
            )
            tasks = (
                await query(
                    "SELECT t.task_id,t.status,t.check_due,t.expires_at,c.title,c.reward,c.guarantee_seconds FROM funding_tasks t JOIN funding_campaigns c ON c.campaign_id=t.campaign_id WHERE t.user_id=? ORDER BY COALESCE(t.verified_at,t.expires_at) DESC LIMIT 30",
                    (user,),
                    fetchall=True,
                )
                or []
            )
            watches=await query('SELECT watch_id,title,reward,penalty_amount,status,granted_at,last_checked,penalized_at FROM funding_membership_watch WHERE user_id=? ORDER BY watch_id DESC LIMIT 50',(user,),fetchall=True) or []
            return response(
                dict(
                    points=wallet[0] if wallet else 0,
                    watches=[obj(r,('id','title','reward','penalty_amount','status','granted_at','last_checked','penalized_at')) for r in watches],
                    config=await config(),
                    campaigns=await campaigns(user),
                    stats=await funding_stats(user),
                    ledger=[obj(r, ("id", "amount", "balance", "operation", "date")) for r in ledger],
                    tasks=[
                        obj(r, ("id", "status", "check_due", "expires_at", "title", "reward", "guarantee_seconds"))
                        for r in tasks
                    ],
                )
            )
        if path == "funding/task":
            if not (await config())["enabled"]:
                raise ValueError("خدمة جمع النقاط متوقفة حاليًا.")
            existing = await query(
                "SELECT t.task_id,c.title,c.target,c.reward,c.guarantee_seconds,t.expires_at,t.status,t.check_due,t.penalty_multiplier FROM funding_tasks t JOIN funding_campaigns c ON c.campaign_id=t.campaign_id WHERE t.user_id=? AND ((t.status='reserved' AND t.expires_at>CURRENT_TIMESTAMP) OR t.status='pending') ORDER BY t.expires_at DESC LIMIT 1",
                (user,),
                fetchone=True,
            )
            if existing:
                return response(
                    obj(
                        existing,
                        ("id", "title", "target", "reward", "guarantee_seconds", "expires_at", "status", "check_due","penalty_multiplier"),
                    )
                )
            if request.method == "GET":
                return response(None)
            candidates = (
                await query(
                    "SELECT c.campaign_id,c.target_id FROM funding_campaigns c WHERE c.status='active' AND c.user_id<>? AND NOT EXISTS(SELECT 1 FROM funding_memberships m WHERE m.target_id=c.target_id AND m.user_id=?) ORDER BY c.campaign_id LIMIT 30",
                    (user, user),
                    fetchall=True,
                )
                or []
            )
            for key, target in candidates:
                if await promotions.is_member(bot, target, user):
                    continue
                try:
                    task = await to_thread(store.reserve_task, user, key)
                except ValueError:
                    continue
                row = await query(
                    "SELECT t.task_id,c.title,c.target,c.reward,c.guarantee_seconds,t.expires_at,t.status,t.check_due,t.penalty_multiplier FROM funding_tasks t JOIN funding_campaigns c ON c.campaign_id=t.campaign_id WHERE t.task_id=?",
                    (task,),
                    fetchone=True,
                )
                return response(
                    obj(
                        row,
                        ("id", "title", "target", "reward", "guarantee_seconds", "expires_at", "status", "check_due","penalty_multiplier"),
                    )
                )
            return response(None)
        if path == "console":
            import web_console

            transport = "api" if request.get("external_api") else "web"
            return response(web_console.get(user, bot, transport).snapshot())
        if path == "admin":
            store.developer(user)
            balances = await query("SELECT COUNT(*),COALESCE(SUM(coins),0) FROM users", fetchone=True)
            roles = await query("SELECT user_id,role FROM staff_roles ORDER BY user_id LIMIT 100", fetchall=True) or []
            audits = (
                await query(
                    "SELECT user_id,action,created_at FROM audit_log ORDER BY created_at DESC LIMIT 50", fetchall=True
                )
                or []
            )
            users = (
                await query(
                    "SELECT u.user_id,u.first_name,u.coins,EXISTS(SELECT 1 FROM bad_guys b WHERE b.user_id=u.user_id) FROM users u ORDER BY join_date DESC LIMIT 50",
                    fetchall=True,
                )
                or []
            )
            settings = await query(
                "SELECT enabled,max_orders,members_limit,reactions_limit,search_limit,delay_seconds,error_limit,cooldown_seconds,attempt_multiplier FROM promotion_execution_config WHERE id=1",
                fetchone=True,
            )
            failures = (
                await query(
                    "SELECT task_id,user_id,phase,result,created_at FROM funding_verifications ORDER BY id DESC LIMIT 50",
                    fetchall=True,
                )
                or []
            )
            return response(
                dict(
                    users_total=balances[0],
                    wallet_total=balances[1],
                    users=[obj(r, ("id", "name", "balance", "banned")) for r in users],
                    roles=[obj(r, ("id", "role")) for r in roles],
                    funding_config=await config(),
                    funding_stats=await funding_stats(),
                    campaigns=await campaigns(user, True),
                    orders=await order_list(user, True),
                    audits=[obj(r, ("user", "action", "date")) for r in audits],
                    verifications=[obj(r, ("task", "user", "phase", "result", "date")) for r in failures],
                    execution=obj(
                        settings,
                        (
                            "enabled",
                            "max_orders",
                            "members_limit",
                            "reactions_limit",
                            "search_limit",
                            "delay_seconds",
                            "error_limit",
                            "cooldown_seconds",
                            "attempt_multiplier",
                        ),
                    ),
                )
            )
        raise web.HTTPNotFound()
    data = await body(request)
    if path=='tasks/cleanup/quote':
        from task_cleanup import preview
        return response(await to_thread(preview,user,str(data.get('scope','mine'))))
    if path=='tasks/cleanup/confirm':
        from task_cleanup import confirm
        return response(await to_thread(confirm,user,str(data['quote_id'])))
    if path == "preferences":
        theme, language = data.get("theme", "dark"), data.get("language", "ar")
        if theme not in ("dark", "light") or language not in ("ar", "en"):
            raise ValueError("إعداد واجهة غير صالح.")
        await query(
            "INSERT INTO platform_preferences(user_id,theme,language) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET theme=excluded.theme,language=excluded.language",
            (user, theme, language),
            commit=True,
        )
        return response({"ok": True})
    if path == "promotions/quote":
        kind = data["kind"]
        if (
            kind not in ("members", "reactions", "search")
            or kind == "reactions"
            and data.get("emoji") not in promotions.EMOJIS
        ):
            raise ValueError("الخدمة أو التفاعل غير صالح.")
        target = await promotions.validate_paid_target(bot,user,str(data['target']),kind,invite=data.get('invite') or None)
        key, amount = await to_thread(
            store.quote,
            user,
            kind,
            target,
            int(data["quantity"]),
            str(data.get("emoji", "")),
            str(data.get("search_phrase", "")),
        )
        return response(dict(quote_id=key, amount=amount, quantity=int(data["quantity"]), title=target["title"]))
    if path == "promotions/confirm":
        row = await query(
            "SELECT target_id,target,message_id,kind FROM promotion_orders WHERE order_id=? AND user_id=?",
            (data["quote_id"], user),
            fetchone=True,
        )
        if not row:
            raise ValueError("الطلب غير موجود ضمن حسابك.")
        await promotions.revalidate_paid_target(bot,user,row[3],dict(id=row[0],link=row[1],message_id=row[2]))
        result = await to_thread(store.accept, user, str(data["quote_id"]), await promotions.pool())
        await promotions.dispatch()
        return response(dict(status=result))
    if path == "promotions/cancel":
        await to_thread(store.cancel_order, user, str(data["id"]))
        return response({"ok": True})
    if path == "funding/quote":
        target = await promotions.validate_target(bot, user, str(data["target"]), "members")
        cfg = await config()
        qty = int(data["quantity"])
        if not cfg["enabled"] or not cfg["minimum"] <= qty <= cfg["maximum"]:
            raise ValueError("العدد خارج حدود التمويل أو الخدمة متوقفة.")
        payload = dict(
            target=target,
            quantity=qty,
            cost=cfg["cost"],
            reward=cfg["reward"],
            guarantee_seconds=cfg["guarantee_seconds"],
        )
        key = await draft(user, "funding", payload)
        return response(
            dict(
                quote_id=key,
                title=target["title"],
                quantity=qty,
                amount=qty * cfg["cost"],
                reward=cfg["reward"],
                guarantee_seconds=cfg["guarantee_seconds"],
            )
        )
    if path == "funding/confirm":
        key = str(data["quote_id"])
        row = await query(
            "SELECT payload FROM platform_drafts WHERE draft_id=? AND user_id=? AND kind='funding' AND expires_at>CURRENT_TIMESTAMP",
            (key, user),
            fetchone=True,
        )
        if not row:
            raise ValueError("انتهى عرض التمويل؛ اطلب معاينة جديدة.")
        payload = json.loads(row[0])
        await promotions.validate_target(bot, user, str(payload["target"]["id"]), "members")
        campaign = await to_thread(
            store.campaign,
            user,
            payload["target"],
            payload["quantity"],
            payload["cost"],
            payload["reward"],
            request_key=key,
            expected_guarantee=payload["guarantee_seconds"],
        )
        return response({"id": campaign})
    if path == "funding/verify":
        return response(await funding_service.verify(bot, user, str(data["task_id"])))
    if path == "funding/cancel":
        return response(dict(refunded=await to_thread(store.cancel_campaign, user, int(data["id"]))))
    if path == "funding/increase":
        return response(
            dict(
                increased=await to_thread(
                    store.increase_campaign,
                    user,
                    int(data["id"]),
                    int(data["quantity"]),
                    request.headers["X-Idempotency-Key"],
                )
            )
        )
    if path.startswith("console/"):
        import web_console

        method = path.split("/")[1]
        transport = "api" if request.get("external_api") else "web"
        if method == "start":
            return response(await web_console.start(user, bot, str(data["section"]), transport))
        if method == "click":
            return response(await web_console.click(user, bot, str(data["token"]), transport))
        if method == "answer":
            return response(await web_console.answer(user, bot, str(data["prompt"]), str(data["text"]), transport))
        raise web.HTTPNotFound()
    if path.startswith("admin/"):
        store.developer(user)
        method = path.split("/")[1]
        if method == "funding":
            await to_thread(
                store.configure_points,
                user,
                int(data["reward"]),
                int(data["cost"]),
                bool(data["enabled"]),
                minimum=int(data["minimum"]),
                maximum=int(data["maximum"]),
                guarantee_seconds=int(data.get("guarantee_seconds",0)),
            )
        elif method == "price":
            await to_thread(store.price, user, data["kind"], data["unit_price"], bool(data["enabled"]))
        elif method == "grant":
            await to_thread(store.grant_points, user, int(data["user_id"]), int(data["amount"]))
        elif method == "execution":
            await to_thread(store.configure_execution, user, data)
        elif method == "refund":
            await to_thread(store.release_unknown, user, str(data["id"]))
        else:
            raise web.HTTPNotFound()
        await query(
            "INSERT INTO audit_log(user_id,action) VALUES(?,?)", (user, "platform_admin:" + method), commit=True
        )
        return response({"ok": True})
    raise web.HTTPNotFound()


async def events(request):
    user = request["user"]
    if _streams.get(user, 0) >= 2:
        raise web.HTTPTooManyRequests(text="يوجد اتصالان مباشران لهذا الحساب؛ أعد المحاولة لاحقًا.")
    _streams[user] = _streams.get(user, 0) + 1
    try:
        return await _events(request)
    finally:
        _streams[user] -= 1
        if not _streams[user]:
            _streams.pop(user, None)


async def _events(request):
    from miniapp_server import check_identity, serialize

    stream = web.StreamResponse(
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )
    await stream.prepare(request)
    previous = None
    try:
        for index in range(120):
            if index % 6 == 0:
                import web_auth

                identity, _, _ = await web_auth.authenticate(request)
                if identity != request['user']:
                    break
                await check_identity(identity)
            data = await dashboard(request["user"])
            raw = json.dumps(data, default=serialize, ensure_ascii=False)
            revision = hashlib.sha256(raw.encode()).hexdigest()[:20]
            if revision != previous:
                await stream.write(f"id: {revision}\nevent: update\ndata: {raw}\n\n".encode())
                previous = revision
            else:
                await stream.write(b": heartbeat\n\n")
            await asyncio.sleep(10)
    except (ConnectionResetError, asyncio.CancelledError, web.HTTPException, ValueError):
        pass
    return stream
