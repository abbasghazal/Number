"""Transactional wallet holds and verified point-funding rewards."""

import secrets
from decimal import Decimal, ROUND_UP

from config import DEVELOPER_ID
from postgres import connect
from workflow import enqueue


def developer(actor):
    if not DEVELOPER_ID or actor != DEVELOPER_ID:
        raise ValueError("هذه العملية متاحة للمطور فقط.")


def money(rate, count):
    return (Decimal(str(rate)) * int(count)).quantize(Decimal(".01"), rounding=ROUND_UP)


def price(actor, kind, rate, enabled):
    developer(actor)
    try:
        rate = Decimal(str(rate))
    except ArithmeticError:
        raise ValueError("أدخل سعرًا رقميًا صالحًا.") from None
    if kind not in {"members", "reactions", "search"} or not rate.is_finite() or not Decimal(".000001") <= rate <= 100:
        raise ValueError("سعر الوحدة يجب أن يكون من 0.000001 إلى 100 دولار.")
    with connect() as db:
        db.execute(
            "UPDATE promotion_prices SET unit_price=?,enabled=?,version=version+1 WHERE kind=?",
            (rate, bool(enabled), kind),
        )


def quote(user, kind, target, count, emoji="", search_phrase=""):
    if kind not in {"members", "reactions", "search"} or not 1 <= count <= 1000:
        raise ValueError("اختر عددًا من 1 إلى 1000.")
    if kind == "search" and not 2 <= len(search_phrase.strip()) <= 200:
        raise ValueError("عبارة البحث يجب أن تكون بين حرفين و200 حرف.")
    with connect() as db:
        row = db.execute("SELECT unit_price,version,enabled FROM promotion_prices WHERE kind=?", (kind,)).fetchone()
        if not row or not row[2]:
            raise ValueError("الخدمة متوقفة حاليًا؛ يجب على المطور تفعيل سعرها.")
        key = secrets.token_hex(12)
        db.execute(
            """INSERT INTO promotion_orders(order_id,user_id,kind,target_id,target,title,message_id,emoji,quantity,unit_price,price_version,search_phrase)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                user,
                kind,
                target["id"],
                target["link"],
                target["title"],
                target.get("message_id", 0),
                emoji,
                count,
                row[0],
                row[1],
                search_phrase.strip(),
            ),
        )
        return key, money(row[0], count)


def accept(user, key, accounts):
    with connect() as db:
        row = db.execute(
            "SELECT kind,quantity,unit_price,price_version,status,expires_at>CURRENT_TIMESTAMP,charge FROM promotion_orders WHERE order_id=? AND user_id=? FOR UPDATE",
            (key, user),
        ).fetchone()
        if not row:
            raise ValueError("الطلب غير موجود ضمن حسابك.")
        kind, count, rate, version, status, fresh, charge = row
        if status != "quote":
            return status
        current = db.execute("SELECT version,enabled FROM promotion_prices WHERE kind=?", (kind,)).fetchone()
        if not fresh or current != (version, True):
            raise ValueError("انتهى عرض السعر أو تغيرت إعدادات الخدمة؛ أنشئ عرضًا جديدًا.")
        # Pool entries contain consent and a fingerprint, never a plain session.
        eligible = []
        for phone, owner, fingerprint in accounts:
            if db.execute(
                "SELECT 1 FROM promotion_pool WHERE phone=? AND owner_id=? AND session_key=? AND enabled FOR SHARE",
                (phone, owner, fingerprint),
            ).fetchone():
                eligible.append((phone, owner, fingerprint))
        if len({a[0] for a in eligible}) < count:
            raise ValueError("الحسابات المتاحة أقل من العدد المطلوب؛ لم يُخصم الرصيد.")
        charge = money(rate, count)
        user_balance = db.execute("SELECT referral_balance FROM users WHERE user_id=? FOR UPDATE", (user,)).fetchone()
        referral_charge = min(charge, Decimal(str(user_balance[0] or 0))) if user_balance else Decimal(0)
        balance = db.execute(
            "UPDATE users SET coins=coins-?,referral_balance=GREATEST(0,referral_balance-?) WHERE user_id=? AND coins>=? RETURNING coins",
            (charge, charge, user, charge),
        ).fetchone()
        if not balance:
            raise ValueError("رصيدك غير كافٍ؛ لم يبدأ الطلب.")
        db.execute(
            "INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,fee) VALUES(?,?,?,?,?,0)",
            (user, -charge, balance[0], "promotion_purchase", key),
        )
        for phone, owner, fingerprint in eligible[:count]:
            db.execute(
                "INSERT INTO promotion_items(order_id,phone,owner_id,session_key) VALUES(?,?,?,?)",
                (key, phone, owner, fingerprint),
            )
        db.execute(
            "UPDATE promotion_orders SET status='running',charge=?,referral_charge=?,updated_at=CURRENT_TIMESTAMP WHERE order_id=?",
            (charge, referral_charge, key),
        )
        return "running"


def reserve_item(key, phone):
    with connect() as db:
        order = db.execute(
            "SELECT kind,target_id,message_id,cancel_requested FROM promotion_orders WHERE order_id=? AND status='running' FOR UPDATE",
            (key,),
        ).fetchone()
        if not order:
            return False
        row = db.execute(
            "UPDATE promotion_items SET status='checking',updated_at=CURRENT_TIMESTAMP WHERE order_id=? AND phone=? AND status='pending' RETURNING phone",
            (key, phone),
        ).fetchone()
        if not row:
            return False
        kind, target, msg, cancelled = order
        slot = (
            None
            if cancelled
            else db.execute(
                """INSERT INTO promotion_slots(kind,target_id,message_id,phone,order_id) VALUES(?,?,?,?,?)
            ON CONFLICT(kind,target_id,message_id,phone) DO UPDATE SET order_id=promotion_slots.order_id
            WHERE promotion_slots.order_id=excluded.order_id RETURNING phone""",
                (kind, target, msg, phone, key),
            ).fetchone()
        )
        if not slot:
            db.execute(
                "UPDATE promotion_items SET status='failed',result=? WHERE order_id=? AND phone=?",
                ("cancelled" if cancelled else "duplicate_target", key, phone),
            )
            db.execute("DELETE FROM promotion_slots WHERE order_id=? AND phone=?", (key, phone))
            return False
        return True


def finish_item(key, phone, status, result):
    if status not in {"success", "failed", "unknown"}:
        raise ValueError("invalid result")
    with connect() as db:
        changed = db.execute(
            "UPDATE promotion_items SET status=?,result=?,updated_at=CURRENT_TIMESTAMP WHERE order_id=? AND phone=? AND status NOT IN ('success','failed') RETURNING phone",
            (status, result, key, phone),
        ).fetchone()
        if changed and status == "failed":
            db.execute("DELETE FROM promotion_slots WHERE order_id=? AND phone=?", (key, phone))


def release_unknown(actor, key):
    developer(actor)
    with connect() as db:
        if not db.execute(
            "SELECT 1 FROM promotion_orders WHERE order_id=? AND status='review' FOR UPDATE", (key,)
        ).fetchone():
            raise ValueError("الطلب غير متاح للمراجعة.")
        phones = db.execute(
            "UPDATE promotion_items SET status='failed',result='developer_refund' WHERE order_id=? AND status='unknown' RETURNING phone",
            (key,),
        ).fetchall()
        for (phone,) in phones:
            db.execute("DELETE FROM promotion_slots WHERE order_id=? AND phone=?", (key, phone))
        db.execute("INSERT INTO audit_log(user_id,action) VALUES(?,?)", (actor, f"promotion_unknown_refund:{key}"))
    settle(key)


def settle(key):
    with connect() as db:
        row = db.execute(
            "SELECT user_id,quantity,unit_price,charge,refunded,referral_charge,status,cancel_requested FROM promotion_orders WHERE order_id=? FOR UPDATE",
            (key,),
        ).fetchone()
        if not row or row[6] not in {"running", "review"}:
            return
        user, quantity, rate, charge, refunded, referral_charge, _, cancelled = row
        counts = dict(
            db.execute(
                "SELECT status,COUNT(*) FROM promotion_items WHERE order_id=? GROUP BY status", (key,)
            ).fetchall()
        )
        if any(counts.get(s, 0) for s in ("pending", "checking", "executing")):
            return
        success, unknown = counts.get("success", 0), counts.get("unknown", 0)
        # An ambiguous network result retains only its own part of the hold.
        refund = max(Decimal(0), charge - money(rate, success + unknown) - refunded)
        if refund:
            referral_refund = min(refund, max(Decimal(0), Decimal(str(referral_charge)) - Decimal(str(refunded))))
            balance = db.execute(
                "UPDATE users SET coins=coins+?,referral_balance=referral_balance+? WHERE user_id=? RETURNING coins",
                (refund, referral_refund, user)
            ).fetchone()
            db.execute(
                "INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,fee) VALUES(?,?,?,?,?,0)",
                (user, refund, balance[0], "promotion_refund", key),
            )
        status = (
            "review"
            if unknown
            else "completed"
            if success == quantity
            else "cancelled"
            if cancelled and not success
            else "partial"
        )
        db.execute(
            "UPDATE promotion_orders SET status=?,refunded=refunded+?,updated_at=CURRENT_TIMESTAMP WHERE order_id=?",
            (status, refund, key),
        )
        enqueue(
            db,
            user,
            f"promotion:{key}:{success}:{unknown}:{status}",
            f"طلب الرشق {key}\nالمنجز: {success}/{quantity} · غير المحسوم: {unknown}\nالخصم الحالي: {charge - refunded - refund}$ · المسترد: {refunded + refund}$\nيمكنك متابعة الطلب من قسم الرشق.",
        )


def point_change(db, user, amount, operation, reference, *, allow_negative=False):
    db.execute("INSERT INTO funding_wallets(user_id) VALUES(?) ON CONFLICT DO NOTHING", (user,))
    condition='' if allow_negative or amount>=0 else ' AND points>=?'
    params=(amount,user) if not condition else (amount,user,-amount)
    balance = db.execute('UPDATE funding_wallets SET points=points+? WHERE user_id=?'+condition+' RETURNING points',params).fetchone()
    if balance is None:
        raise ValueError("نقاطك غير كافية.")
    db.execute(
        "INSERT INTO funding_ledger(user_id,amount,balance_after,operation,reference) VALUES(?,?,?,?,?)",
        (user, amount, balance[0], operation, str(reference)),
    )
    return balance[0]


def grant_points(actor, user, amount):
    developer(actor)
    if not 1 <= amount <= 10000000:
        raise ValueError("أدخل عدد نقاط من 1 إلى 10000000.")
    with connect() as db:
        if not db.execute("SELECT 1 FROM users WHERE user_id=?", (user,)).fetchone():
            raise ValueError("المستخدم غير مسجل في البوت.")
        balance = point_change(db, user, amount, "developer_grant", secrets.token_hex(12))
        db.execute("INSERT INTO audit_log(user_id,action) VALUES(?,?)", (actor, f"funding_grant:{user}:{amount}"))
        return balance


def configure_points(actor, reward, cost, enabled, *, minimum=1, maximum=100000, guarantee_seconds=0):
    developer(actor)
    if (
        not 1 <= reward <= cost <= 1000000
        or not 1 <= minimum <= maximum <= 100000
        or guarantee_seconds != 0
    ):
        raise ValueError("تحقق من المكافأة والتكلفة والحدود. مكافأة الاشتراك فورية دون مدة انتظار.")
    with connect() as db:
        db.execute(
            "UPDATE funding_config SET reward=?,cost=?,enabled=?,minimum=?,maximum=?,guarantee_seconds=? WHERE id=1",
            (reward, cost, bool(enabled), minimum, maximum, guarantee_seconds),
        )
        db.execute("INSERT INTO audit_log(user_id,action) VALUES(?,?)", (actor, "funding_configuration_updated"))


def configure_execution(actor, data):
    developer(actor)
    names = (
        "max_orders",
        "members_limit",
        "reactions_limit",
        "search_limit",
        "delay_seconds",
        "error_limit",
        "cooldown_seconds",
        "attempt_multiplier",
    )
    values = [int(data[name]) for name in names]
    bounds = ((1, 20), (1, 10), (1, 10), (1, 10), (1, 60), (1, 100), (30, 86400), (1, 10))
    if any(not lo <= v <= hi for v, (lo, hi) in zip(values, bounds)):
        raise ValueError("إعدادات التنفيذ خارج النطاق المسموح.")
    with connect() as db:
        db.execute(
            "UPDATE promotion_execution_config SET enabled=?,max_orders=?,members_limit=?,reactions_limit=?,search_limit=?,delay_seconds=?,error_limit=?,cooldown_seconds=?,attempt_multiplier=? WHERE id=1",
            (bool(data["enabled"]), *values),
        )
        db.execute("INSERT INTO audit_log(user_id,action) VALUES(?,?)", (actor, "promotion_execution_updated"))


def campaign(user, target, quantity, expected_cost, expected_reward, *, request_key=None, expected_guarantee=None):
    if not 1 <= quantity <= 100000:
        raise ValueError("اختر عددًا صحيحًا بين 1 و100000.")
    with connect() as db:
        if request_key:
            db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"campaign-request:{user}:{request_key}",))
            existing = db.execute(
                "SELECT campaign_id FROM funding_campaigns WHERE user_id=? AND request_key=?", (user, request_key)
            ).fetchone()
            if existing:
                return existing[0]
        reward, cost, enabled, minimum, maximum, guarantee = db.execute(
            "SELECT reward,cost,enabled,minimum,maximum,guarantee_seconds FROM funding_config WHERE id=1 FOR SHARE"
        ).fetchone()
        if (
            not enabled
            or (cost, reward) != (expected_cost, expected_reward)
            or not minimum <= quantity <= maximum
            or expected_guarantee is not None
            and expected_guarantee != guarantee
        ):
            raise ValueError("تغيرت إعدادات التمويل؛ اطلب معاينة جديدة قبل التأكيد.")
        db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"funding:{target['id']}",))
        if db.execute(
            "SELECT 1 FROM funding_campaigns WHERE target_id=? AND status IN ('active','pausing')", (target["id"],)
        ).fetchone():
            raise ValueError("توجد حملة نشطة أو مهام معلقة لهذا الهدف.")
        key = db.execute(
            "INSERT INTO funding_campaigns(user_id,target_id,target,title,quantity,reward,cost,guarantee_seconds,request_key) VALUES(?,?,?,?,?,?,?,?,?) RETURNING campaign_id",
            (user, target["id"], target["link"], target["title"], quantity, reward, cost, guarantee, request_key),
        ).fetchone()[0]
        point_change(db, user, -cost * quantity, "campaign_purchase", key)
        return key


def _expire_tasks(db, campaign_id):
    db.execute(
        "UPDATE funding_tasks SET status='expired',last_result='deadline' WHERE campaign_id=? AND status='reserved' AND expires_at<=CURRENT_TIMESTAMP",
        (campaign_id,),
    )


def reserve_task(user, campaign_id):
    with connect() as db:
        row = db.execute(
            "SELECT user_id,target_id,quantity,delivered,status FROM funding_campaigns WHERE campaign_id=? FOR UPDATE",
            (campaign_id,),
        ).fetchone()
        if not row or row[0] == user or row[4] != "active":
            raise ValueError("المهمة غير متاحة.")
        if db.execute("SELECT 1 FROM funding_memberships WHERE target_id=? AND user_id=?", (row[1], user)).fetchone():
            raise ValueError("حُسبت مكافأة لهذا الهدف مسبقًا.")
        _expire_tasks(db, campaign_id)
        existing = db.execute(
            "SELECT task_id FROM funding_tasks WHERE campaign_id=? AND user_id=? AND status IN ('reserved','pending')",
            (campaign_id, user),
        ).fetchone()
        if existing:
            return existing[0]
        reserved = db.execute(
            "SELECT COUNT(*) FROM funding_tasks WHERE campaign_id=? AND status IN ('reserved','pending')",
            (campaign_id,),
        ).fetchone()[0]
        if row[3] + reserved >= row[2]:
            raise ValueError("كل أماكن الحملة محجوزة؛ حاول لاحقًا.")
        key = secrets.token_hex(12)
        db.execute(
            """INSERT INTO funding_tasks(task_id,campaign_id,user_id,penalty_multiplier) VALUES(?,?,?,2) ON CONFLICT(campaign_id,user_id)
            DO UPDATE SET task_id=excluded.task_id,status='reserved',expires_at=excluded.expires_at,check_due=NULL,verified_at=NULL,last_result=NULL,lease_until=NULL,penalty_multiplier=2""",
            (key, campaign_id, user),
        )
        return key


def _complete_reward(db, user, task, campaign_id, row):
    target, reward, quantity, delivered, status, cost, guarantee = row
    if status not in ("active", "pausing") or delivered >= quantity:
        raise ValueError("الحملة غير قابلة للدفع حاليًا.")
    inserted = db.execute(
        "INSERT INTO funding_memberships(target_id,user_id) VALUES(?,?) ON CONFLICT DO NOTHING RETURNING user_id",
        (target, user),
    ).fetchone()
    if not inserted:
        raise ValueError("حُسبت مكافأة هذا الهدف مسبقًا.")
    point_change(db, user, reward, "task_reward", task)
    db.execute('''INSERT INTO funding_membership_watch(task_id,campaign_id,target_id,user_id,title,reward,penalty_amount)
        SELECT t.task_id,c.campaign_id,c.target_id,t.user_id,c.title,c.reward,c.reward*t.penalty_multiplier
        FROM funding_tasks t JOIN funding_campaigns c ON c.campaign_id=t.campaign_id
        WHERE t.task_id=? AND t.penalty_multiplier=2 ON CONFLICT DO NOTHING''',(task,))
    db.execute(
        "UPDATE funding_tasks SET status='completed',verified_at=CURRENT_TIMESTAMP,last_result='confirmed',lease_until=NULL WHERE task_id=?",
        (task,),
    )
    db.execute(
        "INSERT INTO funding_margin(task_id,campaign_id,amount) VALUES(?,?,?) ON CONFLICT DO NOTHING",
        (task, campaign_id, cost - reward),
    )
    db.execute(
        "UPDATE funding_campaigns SET delivered=delivered+1,status=CASE WHEN delivered+1=quantity THEN 'completed' ELSE status END,updated_at=CURRENT_TIMESTAMP WHERE campaign_id=?",
        (campaign_id,),
    )
    enqueue(db, user, f"funding-reward:{task}", f"اكتمل التحقق من الاشتراك وأضيفت {reward} نقطة إلى رصيدك.")
    return reward


def reward_task(user, task):
    with connect() as db:
        campaign_id = db.execute(
            "SELECT campaign_id FROM funding_tasks WHERE task_id=? AND user_id=?", (task, user)
        ).fetchone()
        if not campaign_id:
            raise ValueError("المهمة ليست ضمن حسابك.")
        row = db.execute(
            "SELECT target_id,reward,quantity,delivered,status,cost,guarantee_seconds FROM funding_campaigns WHERE campaign_id=? FOR UPDATE",
            campaign_id,
        ).fetchone()
        state = db.execute(
            "SELECT status,expires_at>CURRENT_TIMESTAMP FROM funding_tasks WHERE task_id=? AND user_id=? FOR UPDATE",
            (task, user),
        ).fetchone()
        if not state:
            raise ValueError("المهمة ليست ضمن حسابك.")
        if state[0] == "completed":
            return 0
        if (state != ("reserved", True) and state[0]!='pending') or row[4] not in ("active", "pausing"):
            raise ValueError("انتهت مهلة التحقق؛ اطلب مهمة جديدة.")
        db.execute(
            "INSERT INTO funding_verifications(task_id,user_id,phase,result) VALUES(?,?,'initial','member')",
            (task, user),
        )
        return _complete_reward(db, user, task, campaign_id[0], row)


def final_reward(user, task, member):
    with connect() as db:
        key = db.execute("SELECT campaign_id FROM funding_tasks WHERE task_id=? AND user_id=?", (task, user)).fetchone()
        if not key:
            return 0
        row = db.execute(
            "SELECT target_id,reward,quantity,delivered,status,cost,guarantee_seconds FROM funding_campaigns WHERE campaign_id=? FOR UPDATE",
            key,
        ).fetchone()
        state = db.execute(
            "SELECT status,check_due<=CURRENT_TIMESTAMP FROM funding_tasks WHERE task_id=? FOR UPDATE", (task,)
        ).fetchone()
        if not state or state[0] != 'pending':
            return 0
        db.execute(
            "INSERT INTO funding_verifications(task_id,user_id,phase,result) VALUES(?,?,'retention',?)",
            (task, user, "member" if member else "left"),
        )
        if member:
            return _complete_reward(db, user, task, key[0], row)
        db.execute(
            "UPDATE funding_tasks SET status='failed',last_result='left_before_guarantee',lease_until=NULL WHERE task_id=?",
            (task,),
        )
        return 0


def penalize_departure(user,watch_id,*,observed_at=None):
    with connect() as db:
        row=db.execute('SELECT reward,penalty_amount,status,title,granted_at FROM funding_membership_watch WHERE watch_id=? AND user_id=? FOR UPDATE',(watch_id,user)).fetchone()
        if not row or row[2]!='active' or observed_at is not None and observed_at<row[4]:
            return 0
        point_change(db,user,-row[1],'membership_penalty',watch_id,allow_negative=True)
        db.execute("UPDATE funding_membership_watch SET status='penalized',penalized_at=CURRENT_TIMESTAMP,last_checked=CURRENT_TIMESTAMP,last_result='left',lease_until=NULL WHERE watch_id=?",(watch_id,))
        db.execute("INSERT INTO funding_verifications(task_id,user_id,phase,result) SELECT task_id,user_id,'departure','left_penalized' FROM funding_membership_watch WHERE watch_id=?",(watch_id,))
        enqueue(db,user,f'membership-penalty:{watch_id}',f'رُصدت مغادرتك {row[3]}. خُصمت {row[1]} نقطة، وهي ضعف مكافأة الاشتراك ({row[0]}). يمكنك مراجعة سجل النقاط.')
        return row[1]


def settle_campaign(key):
    with connect() as db:
        row = db.execute(
            "SELECT user_id,quantity,delivered,cost,status,refunded FROM funding_campaigns WHERE campaign_id=? FOR UPDATE",
            (key,),
        ).fetchone()
        if not row or row[4] != "pausing":
            return 0
        _expire_tasks(db, key)
        outstanding = db.execute(
            "SELECT COUNT(*) FROM funding_tasks WHERE campaign_id=? AND status IN ('reserved','pending')", (key,)
        ).fetchone()[0]
        refund = max(0, (row[1] - row[2] - outstanding) * row[3] - row[5])
        if refund:
            point_change(db, row[0], refund, "campaign_refund", f"{key}:{row[5] + refund}")
        if refund or not outstanding:
            db.execute(
                "UPDATE funding_campaigns SET refunded=refunded+?,status=?,updated_at=CURRENT_TIMESTAMP WHERE campaign_id=?",
                (refund, "pausing" if outstanding else "cancelled", key),
            )
        return refund


def cancel_campaign(user, key):
    with connect() as db:
        row = db.execute(
            "SELECT status FROM funding_campaigns WHERE campaign_id=? AND user_id=? FOR UPDATE", (key, user)
        ).fetchone()
        if not row:
            raise ValueError("الحملة ليست ضمن حسابك.")
        if row[0] not in ("active", "pausing"):
            return 0
        db.execute(
            "UPDATE funding_campaigns SET status='pausing',updated_at=CURRENT_TIMESTAMP WHERE campaign_id=?", (key,)
        )
    return settle_campaign(key)


def increase_campaign(user, key, extra, request_key):
    if not 1 <= extra <= 100000 or not request_key:
        raise ValueError("العدد أو معرف طلب الزيادة غير صالح.")
    with connect() as db:
        row = db.execute(
            "SELECT quantity,cost,status FROM funding_campaigns WHERE campaign_id=? AND user_id=? FOR UPDATE",
            (key, user),
        ).fetchone()
        if not row:
            raise ValueError("الحملة ليست ضمن حسابك.")
        old = db.execute(
            "SELECT quantity FROM funding_adjustments WHERE request_key=? AND user_id=? AND campaign_id=?",
            (request_key, user, key),
        ).fetchone()
        if old:
            return old[0]
        maximum, enabled = db.execute("SELECT maximum,enabled FROM funding_config WHERE id=1").fetchone()
        if row[2] != "active" or not enabled or row[0] + extra > maximum:
            raise ValueError("الحملة متوقفة أو الزيادة تتجاوز الحد المسموح.")
        point_change(db, user, -extra * row[1], "campaign_increase", request_key)
        db.execute(
            "INSERT INTO funding_adjustments(request_key,campaign_id,user_id,quantity,cost) VALUES(?,?,?,?,?)",
            (request_key, key, user, extra, extra * row[1]),
        )
        db.execute(
            "UPDATE funding_campaigns SET quantity=quantity+?,updated_at=CURRENT_TIMESTAMP WHERE campaign_id=?",
            (extra, key),
        )
        return extra


def cancel_order(user, key):
    with connect() as db:
        row = db.execute(
            "SELECT status FROM promotion_orders WHERE order_id=? AND user_id=? FOR UPDATE", (key, user)
        ).fetchone()
        if not row:
            raise ValueError("الطلب ليس ضمن حسابك.")
        if row[0] not in ("running", "review"):
            return
        db.execute(
            "UPDATE promotion_orders SET cancel_requested=TRUE,updated_at=CURRENT_TIMESTAMP WHERE order_id=?", (key,)
        )
        db.execute(
            "UPDATE promotion_items SET status='failed',result='cancelled' WHERE order_id=? AND status='pending'",
            (key,),
        )
    settle(key)
