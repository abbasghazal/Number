"""Search on each selected user session, join only the exact returned channel."""

import asyncio
import collections
import secrets
import re

from telethon import errors, functions, types
from telethon.sessions import StringSession

import account_access
import workflow
from config import API_ID, API_HASH
from session_guard import TelegramClient, SessionBusyError

LABELS = {
    "joined": "انضم بنجاح",
    "already": "منضم مسبقًا",
    "not_found": "لم تظهر القناة",
    "requested": "طلب انضمام ينتظر الموافقة",
    "invalid": "جلسة غير صالحة",
    "flood_wait": "انتظار مفروض من تيليجرام",
    "failed": "تعذر التنفيذ",
    "unauthorized": "انتهى الإذن",
    "unknown": "نتيجة غير محسومة",
    "cancelled": "أُلغي قبل الانضمام",
}
_tasks = {}


def channel_id(value):
    value = int(value)
    return -value - 1000000000000 if value <= -1000000000000 else abs(value)


async def search_and_join(client, phrase, target_id, checkpoint=None):
    result = await client(functions.contacts.SearchRequest(q=phrase, limit=100))
    # Never trust a display name, the first result, or a separately resolved peer.
    peers = {p.channel_id for p in list(result.results) + list(result.my_results) if isinstance(p, types.PeerChannel)}
    match = next(
        (c for c in result.chats if isinstance(c, types.Channel) and c.id == target_id and c.id in peers), None
    )
    if match is None:
        return "not_found"
    try:
        await client(functions.channels.GetParticipantRequest(channel=match, participant="me"))
        return "already"
    except errors.UserNotParticipantError:
        pass
    if checkpoint:
        await checkpoint()
    try:
        await client(functions.channels.JoinChannelRequest(channel=match))
    except errors.InviteRequestSentError:
        return "requested"
    except errors.UserAlreadyParticipantError:
        return "already"
    # A successful request must result in actual membership before counting it.
    try:
        await client(functions.channels.GetParticipantRequest(channel=match, participant="me"))
    except errors.UserNotParticipantError:
        return "unknown"
    return "joined"


async def start(bot, user, phrase, target, accounts):
    if not 2 <= len(phrase.strip()) <= 200 or not accounts:
        raise ValueError("عبارة البحث أو الحسابات غير صالحة.")
    first = accounts[0]
    if not await account_access.authorized(user, first):
        raise ValueError("الحساب لم يعد متاحًا.")
    client = TelegramClient(StringSession(first[1]), API_ID, API_HASH, exclusive_session=True, flood_sleep_threshold=0)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("جلسة الحساب غير صالحة.")
        if str(target).lstrip("-").isdigit():
            target_id = channel_id(target)
        else:
            if not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", str(target)):
                raise ValueError("أرسل معرف القناة العام أو معرفها الرقمي الثابت.")
            entity = await client.get_entity(target)
            if not isinstance(entity, types.Channel):
                raise ValueError("الهدف ليس قناة أو مجموعة عامة.")
            target_id = entity.id
    finally:
        await client.disconnect()
    job = secrets.token_hex(8)
    await workflow.query(
        "INSERT INTO workflow_jobs(job_id,user_id,kind,total) VALUES(?,?,?,?)",
        (job, user, "search_join", len(accounts)),
        commit=True,
    )
    task = asyncio.create_task(run(bot, user, job, phrase.strip(), target_id, accounts), name=f"search-{job}")
    _tasks[job] = task
    task.add_done_callback(lambda done: _tasks.pop(job, None))
    return job


async def run(bot, user, job, phrase, target_id, accounts):
    token = workflow.current_job.set(job)
    counts = collections.Counter()
    status = "completed"
    try:
        for account in accounts:
            await workflow.progress()
            phone, session = account[:2]
            result, wait = "failed", None
            await workflow.query(
                "INSERT INTO search_join_results(job_id,phone,result) VALUES(?,?,?)",
                (job, phone, "unknown"),
                commit=True,
            )
            try:
                if not await account_access.authorized(user, account):
                    result = "unauthorized"
                else:
                    client = TelegramClient(
                        StringSession(session), API_ID, API_HASH, exclusive_session=True, flood_sleep_threshold=0
                    )
                    try:
                        await client.connect()
                        if not await client.is_user_authorized():
                            result = "invalid"
                        else:

                            async def checkpoint():
                                await workflow.progress()
                                if not await account_access.authorized(user, account):
                                    raise PermissionError()

                            result = await search_and_join(client, phrase, target_id, checkpoint)
                    finally:
                        await client.disconnect()
            except errors.FloodWaitError as exc:
                result, wait = "flood_wait", exc.seconds
            except (
                errors.AuthKeyUnregisteredError,
                errors.AuthKeyInvalidError,
                errors.SessionExpiredError,
                errors.SessionRevokedError,
                errors.UserDeactivatedError,
                errors.UserDeactivatedBanError,
            ):
                result = "invalid"
            except PermissionError:
                result = "unauthorized"
            except (errors.RPCError, SessionBusyError, OSError, ValueError):
                result = "failed"
            counts[result] += 1
            await workflow.query(
                "UPDATE search_join_results SET result=?,wait_seconds=? WHERE job_id=? AND phone=?",
                (result, wait, job, phone),
                commit=True,
            )
            await account_access.audit(user, account, "search_join", result)
            await workflow.progress(
                processed=sum(counts.values()), success=counts["joined"], failed=sum(counts.values()) - counts["joined"]
            )
            await workflow.pause(3)
    except asyncio.CancelledError:
        status = "cancelled"
    except Exception:
        status = "failed"
    finally:
        workflow.current_job.reset(token)
        await workflow.query(
            "UPDATE workflow_jobs SET status=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (status, job), commit=True
        )
        rows = (
            await workflow.query(
                "SELECT result,COUNT(*) FROM search_join_results WHERE job_id=? GROUP BY result", (job,), fetchall=True
            )
            or []
        )
        report = f"🔎 تقرير رشق بحث · {status}\n" + "\n".join(f"{LABELS.get(r, r)}: {n}" for r, n in rows)
        report += f"\nلم يبدأ تنفيذها: {len(accounts) - sum(n for _, n in rows)}"
        await workflow.queue_notice(user, f"search:report:{job}", report)


async def stop():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
