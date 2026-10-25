"""Physical terminal-task cleanup with scoped, single-use database previews."""

import json
import secrets

from postgres import connect
import promotion_store
from utils.asyncio_compat import to_thread
from workflow import query

# Financial records, reward deduplication and ongoing membership watches are separate.
TABLES = {
    "workflow": ("workflow_jobs", "job_id", "status IN ('completed','cancelled')"),
    "automation": ("automation_jobs", "job_id", "status IN ('completed','cancelled')"),
    "scheduled": ("scheduled_operations", "schedule_id", "status IN ('completed','cancelled')"),
    "funding": ("funding_tasks", "task_id", "status='completed'"),
}


def preview(actor, scope="mine"):
    if scope not in ("mine", "all"):
        raise ValueError("نطاق التنظيف غير صالح.")
    if scope == "all":
        promotion_store.developer(actor)
    target = actor if scope == "mine" else None
    key = secrets.token_hex(16)
    counts = {}
    with connect() as db:
        db.execute(
            "INSERT INTO task_cleanup_batches(batch_id,actor_id,target_user) VALUES(?,?,?)", (key, actor, target)
        )
        for kind, (table, identity, terminal) in TABLES.items():
            condition = " AND user_id=?" if target is not None else ""
            args = (key, kind, target) if target is not None else (key, kind)
            db.execute(
                f"INSERT INTO task_cleanup_candidates(batch_id,kind,item_id) SELECT ?,?,{identity}::text FROM {table} WHERE {terminal}"
                + condition,
                args,
            )
            counts[kind] = db.execute(
                "SELECT COUNT(*) FROM task_cleanup_candidates WHERE batch_id=? AND kind=?", (key, kind)
            ).fetchone()[0]
    return dict(quote_id=key, counts=counts, total=sum(counts.values()), scope=scope)


def confirm(actor, key):
    with connect() as db:
        batch = db.execute(
            "SELECT target_user,status,result,expires_at>CURRENT_TIMESTAMP FROM task_cleanup_batches WHERE batch_id=? AND actor_id=? FOR UPDATE",
            (key, actor),
        ).fetchone()
        if not batch:
            raise ValueError("معاينة التنظيف ليست ضمن حسابك.")
        if batch[0] is None:
            promotion_store.developer(actor)
        if batch[1] == "completed":
            return json.loads(batch[2])
        if not batch[3]:
            raise ValueError("انتهت معاينة التنظيف؛ اطلب معاينة جديدة.")
        counts = {}
        for kind, (table, identity, terminal) in TABLES.items():
            condition = " AND user_id=?" if batch[0] is not None else ""
            args = (key, kind, batch[0]) if batch[0] is not None else (key, kind)
            selected = db.execute(
                f"""SELECT {identity} FROM {table} WHERE {terminal} AND {identity}::text IN
                (SELECT item_id FROM task_cleanup_candidates WHERE batch_id=? AND kind=?)"""
                + condition
                + " FOR UPDATE",
                args,
            ).fetchall()
            ids = [r[0] for r in selected]
            if ids:
                if kind == "workflow":
                    db.execute("DELETE FROM search_join_results WHERE job_id=ANY(?)", (ids,))
                db.execute(f"DELETE FROM {table} WHERE {identity}=ANY(?)", (ids,))
            counts[kind] = len(ids)
        result = dict(counts=counts, total=sum(counts.values()))
        db.execute(
            "UPDATE task_cleanup_batches SET status='completed',result=? WHERE batch_id=?", (json.dumps(result), key)
        )
        db.execute("DELETE FROM task_cleanup_candidates WHERE batch_id=?", (key,))
        db.execute(
            "INSERT INTO audit_log(user_id,action) VALUES(?,?)",
            (actor, f"terminal_tasks_cleaned:{key}:{result['total']}"),
        )
        return result


async def listing(user):
    rows = (
        await query(
            """SELECT id,kind,status,updated,total,processed FROM (
        SELECT job_id::text id,kind,status,updated_at updated,total,processed FROM workflow_jobs WHERE user_id=?
        UNION ALL SELECT job_id::text,'automation:'||kind,status,due_at,NULL,run_count FROM automation_jobs WHERE user_id=?
        UNION ALL SELECT schedule_id::text,'schedule:'||kind,status,due_at,NULL,NULL FROM scheduled_operations WHERE user_id=?
        UNION ALL SELECT task_id,'funding',status,COALESCE(verified_at,expires_at),1,CASE WHEN status='completed' THEN 1 ELSE 0 END FROM funding_tasks WHERE user_id=?
        ) tasks ORDER BY updated DESC LIMIT 100""",
            (user, user, user, user),
            fetchall=True,
        )
        or []
    )
    return [dict(zip(("id", "kind", "status", "date", "total", "processed"), r)) for r in rows]


async def menu(event, action, value):
    from keyboards import Button

    if action == "cleanup":
        result = await to_thread(preview, event.sender_id, value if value in ("all", "mine") else "mine")
        labels = {
            "workflow": "مهام التشغيل",
            "automation": "مهام الأتمتة",
            "scheduled": "المهام المجدولة",
            "funding": "مهام الاشتراك المكتملة",
        }
        text = "حذف المهام المكتملة والملغاة\n" + "\n".join(f"{labels[k]}: {v}" for k, v in result["counts"].items())
        text += "\nسيُحذف سجل المهام ونتائجها المرتبطة من قاعدة البيانات. تبقى الأرصدة والسجل المالي ومتابعة الاشتراكات محفوظة."
        return await event.edit(
            text,
            parse_mode=None,
            buttons=[
                [Button.inline("تأكيد حذف المهام المنتهية", data=f"ops:jobs:purge:{result['quote_id']}")],
                [Button.inline("رجوع", data="ops:jobs:list:0")],
            ],
        )
    result = await to_thread(confirm, event.sender_id, value)
    return await event.edit(
        f"حُذفت {result['total']} مهمة من قاعدة البيانات.",
        buttons=[[Button.inline("مركز المهام", data="ops:jobs:list:0")]],
    )
