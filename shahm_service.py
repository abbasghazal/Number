"""Run Shahm operations with the project's ownership, jobs and session policy."""

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass

from telethon import errors

import account_access
import automation
import workflow
from account_health import session_is_usable
from session_guard import SessionBusyError
from shahm_engine import BotProfile, BotSession, ShahmError, profile_for, transfer_values

LOG = logging.getLogger(__name__)
ACTIONS = {"collect", "transfer", "gift", "buttons", "press"}
LABELS = {"collect": "تجميع", "transfer": "تحويل", "gift": "هدية", "buttons": "عرض الأزرار", "press": "ضغط زر"}
_runs = {}
_reserved = set()
_stopping = False


@dataclass
class Run:
    user: int
    phones: tuple
    task: asyncio.Task | None = None


async def access_active(user):
    return await automation.access_active(user)


async def checkpoint(user, account):
    await workflow.progress()
    if not await access_active(user) or not await account_access.authorized(user, account, owned_only=True):
        raise PermissionError("account_access")


async def pause(seconds, user, account):
    end = asyncio.get_running_loop().time() + max(0, seconds)
    while asyncio.get_running_loop().time() < end:
        await asyncio.sleep(min(5, end - asyncio.get_running_loop().time()))
        await checkpoint(user, account)


async def report(bot, user, job, suffix, text):
    try:
        await asyncio.wait_for(bot.send_message(user, text, parse_mode=None), 20)
    except Exception:
        await workflow.queue_notice(user, f"shahm:{job}:{suffix}", text)


def validate(action, profile, phones, values, repeat_seconds, limit, button):
    if action not in ACTIONS or not isinstance(profile, BotProfile):
        raise ShahmError("العملية أو تعريف البوت غير صالح.")
    if not phones or len(set(phones)) != len(phones):
        raise ShahmError("اختر حسابًا واحدًا على الأقل دون تكرار.")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ShahmError("حد المهام لكل حساب بين 1 و100.")
    if repeat_seconds is not None and (
        action != "collect" or not isinstance(repeat_seconds, int) or not 60 <= repeat_seconds <= 2592000
    ):
        raise ShahmError("التكرار متاح للتجميع بفاصل من 60 ثانية إلى 30 يومًا.")
    if action == "transfer":
        if len(phones) != 1:
            raise ShahmError("اختر حسابًا واحدًا للتحويل.")
        transfer_values(profile, values)
    if action == "press" and (not isinstance(button, int) or not 0 <= button <= 80):
        raise ShahmError("رقم الزر يجب أن يكون بين 0 و80.")
    # Re-validate profile values even when invoked outside the user interface.
    profile_for(profile.username, profile.start_buttons)


async def start(bot, user, phones, profile, action, *, values=(), repeat_seconds=None, limit=50, button=None):
    phones = tuple(str(p).lstrip("+") for p in phones)
    validate(action, profile, phones, values, repeat_seconds, limit, button)
    if _stopping:
        raise ShahmError("الخدمة تُغلق الآن؛ حاول بعد اكتمال التشغيل.")
    if set(phones) & _reserved:
        raise ShahmError("توجد عملية شهم على أحد الحسابات المختارة. أوقفها أو انتظر انتهاءها.")
    _reserved.update(phones)
    try:
        if not await access_active(user):
            raise ShahmError("يلزم اشتراك فعال وحساب غير محظور.")
        available = {a[0]: a for a in await account_access.accounts(user, owned_only=True)}
        if not set(phones) <= available.keys():
            raise ShahmError("الخدمة متاحة للحسابات المملوكة لك حاليًا فقط.")
        for phone in phones:
            if not await session_is_usable(available[phone][1]):
                raise ShahmError("أحد الحسابات يحتاج إلى مراجعة صحته قبل التشغيل.")
        job = secrets.token_hex(8)
        await workflow.query(
            "INSERT INTO workflow_jobs(job_id,user_id,kind,total,message) VALUES(?,?,?,?,?)",
            (job, user, "shahm_" + action, len(phones), f"{LABELS[action]} · {profile.username}"), commit=True,
        )
        run = Run(user, phones)
        _runs[job] = run
        run.task = asyncio.create_task(
            worker(bot, user, job, phones, profile, action, values, repeat_seconds, limit, button), name=f"shahm-{job}"
        )

        def finished(task):
            _runs.pop(job, None)
            _reserved.difference_update(phones)
            if not task.cancelled() and task.exception():
                LOG.error("Shahm worker stopped unexpectedly (%s)", type(task.exception()).__name__)

        run.task.add_done_callback(finished)
        return job
    except BaseException:
        _reserved.difference_update(phones)
        raise


async def perform(user, account, profile, action, values, limit, button):
    await checkpoint(user, account)
    if not await session_is_usable(account[1]):
        raise ShahmError("جلسة الحساب تحتاج إلى مراجعة صحته.")
    async with automation.connection(account) as client:
        me = await client.get_me()
        if not me or me.bot or str(getattr(me, "phone", "")).lstrip("+") != account[0].lstrip("+"):
            raise ShahmError("جلسة الحساب لا تطابق الرقم المختار.")
        engine = BotSession(client, profile, lambda: checkpoint(user, account), lambda s: pause(s, user, account))
        await engine.prepare()
        if action == "collect":
            stats, reason = await engine.collect(limit)
            return (
                f"المهام المعالجة: {stats['attempts']} | انضمام جديد: {stats['joined']}\n"
                f"أكد البوت نجاحها: {stats['verified']} | تعذر تنفيذها: {stats['skipped']}\n{reason}\n"
                "الرصيد يحدده البوت المستهدف؛ لا تُضاف نقاط إلى محفظة المشروع."
            )
        if action == "transfer":
            return await engine.transfer(values)
        if action == "gift":
            return await engine.gift()
        if action == "buttons":
            return await engine.buttons()
        return await engine.press(button)


async def worker(bot, user, job, phones, profile, action, values, repeat_seconds, limit, button):
    token = workflow.current_job.set(job)
    status, final = "completed", "اكتملت معالجة الطلب. راجع ردود البوت المستهدف للنتائج."
    processed = success = failed = round_id = 0
    try:
        while True:
            round_id += 1
            for phone in phones:
                await workflow.progress()
                account = await account_access.resolve(user, phone, owned_only=True)
                if account is None:
                    raise PermissionError("account_access")
                try:
                    text = await asyncio.wait_for(perform(user, account, profile, action, values, limit, button), 3600)
                except errors.FloodWaitError as exc:
                    await automation.save_wait(phone, exc.seconds + 1)
                    raise ShahmError(f"طلب تيليجرام الانتظار {exc.seconds} ثانية على +{phone}. توقفت المهمة.") from None
                except SessionBusyError:
                    raise ShahmError(f"الحساب +{phone} مشغول بخدمة أخرى. أوقفها أو انتظر انتهاءها.") from None
                except ShahmError as exc:
                    text = str(exc)
                    failed += 1
                else:
                    success += 1
                # Check the current owner again before forwarding external bot responses.
                await checkpoint(user, account)
                processed += 1
                await workflow.progress(total=round_id * len(phones), processed=processed, success=success, failed=failed)
                await report(bot, user, job, f"{round_id}:{phone}", f"شهم · {LABELS[action]} · +{phone} · {profile.username}\n{text}")
            if failed:
                status, final = "failed", "تعذر إكمال بعض العمليات؛ راجع النتائج قبل إعادة الطلب."
                break
            if repeat_seconds is None:
                break
            await report(bot, user, job, f"wait:{round_id}", f"انتهت دورة التجميع {round_id}. الدورة التالية بعد {repeat_seconds} ثانية. يمكنك الإيقاف من قسم شهم أو مركز المهام.")
            # Connections are released between rounds. Re-resolve sessions after the wait.
            end = asyncio.get_running_loop().time() + repeat_seconds
            while asyncio.get_running_loop().time() < end:
                await asyncio.sleep(min(5, end - asyncio.get_running_loop().time()))
                await workflow.progress()
                if not await access_active(user):
                    raise PermissionError("account_access")
                available = {a[0] for a in await account_access.accounts(user, owned_only=True)}
                if not set(phones) <= available:
                    raise PermissionError("account_access")
    except workflow.JobStopped:
        status, final = "cancelled", "أُوقفت المهمة. راجع آخر رد داخل البوت المستهدف قبل إعادة أي تحويل أو هدية."
    except asyncio.CancelledError:
        status, final = "interrupted" if _stopping else "cancelled", "توقفت المهمة. راجع آخر رد داخل البوت المستهدف قبل إعادة أي تحويل أو هدية."
    except PermissionError:
        status, final = "failed", "توقفت المهمة لانتهاء صلاحية الحساب أو الاشتراك."
    except ShahmError as exc:
        status, final = "failed", str(exc)
    except Exception as exc:
        LOG.warning("Shahm operation failed (%s)", type(exc).__name__)
        status, final = "failed", "تعذر الاتصال أو إكمال العملية. راجع البوت المستهدف وصحة الحساب قبل إعادة المحاولة."
    finally:
        workflow.current_job.reset(token)
        await workflow.query(
            "UPDATE workflow_jobs SET status=?,message=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND user_id=?",
            (status, final, job, user), commit=True,
        )
        if not _stopping:
            await report(bot, user, job, "end", f"شهم · المهمة {job}\n{final}")


async def stop_user(user):
    selected = [(job, run.task) for job, run in _runs.items() if run.user == user and run.task and not run.task.done()]
    for _, task in selected:
        task.cancel()
    if selected:
        await asyncio.gather(*(task for _, task in selected), return_exceptions=True)
    for job, _ in selected:
        # A task cancelled before its first turn cannot execute its finally block.
        await workflow.query(
            "UPDATE workflow_jobs SET status='cancelled',message=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND user_id=? AND status='running'",
            ("أُوقفت المهمة؛ راجع البوت المستهدف قبل إعادة الطلب.", job, user), commit=True,
        )
    return len(selected)


async def stop():
    global _stopping
    _stopping = True
    try:
        selected = list(_runs.items())
        for _, run in selected:
            if run.task:
                run.task.cancel()
        for job, run in selected:
            if run.task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run.task
                await workflow.query(
                    "UPDATE workflow_jobs SET status='interrupted',message=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND user_id=? AND status='running'",
                    ("توقفت المهمة أثناء إغلاق المشروع؛ راجع البوت المستهدف قبل إعادة الطلب.", job, run.user), commit=True,
                )
        _runs.clear()
        _reserved.clear()
    finally:
        _stopping = False
