"""Central automation panels; new configurations remain paused for preview."""

import asyncio
import json

from telethon import Button

import account_access
import automation
from messages import rule_preview, job_preview
from workflow import query
from .base import active_conversations
from .number_handlers import prompt
from .input_choices import choose, choose_accounts, confirm, input_session, preset


async def choose_phones(conv, user):
    accounts = await account_access.accounts(user, owned_only=True)
    if not accounts:
        raise ValueError("لا توجد حسابات مملوكة متاحة.")
    rows = await query("SELECT phone,folder FROM account_labels WHERE user_id=? AND folder IS NOT NULL",
                       (user,), fetchall=True) or []
    groups = {}
    for phone, folder in rows:
        groups.setdefault(folder, []).append(phone)
    return await choose_accounts(conv, [a[0] for a in accounts], groups=groups)


async def choose_timezone(conv):
    value = await choose(conv, "اختر المنطقة الزمنية:", [(z, z) for z in (
        "Asia/Baghdad", "Asia/Riyadh", "Asia/Dubai", "Africa/Cairo", "UTC",
    )] + [(None, "منطقة زمنية أخرى")])
    return value or await prompt(conv, "أدخل اسم المنطقة الزمنية مثل Asia/Baghdad:")


async def choose_days(conv):
    labels = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    selected = set()
    while True:
        value = await choose(conv, "حدد أيام العمل ثم اضغط تأكيد الأيام:",
            [(i, f"{'محدد · ' if i in selected else ''}{label}") for i, label in enumerate(labels)]
            + [("all", "كل الأيام"), ("done", "تأكيد الأيام"), ("reset", "مسح الأيام")])
        if value == "all":
            return list(range(7))
        if value == "reset":
            selected.clear()
        elif value == "done":
            if selected:
                return sorted(selected)
            await conv.send_message("اختر يومًا واحدًا على الأقل.", parse_mode=None)
        else:
            selected.symmetric_difference_update({value})


async def choose_content(conv, user, message):
    rows = await query("SELECT template_id,text FROM automation_templates WHERE user_id=? ORDER BY template_id DESC",
                       (user,), fetchall=True) or []
    if rows:
        key = await choose(conv, "اختر نصًا جديدًا أو قالبًا محفوظًا:",
                           [(None, "كتابة نص جديد")] + [(r[0], f"قالب #{r[0]} · {r[1][:35]}") for r in rows])
        if key is not None:
            return {"template_id": key}
    text = await prompt(conv, message)
    return {"template_id": int(text.split(":", 1)[1])} if text.startswith("قالب:") else {"text": text}


async def rule_wizard(event, edit=None, copy=None):
    user = event.chat_id
    async with event.client.conversation(user) as conv, input_session(conv):
        active_conversations[user] = conv
        try:
            phones = await choose_phones(conv, user)
            if copy:
                row = await query(
                    "SELECT kind,payload FROM automation_rules WHERE rule_id=? AND user_id=?",
                    (copy, user),
                    fetchone=True,
                )
                if not row:
                    raise ValueError("القاعدة غير موجودة.")
                kind, payload = row[0], json.loads(row[1])
            else:
                kind = await choose(conv, "اختر نوع الرد:", [
                    ("welcome", "ترحيب"), ("away", "خارج الدوام"), ("keyword", "كلمة"),
                    ("exact", "تطابق"), ("contains", "احتواء"),
                ])
                payload = await choose_content(conv, user, "نص الرد:")
                if kind in {"keyword", "exact", "contains"}:
                    payload["match"] = await prompt(conv, "النص المطلوب مطابقته:")
                payload["scope"] = await choose(conv, "نطاق الرد:", [("private", "خاص"), ("groups", "مجموعات")])
                if payload["scope"] == "groups":
                    payload["chats"] = [
                        int(x) for x in (await prompt(conv, "معرفات المجموعات المسموح بها بفواصل:")).split(",")
                    ]
                payload["timezone"] = await choose_timezone(conv)
                hours = await choose(conv, "ساعات العمل:", [("0-24", "طوال اليوم"), ("9-17", "9-17"), (None, "ساعات أخرى")])
                if hours is None:
                    hours = await prompt(conv, "ساعات العمل بصيغة بداية-نهاية، مثال 9-17:")
                payload["start_hour"], payload["end_hour"] = [int(x) for x in hours.split("-")]
                payload["days"] = await choose_days(conv)
                payload["priority"] = int(await preset(conv, "الأولوية: الأقل يُنفّذ أولًا:", [1, 5, 10]))
                payload["cooldown"] = int(await preset(conv, "منع تكرار الرد لنفس المرسل بالثواني:", [60, 300, 600]))
                for key, label in [("exclude_chats", "المحادثات المستثناة"), ("exclude_senders", "المرسلون المستثنون")]:
                    raw = await choose(conv, f"{label}:", [("-", "دون استثناءات"), (None, "إدخال المعرفات")])
                    if raw is None:
                        raw = await prompt(conv, "أدخل المعرفات بفواصل:")
                    payload[key] = [] if raw == "-" else [int(x) for x in raw.split(",")]
            ids = await automation.save_rule(user, phones, kind, payload, rule_id=edit)
            await conv.send_message(
                "✅ حُفظت القاعدة متوقفة. افتح معاينتها ثم فعّلها بعد مراجعة الإعدادات.",
                parse_mode=None,
                buttons=[[Button.inline(f"معاينة #{i}", data=f"auto:rule:{i}")] for i in ids],
            )
        except (ValueError, KeyError, asyncio.TimeoutError) as exc:
            await conv.send_message(str(exc) or "انتهى وقت الإدخال.", parse_mode=None)
        finally:
            active_conversations.pop(user, None)


async def job_wizard(event, edit=None):
    user = event.chat_id
    async with event.client.conversation(user) as conv, input_session(conv):
        active_conversations[user] = conv
        try:
            phones = await choose_phones(conv, user)
            kind = await choose(conv, "نوع المهمة:", [("post", "نشر"), ("profile", "ملف"), ("health_scan", "فحص")])
            payload = {}
            if kind == "post":
                payload["target"] = await prompt(conv, "وجهة النشر: @معرف أو المعرف الرقمي للقناة أو المجموعة:")
                payload.update(await choose_content(conv, user, "نص المنشور:"))
            if kind == "profile":
                payload["first_name"] = await prompt(conv, "الاسم الأول الجديد:")
                payload["last_name"] = await prompt(conv, "اسم العائلة الجديد، أو - لتفريغه:")
                payload["about"] = await prompt(conv, "النبذة الجديدة، أو - لتفريغها:")
                payload = {k: ("" if v == "-" else v) for k, v in payload.items()}
            timezone = await choose_timezone(conv)
            due = await prompt(conv, "وقت البداية: YYYY-MM-DDTHH:MM:SS؛ يُفسّر بالمنطقة المختارة:")
            ends = await choose(conv, "وقت النهاية:", [("-", "بلا نهاية"), (None, "تحديد وقت النهاية")])
            if ends is None:
                ends = await prompt(conv, "وقت النهاية: YYYY-MM-DDTHH:MM:SS:")
            interval = int(await preset(conv, "التكرار بالدقائق: 0 لمرة واحدة:", [0, 5, 15, 60, 1440]))
            job = await automation.save_job(
                user, phones, kind, payload, due, timezone, None if ends == "-" else ends, interval or None, job_id=edit
            )
            await conv.send_message(
                "✅ حُفظت المهمة متوقفة. راجع المعاينة قبل تشغيلها.",
                parse_mode=None,
                buttons=[[Button.inline("معاينة المهمة", data=f"auto:job:{job}")]],
            )
        except (ValueError, KeyError, asyncio.TimeoutError) as exc:
            await conv.send_message(str(exc) or "انتهى وقت الإدخال.", parse_mode=None)
        finally:
            active_conversations.pop(user, None)


async def menu(event, data="auto:menu"):
    user = event.chat_id
    parts = data.split(":")
    action = parts[1]
    key = int(parts[2]) if len(parts) > 2 else None
    try:
        if action == "templates":
            rows = (
                await query(
                    "SELECT template_id,text FROM automation_templates WHERE user_id=? ORDER BY template_id DESC LIMIT 30",
                    (user,),
                    fetchall=True,
                )
                or []
            )
            return await event.edit(
                "📝 قوالب الرد والنشر\nاختر قالبًا لمعاينته أو تعديله.",
                parse_mode=None,
                buttons=[[Button.inline(f"#{r[0]} · {r[1][:35]}", data=f"auto:templateview:{r[0]}")] for r in rows]
                + [[Button.inline("قالب جديد", data="auto:template")], [Button.inline("رجوع", data="auto:menu")]],
            )
        if action == "templateview":
            row = await query(
                "SELECT text,media,media_name FROM automation_templates WHERE template_id=? AND user_id=?",
                (key, user),
                fetchone=True,
            )
            if not row:
                raise ValueError("القالب غير موجود.")
            if row[1]:
                import io

                stream = io.BytesIO(bytes(row[1]))
                stream.name = row[2] or "attachment.bin"
                await event.client.send_file(user, stream, caption=row[0], parse_mode=None)
            return await event.edit(
                f"📝 القالب #{key}\n{row[0]}",
                parse_mode=None,
                buttons=[
                    [
                        Button.inline("تعديل", data=f"auto:templateedit:{key}"),
                        Button.inline("حذف", data=f"auto:templatedel:{key}"),
                    ],
                    [Button.inline("رجوع", data="auto:templates")],
                ],
            )
        if action == "templatedel":
            await query(
                "UPDATE automation_rules SET enabled=FALSE WHERE user_id=? AND payload::jsonb->>'template_id'=?",
                (user, str(key)),
                commit=True,
            )
            await query(
                "UPDATE automation_jobs SET cancel_requested=TRUE,status=CASE WHEN status='running' THEN status ELSE 'paused' END WHERE user_id=? AND payload::jsonb->>'template_id'=?",
                (user, str(key)),
                commit=True,
            )
            await query("DELETE FROM automation_templates WHERE template_id=? AND user_id=?", (key, user), commit=True)
            return await menu(event, "auto:templates")
        if action == "newrule":
            return await rule_wizard(event)
        if action == "newjob":
            return await job_wizard(event)
        if action == "editrule":
            return await rule_wizard(event, edit=key)
        if action == "copy":
            return await rule_wizard(event, copy=key)
        if action == "editjob":
            return await job_wizard(event, edit=key)
        if action == "stopall":
            await automation.stop_all(user)
        if action == "rule":
            row = await query(
                "SELECT phone,kind,payload,enabled FROM automation_rules WHERE rule_id=? AND user_id=?",
                (key, user),
                fetchone=True,
            )
            if not row:
                raise ValueError("القاعدة غير موجودة.")
            return await event.edit(
                rule_preview(key, row),
                parse_mode=None,
                buttons=[
                    [Button.inline("إيقاف" if row[3] else "تشغيل", data=f"auto:togglerule:{key}")],
                    [
                        Button.inline("تعديل", data=f"auto:editrule:{key}"),
                        Button.inline("نسخ إلى حسابات أخرى", data=f"auto:copy:{key}"),
                    ],
                    [Button.inline("حذف", data=f"auto:delrule:{key}")],
                    [Button.inline("رجوع", data="auto:menu")],
                ],
            )
        if action == "job":
            row = await query(
                "SELECT kind,payload,due_at,ends_at,timezone,interval_minutes,status FROM automation_jobs WHERE job_id=? AND user_id=?",
                (key, user),
                fetchone=True,
            )
            if not row:
                raise ValueError("المهمة غير موجودة.")
            progress = (
                await query(
                    "SELECT phone,status FROM automation_job_accounts WHERE job_id=? ORDER BY phone",
                    (key,),
                    fetchall=True,
                )
                or []
            )
            return await event.edit(
                job_preview(key, row, progress),
                parse_mode=None,
                buttons=[
                    [
                        Button.inline("تشغيل / استئناف", data=f"auto:startjob:{key}"),
                        Button.inline("إيقاف", data=f"auto:pausejob:{key}"),
                    ],
                    [
                        Button.inline("تعديل", data=f"auto:editjob:{key}"),
                        Button.inline("حذف", data=f"auto:deljob:{key}"),
                    ],
                    [Button.inline("رجوع", data="auto:menu")],
                ],
            )
        if action == "togglerule":
            row = await query(
                "SELECT phone FROM automation_rules WHERE rule_id=? AND user_id=?", (key, user), fetchone=True
            )
            if not row or not await account_access.resolve(user, row[0], owned_only=True):
                raise ValueError("الحساب لم يعد مملوكًا لك.")
            await query(
                "UPDATE automation_rules SET enabled=NOT enabled WHERE rule_id=? AND user_id=?",
                (key, user),
                commit=True,
            )
            return await menu(event, f"auto:rule:{key}")
        if action == "delrule":
            await query("DELETE FROM automation_rules WHERE rule_id=? AND user_id=?", (key, user), commit=True)
        if action == "startjob":
            phones = (
                await query(
                    "SELECT a.phone FROM automation_job_accounts a JOIN automation_jobs j ON j.job_id=a.job_id WHERE a.job_id=? AND j.user_id=?",
                    (key, user),
                    fetchall=True,
                )
                or []
            )
            allowed = bool(phones)
            for r in phones:
                if not await account_access.resolve(user, r[0], owned_only=True):
                    allowed = False
            if not allowed:
                raise ValueError("بعض الحسابات لم تعد مملوكة لك.")
            row = await query(
                "UPDATE automation_jobs SET status='pending',cancel_requested=FALSE WHERE job_id=? AND user_id=? AND status='paused' RETURNING job_id",
                (key, user),
                commit=True,
                fetchone=True,
            )
            if not row:
                raise ValueError("يمكن تشغيل مهمة متوقفة فقط. عدّل المهمة المكتملة أو التي تحتاج مراجعة أولًا.")
            return await menu(event, f"auto:job:{key}")
        if action == "pausejob":
            await query(
                "UPDATE automation_jobs SET cancel_requested=TRUE,status=CASE WHEN status='running' THEN status ELSE 'paused' END WHERE job_id=? AND user_id=?",
                (key, user),
                commit=True,
            )
            return await menu(event, f"auto:job:{key}")
        if action == "deljob":
            row = await query(
                "DELETE FROM automation_jobs WHERE job_id=? AND user_id=? AND status<>'running' RETURNING job_id",
                (key, user),
                commit=True,
                fetchone=True,
            )
            if not row:
                raise ValueError("أوقف المهمة وانتظر انتهاء الحساب الجاري قبل حذفها.")
        if action == "log":
            rows = (
                await query(
                    "SELECT phone,action,result,created_at FROM automation_log WHERE user_id=? ORDER BY id DESC LIMIT 25",
                    (user,),
                    fetchall=True,
                )
                or []
            )
            return await event.edit(
                "📋 سجل الأتمتة\n" + "\n".join(f"{r[3]} · {r[0] or ''} · {r[1]} · {r[2]}" for r in rows),
                parse_mode=None,
                buttons=[[Button.inline("رجوع", data="auto:menu")]],
            )
        if action == "manual":
            async with event.client.conversation(user) as conv, input_session(conv):
                active_conversations[user] = conv
                try:
                    phones = await choose_phones(conv, user)
                    chat = int(await prompt(conv, "معرف المحادثة التي ستُعالج يدويًا:"))
                    minutes = int(await preset(conv, "مدة إيقاف الرد بالدقائق (1 إلى 10080):", [15, 30, 60, 1440]))
                    if not 1 <= minutes <= 10080:
                        raise ValueError("مدة غير صالحة.")
                    for phone in phones:
                        await query(
                            """INSERT INTO automation_manual(user_id,phone,chat_id,until_at)
                            VALUES(?,?,?,CURRENT_TIMESTAMP+?*INTERVAL '1 minute') ON CONFLICT(user_id,phone,chat_id)
                            DO UPDATE SET until_at=excluded.until_at""",
                            (user, phone, chat, minutes),
                            commit=True,
                        )
                    await conv.send_message("✅ أُوقفت الردود مؤقتًا في المحادثة.", parse_mode=None)
                finally:
                    active_conversations.pop(user, None)
        if action in {"template", "templateedit"}:
            async with event.client.conversation(user) as conv, input_session(conv):
                active_conversations[user] = conv
                try:
                    text = await prompt(conv, "نص القالب:")
                    media, name = None, None
                    if await confirm(conv, "إرفاق صورة أو ملف وسائط بالقالب؟"):
                        await conv.send_message("أرسل صورة أو ملف وسائط بحجم أقصى 10 ميغابايت:")
                        response = await conv.get_response(timeout=300)
                        if not response.file:
                            raise ValueError("أرسل صورة أو ملفًا؛ لم يُحفظ القالب.")
                        if response.file.size > 10 * 1024 * 1024:
                            raise ValueError("الملف أكبر من المسموح.")
                        media = await response.download_media(bytes)
                        if len(media) > 10 * 1024 * 1024:
                            raise ValueError("الملف أكبر من المسموح.")
                        name = __import__("os").path.basename(response.file.name or "image.jpg")
                    if action == "templateedit":
                        row = await query(
                            "UPDATE automation_templates SET text=?,media=?,media_name=? WHERE template_id=? AND user_id=? RETURNING template_id",
                            (text, media, name, key, user),
                            commit=True,
                            fetchone=True,
                        )
                        if not row:
                            raise ValueError("القالب غير موجود.")
                    else:
                        row = await query(
                            "INSERT INTO automation_templates(user_id,text,media,media_name) VALUES(?,?,?,?) RETURNING template_id",
                            (user, text, media, name),
                            commit=True,
                            fetchone=True,
                        )
                    await conv.send_message(
                        f"✅ حُفظ القالب #{row[0]}. استخدم قالب:{row[0]} في الرد أو النشر.", parse_mode=None
                    )
                finally:
                    active_conversations.pop(user, None)
        rules = (
            await query(
                "SELECT rule_id,phone,enabled FROM automation_rules WHERE user_id=? ORDER BY rule_id DESC LIMIT 20",
                (user,),
                fetchall=True,
            )
            or []
        )
        jobs = (
            await query(
                "SELECT job_id,kind,status FROM automation_jobs WHERE user_id=? ORDER BY job_id DESC LIMIT 20",
                (user,),
                fetchall=True,
            )
            or []
        )
        await event.edit(
            "🤖 أتمتة حساباتي\nإدارة الردود والنشر والملف والفحص من حسابك الأساسي.\nكل إعداد جديد يبقى متوقفًا حتى تراجعه وتفعّله.",
            parse_mode=None,
            buttons=[
                [
                    Button.inline("رد تلقائي جديد", data="auto:newrule"),
                    Button.inline("مهمة مجدولة جديدة", data="auto:newjob"),
                ],
                [
                    Button.inline("قوالب النص والوسائط", data="auto:templates"),
                    Button.inline("معالجة محادثة يدويًا", data="auto:manual"),
                ],
            ]
            + [
                [Button.inline(f"رد #{r[0]} · +{r[1]} · {'يعمل' if r[2] else 'متوقف'}", data=f"auto:rule:{r[0]}")]
                for r in rules
            ]
            + [[Button.inline(f"مهمة #{r[0]} · {r[1]} · {r[2]}", data=f"auto:job:{r[0]}")] for r in jobs]
            + [
                [
                    Button.inline("إيقاف جميع أتمتتي", data="auto:stopall"),
                    Button.inline("سجل التنفيذ", data="auto:log"),
                ],
                [Button.inline("رجوع", data="control_menu")],
            ],
        )
    except (ValueError, KeyError, asyncio.TimeoutError) as exc:
        await event.answer(str(exc) or "انتهى وقت الإدخال.", alert=True)
