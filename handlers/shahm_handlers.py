"""Shahm panels using the shared conversation and inline-input interface."""

import asyncio

import account_access
import shahm_service as service
from keyboards import Button
from shahm_engine import BOTS, ShahmError, parse_sequence, profile_for
from workflow import query
from .base import active_conversations
from .input_choices import choose, choose_accounts, confirm, input_session, preset, read


HELP = (
    "شهم · التجميع والتحويل والهدية\n\n"
    "اختر عملية ثم حسابًا مملوكًا وبوتًا من القائمة، أو أدخل معرف بوت مخصص. "
    "يمكن تعديل تسلسل أزرار التجميع؛ يبدأ الترقيم من 0. "
    "استخدم عرض الأزرار لمعرفة الترتيب الحالي.\n\n"
    "التحويل يرسل القيم بالترتيب إلى البوت المختار؛ دعمكم يتطلب قيمتين وبقية البوتات قيمة واحدة. "
    "راجع تعليمات البوت قبل تأكيد القيم. هايبر لا يملك زر تحويل في التعريف المرفق.\n\n"
    "تسلسلات الأزرار قابلة للتغيير لدى البوتات. تُعرض ردودها كما وردت، ولا يُستنتج الرصيد من عدد الاشتراكات. "
    "يمكن إيقاف المهام من هنا أو من مركز المهام. التشغيل المتكرر يتوقف عند إعادة تشغيل المشروع؛ "
    "راجع النتيجة داخل البوت قبل تكرار تحويل أو هدية."
)


async def wizard(event, action):
    user = event.chat_id
    accounts = await account_access.accounts(user, owned_only=True)
    if not accounts:
        raise ShahmError("لا توجد حسابات مملوكة متاحة. أضف حسابك من قسم تنصيب الحسابات.")
    mark = getattr(event, "mark_input_started", None)
    if mark:
        mark()
    async with event.client.conversation(user) as conv, input_session(conv):
        active_conversations[user] = conv
        try:
            if action in {"transfer", "buttons", "press"}:
                phones = [await choose(conv, "اختر الحساب:", [(a[0], "+" + a[0]) for a in accounts])]
            else:
                phones = await choose_accounts(conv, [a[0] for a in accounts])
            name = await choose(conv, "اختر البوت:", [(name, name) for name in BOTS] + [(None, "بوت مخصص")])
            if name is None:
                name = await read(conv, "أدخل معرف البوت مثل @username:")
            profile = profile_for(name)
            options = {}
            if action == "collect":
                if await choose(conv, "تسلسل أزرار التجميع:", [(False, "استخدام التعريف الحالي"), (True, "تعديل التسلسل")]):
                    sequence = parse_sequence(await read(conv, "أدخل تسلسل الأزرار مثل 2,0؛ يبدأ الترقيم من 0:"))
                    profile = profile_for(name, sequence)
                options["limit"] = int(await preset(conv, "الحد الأعلى للمهام في الدورة لكل حساب (1–100):", [10, 25, 50, 100]))
                if await choose(conv, "التشغيل:", [(False, "دورة واحدة"), (True, "تشغيل متكرر")]):
                    options["repeat_seconds"] = int(await preset(conv, "الفاصل بين الدورات بالثواني (60 ثانية على الأقل):", [60, 600, 3600]))
            if action == "transfer":
                if profile.transfer_button is None:
                    raise ShahmError("زر التحويل غير محدد لهذا البوت.")
                count = 2 if profile.username.lower() == BOTS["دعمكم"].username.lower() else 1
                options["values"] = [await read(conv, f"أدخل قيمة التحويل {i + 1} من {count} وفق تعليمات البوت:") for i in range(count)]
            if action == "press":
                options["button"] = int(await read(conv, "أدخل رقم الزر من 0 إلى 80. استخدم عرض الأزرار لمعرفة الترتيب:"))
            service.validate(action, profile, phones, options.get("values", ()), options.get("repeat_seconds"), options.get("limit", 50), options.get("button"))
            preview = (
                f"معاينة شهم · {service.LABELS[action]}\nالبوت: {profile.username}\n"
                f"الحسابات ({len(phones)}): {', '.join('+' + p for p in phones[:20])}{' …' if len(phones) > 20 else ''}\n"
            )
            if action == "collect":
                preview += f"التسلسل: {','.join(map(str, profile.start_buttons))}\nحد المهام: {options['limit']}\n"
                preview += f"فاصل التكرار: {options.get('repeat_seconds') or 'دورة واحدة'}\n"
            if action == "transfer":
                preview += "القيم بالترتيب: " + " | ".join(options["values"]) + "\n"
            if action == "press":
                preview += f"رقم الزر: {options['button']}\n"
            preview += "ستُنفذ العملية داخل الحساب المختار. بدء التنفيذ؟"
            if not await confirm(conv, preview):
                return await conv.send_message("أُلغيت العملية.", parse_mode=None)
            job = await service.start(event.client, user, phones, profile, action, **options)
            await conv.send_message(
                f"بدأت مهمة شهم {job}. ستصلك ردود البوت هنا.", parse_mode=None,
                buttons=[[Button.inline("متابعة المهمة", data=f"ops:jobs:view:{job}")],
                         [Button.inline("إيقاف مهامي في شهم", data="shahm:stop")]],
            )
        except (ValueError, asyncio.TimeoutError) as exc:
            # int() errors may contain user input; keep unexpected parsing failures generic.
            text = str(exc) if isinstance(exc, ShahmError) else "الإدخال غير صالح أو أُلغيت العملية. افتح القسم للمحاولة مجددًا."
            await conv.send_message(text, parse_mode=None)
        finally:
            if active_conversations.get(user) is conv:
                active_conversations.pop(user, None)


async def menu(event, data="shahm:menu"):
    user = event.chat_id
    if getattr(event, "sender_id", user) != user:
        return await event.answer("استخدم المحادثة الخاصة.", alert=True)
    action = data.split(":", 1)[1]
    if action == "stop":
        count = await service.stop_user(user)
        return await event.respond(f"أُوقفت {count} مهمة من مهامك في شهم. راجع البوت المستهدف قبل إعادة أي تحويل أو هدية.", parse_mode=None)
    if not await service.access_active(user):
        return await event.answer("يلزم اشتراك فعال وحساب غير محظور.", alert=True)
    try:
        if action in service.ACTIONS:
            return await wizard(event, action)
        if action == "help":
            return await event.edit(HELP, parse_mode=None, buttons=[[Button.inline("رجوع", data="shahm:menu")]])
        if action == "history":
            rows = await query(
                "SELECT job_id,kind,status FROM workflow_jobs WHERE user_id=? AND kind LIKE 'shahm_%' ORDER BY created_at DESC LIMIT 20",
                (user,), fetchall=True,
            ) or []
            from .platform_handlers import KINDS, STATES
            buttons = [[Button.inline(f"{KINDS.get(kind, kind)} · {STATES.get(status, status)}", data=f"ops:jobs:view:{job}")]
                       for job, kind, status in rows]
            return await event.edit("مهامي في شهم" if rows else "لا توجد مهام مسجلة في شهم.", parse_mode=None,
                                    buttons=buttons + [[Button.inline("رجوع", data="shahm:menu")]])
        if action != "menu":
            raise ShahmError("العملية غير صالحة. افتح قسم شهم مجددًا.")
        return await event.edit(
            "شهم · التجميع والتحويل والهدية\nاختر العملية. يعمل القسم بالحسابات المملوكة لك وجلساتها المحفوظة.",
            parse_mode=None, buttons=[
                [Button.inline("التجميع", data="shahm:collect"), Button.inline("التحويل", data="shahm:transfer")],
                [Button.inline("الهدية", data="shahm:gift"), Button.inline("عرض الأزرار", data="shahm:buttons")],
                [Button.inline("ضغط زر", data="shahm:press"), Button.inline("مهامي", data="shahm:history")],
                [Button.inline("إيقاف مهامي في شهم", data="shahm:stop")],
                [Button.inline("طريقة الاستخدام", data="shahm:help"), Button.inline("رجوع", data="main")],
            ],
        )
    except ShahmError as exc:
        return await event.respond(str(exc), parse_mode=None)
