"""Persistent owner-scoped replies and schedules with explicit uncertain states."""

import asyncio
import contextlib
import datetime as dt
import io
import json
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from telethon import events, functions, errors
from telethon.sessions import StringSession

import account_access
from config import API_ID, API_HASH
from postgres import connect
from session_guard import TelegramClient
from utils.asyncio_compat import to_thread
from workflow import query, queue_notice

RULE_KINDS = {"welcome", "away", "keyword", "exact", "contains"}
JOB_KINDS = {"post", "profile", "health_scan"}
_clients = {}
_locks = {}
_workers = {}
_stopping = False


async def access_active(user):
    from helpers import is_banned
    from handlers.base import user_has_license_access

    return not await is_banned(user) and (await user_has_license_access(user))[0]


async def save_wait(phone, seconds):
    await query(
        """INSERT INTO automation_waits(phone,until_at) VALUES(?,CURRENT_TIMESTAMP+?*INTERVAL '1 second')
        ON CONFLICT(phone) DO UPDATE SET until_at=GREATEST(automation_waits.until_at,excluded.until_at)""",
        (phone, max(1, int(seconds))),
        commit=True,
    )


def validate_rule(kind, payload):
    if kind not in RULE_KINDS or not isinstance(payload, dict):
        raise ValueError("نوع قاعدة الرد غير صالح.")
    value = dict(payload)
    ZoneInfo(value.get("timezone", "Asia/Baghdad"))
    if value.get("scope", "private") not in {"private", "groups"}:
        raise ValueError("نطاق الرد غير صالح.")
    for key in ("chats", "exclude_chats", "exclude_senders"):
        value[key] = [int(i) for i in value.get(key, [])]
        if len(value[key]) > 100:
            raise ValueError("عدد الاستثناءات أكبر من المسموح.")
    if value.get("scope") == "groups" and not value["chats"]:
        raise ValueError("حدد المجموعات التي يسمح بالرد فيها.")
    value["priority"] = int(value.get("priority", 50))
    value["cooldown"] = int(value.get("cooldown", 3600))
    if not 60 <= value["cooldown"] <= 2592000:
        raise ValueError("فترة منع تكرار الرد من دقيقة إلى 30 يومًا.")
    value["start_hour"], value["end_hour"] = int(value.get("start_hour", 0)), int(value.get("end_hour", 24))
    if not 0 <= value["start_hour"] <= 23 or not 1 <= value["end_hour"] <= 24:
        raise ValueError("الساعات غير صالحة.")
    value["days"] = [int(d) for d in value.get("days", range(7))]
    if any(d < 0 or d > 6 for d in value["days"]):
        raise ValueError("أيام الأسبوع غير صالحة.")
    if kind in {"keyword", "exact", "contains"} and not str(value.get("match", "")).strip():
        raise ValueError("كلمة المطابقة مطلوبة.")
    if not value.get("template_id") and not str(value.get("text", "")).strip():
        raise ValueError("أدخل نص الرد أو قالبًا محفوظًا.")
    if len(value.get("text", "")) > 4096:
        raise ValueError("النص أطول من المسموح.")
    return value


def matches(kind, payload, text, private, chat, sender, now):
    local = now.astimezone(ZoneInfo(payload.get("timezone", "Asia/Baghdad")))
    start, end = payload.get("start_hour", 0), payload.get("end_hour", 24)
    inside = start <= local.hour < end if start < end else local.hour >= start or local.hour < end
    work = inside and local.weekday() in payload.get("days", range(7))
    if (kind == "away" and work) or (kind != "away" and not work):
        return False
    if private != (payload.get("scope", "private") == "private"):
        return False
    if not private and chat not in payload.get("chats", []):
        return False
    if chat in payload.get("exclude_chats", []) or sender in payload.get("exclude_senders", []):
        return False
    needle, hay = str(payload.get("match", "")).casefold(), (text or "").casefold()
    return kind in {"welcome", "away"} or (
        hay == needle if kind == "exact" else needle in hay.split() if kind == "keyword" else needle in hay
    )


async def save_rule(user, phones, kind, payload, rule_id=None):
    payload = validate_rule(kind, payload)
    owned = {a[0] for a in await account_access.accounts(user, owned_only=True)}
    if not phones or not set(phones) <= owned:
        raise ValueError("الأتمتة متاحة للحسابات المملوكة حاليًا فقط.")
    if payload.get("template_id") and not await query(
        "SELECT 1 FROM automation_templates WHERE template_id=? AND user_id=?",
        (int(payload["template_id"]), user),
        fetchone=True,
    ):
        raise ValueError("القالب غير موجود ضمن حسابك.")
    if rule_id:
        if len(phones) != 1:
            raise ValueError("لتعديل قاعدة اختر حسابًا واحدًا. استخدم النسخ لتطبيقها على مجموعة.")
        row = await query(
            "UPDATE automation_rules SET kind=?,payload=?,phone=?,enabled=FALSE WHERE rule_id=? AND user_id=? RETURNING rule_id",
            (kind, json.dumps(payload, ensure_ascii=False), phones[0], rule_id, user),
            commit=True,
            fetchone=True,
        )
        if not row:
            raise ValueError("القاعدة غير موجودة.")
        return [row[0]]
    ids = []
    for phone in phones:
        row = await query(
            "INSERT INTO automation_rules(user_id,phone,kind,payload) VALUES(?,?,?,?) RETURNING rule_id",
            (user, phone, kind, json.dumps(payload, ensure_ascii=False)),
            commit=True,
            fetchone=True,
        )
        ids.append(row[0])
    return ids


async def save_job(user, phones, kind, payload, due, timezone, ends=None, interval=None, job_id=None):
    if kind not in JOB_KINDS or not phones or interval is not None and not 5 <= int(interval) <= 525600:
        raise ValueError("نوع المهمة أو فترة التكرار غير صالح.")
    zone = ZoneInfo(timezone)
    due = dt.datetime.fromisoformat(due)
    if due.tzinfo is None:
        due = due.replace(tzinfo=zone)
    ends = dt.datetime.fromisoformat(ends) if ends else None
    if ends and ends.tzinfo is None:
        ends = ends.replace(tzinfo=zone)
    if due <= dt.datetime.now(dt.timezone.utc) or ends and ends <= due:
        raise ValueError("حدد بداية مستقبلية ونهاية بعد البداية.")
    owned = {a[0] for a in await account_access.accounts(user, owned_only=True)}
    if not set(phones) <= owned:
        raise ValueError("الحسابات ليست مملوكة لك حاليًا.")
    if kind == "post":
        if not payload.get("target") or not payload.get("text") and not payload.get("template_id"):
            raise ValueError("حدد وجهة النشر والنص أو القالب.")
        if str(payload["target"]).lstrip("-").isdigit():
            payload["target"] = int(payload["target"])
        elif not str(payload["target"]).startswith("@"):
            raise ValueError("وجهة النشر: معرف @ أو معرف رقمي.")
    if kind == "profile" and (
        not payload
        or not set(payload) <= {"first_name", "last_name", "about"}
        or any(len(str(v)) > 70 for v in payload.values())
    ):
        raise ValueError("تغييرات الملف المدعومة: first_name وlast_name وabout بطول 70 حرفًا كحد أقصى.")
    if payload.get("template_id") and not await query(
        "SELECT 1 FROM automation_templates WHERE user_id=? AND template_id=?",
        (user, int(payload["template_id"])),
        fetchone=True,
    ):
        raise ValueError("القالب غير متاح.")

    def save():
        with connect() as db:
            if job_id:
                row = db.execute(
                    """UPDATE automation_jobs SET kind=?,payload=?,due_at=?,ends_at=?,timezone=?,
                    interval_minutes=?,status='paused',cancel_requested=FALSE WHERE job_id=? AND user_id=?
                    AND status<>'running' RETURNING job_id""",
                    (kind, json.dumps(payload, ensure_ascii=False), due, ends, timezone, interval, job_id, user),
                ).fetchone()
                if not row:
                    raise ValueError("أوقف المهمة وانتظر انتهاء تنفيذها قبل تعديلها.")
                db.execute("DELETE FROM automation_job_accounts WHERE job_id=?", (job_id,))
            else:
                row = db.execute(
                    """INSERT INTO automation_jobs(user_id,kind,payload,due_at,ends_at,timezone,interval_minutes)
                    VALUES(?,?,?,?,?,?,?) RETURNING job_id""",
                    (user, kind, json.dumps(payload, ensure_ascii=False), due, ends, timezone, interval),
                ).fetchone()
            for phone in phones:
                db.execute("INSERT INTO automation_job_accounts(job_id,phone) VALUES(?,?)", (row[0], phone))
            return row[0]

    return await to_thread(save)


@asynccontextmanager
async def connection(account):
    phone, session = account[:2]
    lock = _locks.setdefault(phone, asyncio.Lock())
    async with lock:
        wait = await query(
            "SELECT CEIL(EXTRACT(EPOCH FROM until_at-CURRENT_TIMESTAMP)) FROM automation_waits WHERE phone=? AND until_at>CURRENT_TIMESTAMP",
            (phone,),
            fetchone=True,
        )
        if wait:
            raise errors.FloodWaitError(None, capture=int(wait[0]))
        existing = _clients.get(phone)
        if existing and existing[1] != session:
            await existing[0].disconnect()
            _clients.pop(phone, None)
            existing = None
        temporary = existing is None
        client = (
            existing[0]
            if existing
            else TelegramClient(
                StringSession(session), API_ID, API_HASH, exclusive_session=True, flood_sleep_threshold=0
            )
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise PermissionError("invalid_session")
            yield client
        finally:
            if temporary:
                await client.disconnect()


async def send_template(client, user, target, payload):
    if payload.get("template_id"):
        row = await query(
            "SELECT text,media,media_name FROM automation_templates WHERE template_id=? AND user_id=?",
            (int(payload["template_id"]), user),
            fetchone=True,
        )
        if not row:
            raise ValueError("template_missing")
        if row[1]:
            stream = io.BytesIO(bytes(row[1]))
            stream.name = row[2] or "attachment.bin"
            return await client.send_file(target, stream, caption=row[0], parse_mode=None)
        return await client.send_message(target, row[0], parse_mode=None)
    return await client.send_message(target, payload["text"], parse_mode=None)


def reserve_reply_sync(phone, chat, message, rule, sender, cooldown):
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"{phone}:{sender}",))
        prior = db.execute(
            "SELECT last_reply FROM automation_cooldowns WHERE rule_id=? AND sender_id=? AND last_reply>CURRENT_TIMESTAMP-(? * INTERVAL '1 second')",
            (rule, sender, cooldown),
        ).fetchone()
        if prior:
            return False
        if not db.execute(
            """INSERT INTO automation_events(phone,chat_id,message_id,status) VALUES(?,?,?,'uncertain')
            ON CONFLICT DO NOTHING RETURNING phone""",
            (phone, chat, message),
        ).fetchone():
            return False
        db.execute(
            """INSERT INTO automation_cooldowns(rule_id,sender_id,last_reply) VALUES(?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(rule_id,sender_id) DO UPDATE SET last_reply=CURRENT_TIMESTAMP""",
            (rule, sender),
        )
        return True


async def on_message(user, account, event):
    if not await access_active(user):
        return
    if event.out or not event.sender_id or event.sender_id == getattr(event.client, "_automation_me", None):
        return
    sender = await event.get_sender()
    if getattr(sender, "bot", False) or getattr(event.message, "via_bot_id", None):
        return
    if not await account_access.authorized(user, account, owned_only=True):
        return
    # Never reply to another automated account managed by this installation.
    if event.sender_id in {getattr(c[0], "_automation_me", None) for c in _clients.values()}:
        return
    if await query(
        "SELECT 1 FROM automation_manual WHERE user_id=? AND phone=? AND chat_id=? AND until_at>CURRENT_TIMESTAMP",
        (user, account[0], event.chat_id),
        fetchone=True,
    ):
        return
    rules = (
        await query(
            "SELECT rule_id,kind,payload FROM automation_rules WHERE user_id=? AND phone=? AND enabled",
            (user, account[0]),
            fetchall=True,
        )
        or []
    )
    rules = sorted([(r[0], r[1], json.loads(r[2])) for r in rules], key=lambda r: (r[2].get("priority", 50), r[0]))
    for rid, kind, payload in rules:
        if not matches(
            kind,
            payload,
            event.raw_text,
            event.is_private,
            event.chat_id,
            event.sender_id,
            dt.datetime.now(dt.timezone.utc),
        ):
            continue
        if kind == "welcome" and await query(
            "SELECT 1 FROM automation_cooldowns WHERE rule_id=? AND sender_id=?", (rid, event.sender_id), fetchone=True
        ):
            continue
        async with connection(account) as client:
            # Recheck pause and rule state after waiting for another operation.
            if not await query(
                "SELECT 1 FROM automation_rules WHERE rule_id=? AND user_id=? AND enabled", (rid, user), fetchone=True
            ):
                return
            if not await account_access.authorized(user, account, owned_only=True):
                return
            if not await to_thread(
                reserve_reply_sync,
                account[0],
                event.chat_id,
                event.id,
                rid,
                event.sender_id,
                payload.get("cooldown", 3600),
            ):
                return
            result = "completed"
            try:
                await send_template(client, user, event.chat_id, payload)
            except errors.FloodWaitError as exc:
                result = "flood_wait"
                await save_wait(account[0], exc.seconds)
                await query("UPDATE automation_rules SET enabled=FALSE WHERE rule_id=?", (rid,), commit=True)
                await queue_notice(
                    user,
                    f"auto:wait:{rid}:{event.id}",
                    f"توقف الرد على +{account[0]}: انتظار تيليجرام {exc.seconds} ثانية.",
                )
            except Exception:
                result = "uncertain"
            await query(
                "UPDATE automation_events SET status=? WHERE phone=? AND chat_id=? AND message_id=?",
                (result, account[0], event.chat_id, event.id),
                commit=True,
            )
            await log(user, account[0], "reply", result)
        break


async def log(user, phone, action, result):
    await query(
        "INSERT INTO automation_log(user_id,phone,action,result) VALUES(?,?,?,?)",
        (user, phone, action, result),
        commit=True,
    )


async def reconcile_listeners():
    rows = await query("SELECT DISTINCT user_id,phone FROM automation_rules WHERE enabled", fetchall=True) or []
    wanted = set()
    for user, phone in rows:
        if not await access_active(user):
            await stop_all(user)
            continue
        account = await account_access.resolve(user, phone, owned_only=True)
        if not account:
            await query(
                "UPDATE automation_rules SET enabled=FALSE WHERE user_id=? AND phone=?", (user, phone), commit=True
            )
            continue
        wanted.add(phone)
        current = _clients.get(phone)
        if current and (current[1] != account[1] or current[2] != user or not current[0].is_connected()):
            await current[0].disconnect()
            _clients.pop(phone, None)
        if phone not in _clients:
            client = TelegramClient(
                StringSession(account[1]), API_ID, API_HASH, exclusive_session=True, flood_sleep_threshold=0
            )
            client._automation_preemptible = True
            try:
                await client.connect()
                me = await client.get_me()
                if not me:
                    raise PermissionError()
                client._automation_me = me.id

                async def listener(event, u=user, a=account):
                    try:
                        await on_message(u, a, event)
                    except Exception:
                        await log(u, a[0], "reply", "failed")

                client.add_event_handler(listener, events.NewMessage(incoming=True))
                _clients[phone] = (client, account[1], user)
            except PermissionError:
                await client.disconnect()
                await query(
                    "UPDATE automation_rules SET enabled=FALSE WHERE user_id=? AND phone=?", (user, phone), commit=True
                )
            except Exception:
                await client.disconnect()
    for phone in set(_clients) - wanted:
        client, _, _ = _clients.pop(phone)
        async with _locks.setdefault(phone, asyncio.Lock()):
            await client.disconnect()


def claim_jobs_sync(slots=2):
    with connect() as db:
        db.execute(
            "UPDATE automation_jobs SET status='completed' WHERE status='pending' AND ends_at<=CURRENT_TIMESTAMP"
        )
        return db.execute(
            """UPDATE automation_jobs SET status='running' WHERE job_id IN (
            SELECT job_id FROM automation_jobs WHERE status='pending' AND NOT cancel_requested AND due_at<=CURRENT_TIMESTAMP
            ORDER BY due_at FOR UPDATE SKIP LOCKED LIMIT ?) RETURNING job_id,user_id,kind,payload,run_count""",
            (slots,),
        ).fetchall()


async def run_job(row):
    job, user, kind, raw, run = row
    payload = json.loads(raw)
    results = (
        await query(
            "SELECT phone,status,run_count FROM automation_job_accounts WHERE job_id=? ORDER BY phone",
            (job,),
            fetchall=True,
        )
        or []
    )
    cancelled = False
    try:
        if not await access_active(user):
            await stop_all(user)
            await query("UPDATE automation_jobs SET status='paused' WHERE job_id=?", (job,), commit=True)
            return
        for phone, state, previous_run in results:
            flag = await query(
                "SELECT cancel_requested,status FROM automation_jobs WHERE job_id=?", (job,), fetchone=True
            )
            if not flag or flag[0] or flag[1] != "running":
                cancelled = True
                break
            if (
                state in {"invalid", "permission_lost"}
                or previous_run == run
                and state in {"completed", "uncertain", "running"}
            ):
                continue
            account = await account_access.resolve(user, phone, owned_only=True)
            if not account:
                result = "permission_lost"
            else:
                result = "failed"
                await query(
                    "UPDATE automation_job_accounts SET status='running',run_count=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND phone=?",
                    (run, job, phone),
                    commit=True,
                )
                try:
                    async with connection(account) as client:
                        if not await account_access.authorized(user, account, owned_only=True):
                            raise PermissionError()
                        flag = await query(
                            "SELECT cancel_requested FROM automation_jobs WHERE job_id=?", (job,), fetchone=True
                        )
                        if flag[0]:
                            cancelled = True
                            break
                        if kind == "post":
                            entity = await client.get_entity(payload["target"])
                            permissions = await client.get_permissions(entity, "me")
                            if not (
                                getattr(permissions, "is_creator", False)
                                or getattr(permissions, "post_messages", False)
                                or not getattr(entity, "broadcast", False)
                                and getattr(permissions, "send_messages", False)
                            ):
                                raise PermissionError()
                            await send_template(client, user, entity, payload)
                        elif kind == "profile":
                            await client(functions.account.UpdateProfileRequest(**payload))
                        else:
                            me = await client.get_me()
                            if not me or getattr(me, "deleted", False) or str(me.phone) != phone:
                                raise PermissionError()
                            await queue_notice(
                                user,
                                f"auto:health:{job}:{run}:{phone}",
                                f"فحص +{phone}: متصل وقت الفحص؛ لا يثبت خلوه من القيود.",
                            )
                        result = "completed"
                except PermissionError:
                    result = "permission_lost"
                except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.UserDeactivatedError):
                    result = "invalid"
                    await query(
                        "UPDATE automation_rules SET enabled=FALSE WHERE phone=? AND user_id=?",
                        (phone, user),
                        commit=True,
                    )
                except errors.FloodWaitError as exc:
                    result = f"flood_wait:{exc.seconds}"
                    await save_wait(phone, exc.seconds)
                except Exception:
                    # The server may have accepted a send before the connection died.
                    result = "uncertain"
            await query(
                "UPDATE automation_job_accounts SET status=?,result=?,run_count=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND phone=?",
                (result, result, run, job, phone),
                commit=True,
            )
            await log(user, phone, kind, result)
        uncertain = await query(
            "SELECT 1 FROM automation_job_accounts WHERE job_id=? AND run_count=? AND status IN ('uncertain','running')",
            (job, run),
            fetchone=True,
        )
        await query(
            """UPDATE automation_jobs SET status=CASE WHEN ? THEN 'needs_review' WHEN cancel_requested OR ? THEN 'paused'
            WHEN interval_minutes IS NOT NULL AND (ends_at IS NULL OR
            CURRENT_TIMESTAMP+interval_minutes*INTERVAL '1 minute'<ends_at) THEN 'pending' ELSE 'completed' END,
            due_at=CASE WHEN NOT cancel_requested AND NOT ? AND NOT ? AND interval_minutes IS NOT NULL
                THEN CURRENT_TIMESTAMP+interval_minutes*INTERVAL '1 minute' ELSE due_at END,
            run_count=CASE WHEN cancel_requested OR ? OR ? THEN run_count ELSE run_count+1 END WHERE job_id=?""",
            (bool(uncertain), cancelled, cancelled, bool(uncertain), cancelled, bool(uncertain), job),
            commit=True,
        )
        await queue_notice(user, f"auto:job:{job}:{run}", f"انتهت دورة المهمة #{job}. راجع سجل الأتمتة لنتيجة كل حساب.")
    except asyncio.CancelledError:
        await query(
            "UPDATE automation_job_accounts SET status='uncertain' WHERE job_id=? AND status='running'",
            (job,),
            commit=True,
        )
        await query("UPDATE automation_jobs SET status='needs_review' WHERE job_id=?", (job,), commit=True)
        raise
    except Exception:
        await query("UPDATE automation_jobs SET status='needs_review' WHERE job_id=?", (job,), commit=True)


async def recover():
    global _stopping
    _stopping = False
    await query("UPDATE automation_job_accounts SET status='uncertain' WHERE status='running'", commit=True)
    await query("UPDATE automation_jobs SET status='needs_review' WHERE status='running'", commit=True)


async def dispatch():
    if _stopping:
        return
    await reconcile_listeners()
    if len(_workers) >= 2:
        return
    for row in await to_thread(claim_jobs_sync, 2 - len(_workers)):
        task = asyncio.create_task(run_job(row), name=f"automation-{row[0]}")
        _workers[row[0]] = task
        task.add_done_callback(lambda done, key=row[0]: _workers.pop(key, None))


async def stop_all(user):
    await query("UPDATE automation_rules SET enabled=FALSE WHERE user_id=?", (user,), commit=True)
    await query(
        "UPDATE automation_jobs SET cancel_requested=TRUE,status=CASE WHEN status='running' THEN status ELSE 'paused' END WHERE user_id=?",
        (user,),
        commit=True,
    )
    await log(user, None, "stop_all", "requested")


async def stop():
    global _stopping
    _stopping = True
    tasks = list(_workers.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for client, _, _ in list(_clients.values()):
        with contextlib.suppress(Exception):
            await client.disconnect()
    _clients.clear()
