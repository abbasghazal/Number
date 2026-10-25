"""Account-local implementation of collection, gifts and bot transfers."""

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from telethon import errors, functions, types


class ShahmError(ValueError):
    """A message safe to display to the account owner."""


@dataclass(frozen=True)
class BotProfile:
    username: str
    start_buttons: tuple[int, ...] = (2, 0)
    transfer_button: int | None = 3
    gift_buttons: tuple[int, ...] = (6,)
    verify_texts: tuple[str, ...] = ("تحقق", "اشتركت")


BOTS = {
    "المليار": BotProfile("@eeobot"),
    "الجوكر": BotProfile("@A_MAN9300BOT"),
    "العقاب": BotProfile("@MARKTEBOT"),
    "العرب": BotProfile("@xnsex21bot"),
    "برليون": BotProfile("@Burllionbot"),
    "اسيا": BotProfile("@cn2bot"),
    "هايبر": BotProfile("@ihyberbot", (0, 0), None),
    "السلطان": BotProfile("@YY8BoT"),
    "دعمكم": BotProfile("@DamKombot", (1, 0), 4, (2, 1), ("اشتركت", "تحقق")),
}
ALIASES = {"آسيا": "اسيا", "اسيـا": "اسيا", "دعم": "دعمكم"}
DONE_PHRASES = (
    "لا يوجد قنوات", "لا توجد قنوات", "لا يوجد مهام", "لا توجد مهام",
    "انتهت القنوات", "نفدت القنوات",
)
VERIFY_SUCCESS = (
    "تم التحقق", "تم إضافة", "تم اضافة", "تم منح", "تم احتساب",
    "حصلت على", "تم الاشتراك بنجاح", "تم قبول",
)
VERIFY_FAILURE = ("لم تشترك", "غير مشترك", "لم يتم", "فشل", "غير مستوف")


def profile_for(name, sequence=None):
    lookup = ALIASES.get(name.strip(), name.strip())
    base = BOTS.get(lookup)
    if base is None:
        base = next((p for p in BOTS.values() if lookup.lstrip("@").lower() == p.username[1:].lower()), None)
    if base is None:
        if not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{4,31}", lookup):
            raise ShahmError("اسم البوت غير صحيح. اختر من القائمة أو أدخل @username.")
        base = BotProfile("@" + lookup.lstrip("@"))
    if sequence is None:
        return base
    sequence = parse_sequence(",".join(str(i) for i in sequence))
    return BotProfile(base.username, sequence, base.transfer_button, base.gift_buttons, base.verify_texts)


def parse_sequence(raw):
    parts = raw.replace("،", ",").split(",")
    if not 1 <= len(parts) <= 12 or any(not re.fullmatch(r"[0-9]{1,2}", p.strip()) for p in parts):
        raise ShahmError("أدخل تسلسلًا مثل 2,0 بحد أقصى 12 ضغطة؛ يبدأ الترقيم من 0.")
    values = tuple(int(p.strip()) for p in parts)
    if any(i > 80 for i in values):
        raise ShahmError("رقم الزر يجب أن يكون بين 0 و80.")
    return values


def transfer_values(profile, values):
    expected = 2 if profile.username.lower() == BOTS["دعمكم"].username.lower() else 1
    if profile.transfer_button is None:
        raise ShahmError("زر التحويل غير محدد لهذا البوت.")
    if len(values) != expected or any(not isinstance(v, str) or not v.strip() or len(v) > 200 or "\n" in v for v in values):
        raise ShahmError(f"هذا البوت يتطلب {expected} قيمة للتحويل، كل قيمة في حقل مستقل.")
    return tuple(v.strip() for v in values)


def flattened_buttons(msg):
    return [(r, c, b) for r, row in enumerate(getattr(msg, "buttons", None) or []) for c, b in enumerate(row)]


def signature(msg):
    if msg is None:
        return None
    return (msg.id, str(getattr(msg, "edit_date", None)), msg.raw_text or "",
            tuple((getattr(b, "text", ""), getattr(b, "url", None), getattr(b, "data", None))
                  for _, _, b in flattened_buttons(msg)))


def parse_telegram_link(url):
    if not isinstance(url, str):
        return None
    url = url.strip()
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", url):
        return "public", url[1:]
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        if parsed.hostname not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
            return None
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443) or parsed.query or parsed.fragment:
            return None
    except ValueError:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) == 1 and re.fullmatch(r"\+[A-Za-z0-9_-]{8,}", parts[0]):
        return "private", parts[0][1:]
    if len(parts) == 2 and parts[0] == "joinchat" and re.fullmatch(r"[A-Za-z0-9_-]{8,}", parts[1]):
        return "private", parts[1]
    if len(parts) == 1 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", parts[0]):
        if parts[0].lower() not in {"share", "proxy", "socks", "login", "joinchat", "addstickers", "addemoji", "invoice"}:
            return "public", parts[0]
    return None


def find_channel_target(msg):
    for _, _, button in flattened_buttons(msg):
        target = parse_telegram_link(getattr(button, "url", None))
        if target:
            return target
    text = msg.raw_text or ""
    if not any(word in text for word in ("اشترك", "انضم", "القناة", "المجموعة")):
        return None
    for entity in getattr(msg, "entities", None) or []:
        target = parse_telegram_link(getattr(entity, "url", None))
        if target:
            return target
    for match in re.finditer(r"https?://[^\s<>]+|(?<!\w)@[A-Za-z][A-Za-z0-9_]{4,31}", text):
        target = parse_telegram_link(match.group().rstrip(".,،)"))
        if target:
            return target
    return None


def verified_status(text):
    if any(s in text for s in VERIFY_FAILURE):
        return "failed"
    return "confirmed" if any(s in text for s in VERIFY_SUCCESS) else "unknown"


def normalized_text(text):
    text = str(text or "").translate(str.maketrans("أإآى", "اااي"))
    return " ".join("".join(c for c in text if not unicodedata.combining(c) and c != "ـ").split())


def subscription_gate(msg):
    """Distinguish a mandatory subscription panel from a normal collection task."""
    text = normalized_text(getattr(msg, "raw_text", ""))
    captions = [normalized_text(b.text) for _, _, b in flattened_buttons(msg)]
    return (
        any(s in text for s in ("الاشتراك الاجباري", "اشتراك اجباري", "الاشتراك في قنوات البوت",
                                "الاشتراك بقنوات البوت", "الاشتراك في القنوات التالية"))
        or (any(s in text for s in ("اشترك", "الاشتراك"))
            and ("اولا" in text or "اول " in text)
            and any(s in text for s in ("القنوات", "قنوات", "القناة")))
        or sum("لست مشترك" in caption for caption in captions) > 1
    )


def subscription_targets(msg, bot_username):
    targets, seen = [], set()
    for _, _, button in flattened_buttons(msg):
        target = parse_telegram_link(getattr(button, "url", None))
        if target is None:
            continue
        kind, value = target
        key = (kind, value.lower() if kind == "public" else value)
        if kind == "public" and value.lower() == bot_username.lstrip("@").lower():
            continue
        if key not in seen:
            targets.append(target)
            seen.add(key)
    return targets


def verification_start(url, bot_username):
    """Accept verification deep links addressed only to the selected bot."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.fragment or parsed.username or parsed.password:
            return None
        if parsed.scheme in {"http", "https"}:
            if parsed.hostname not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"} or parsed.port not in (None, 80, 443):
                return None
            username = parsed.path.strip("/")
        elif parsed.scheme == "tg" and parsed.netloc == "resolve" and not parsed.path:
            domains = query.pop("domain", [])
            if len(domains) != 1:
                return None
            username = domains[0]
        else:
            return None
        if username.lower() != bot_username.lstrip("@").lower() or set(query) - {"start"}:
            return None
        if not query:
            return "/start"
        values = query.get("start", [])
        if len(values) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", values[0]):
            return "/start " + values[0]
    except ValueError:
        pass
    return None


class BotSession:
    def __init__(self, client, profile, checkpoint, pause, *, timeout=15, delay=4):
        self.client, self.profile = client, profile
        self.checkpoint, self.pause = checkpoint, pause
        self.timeout, self.delay = timeout, delay
        self.peer = None

    async def prepare(self):
        await self.checkpoint()
        peer = await self.client.get_entity(self.profile.username)
        if not isinstance(peer, types.User) or not peer.bot:
            raise ShahmError("المعرف المحدد ليس بوت تيليجرام.")
        self.peer = peer

    async def latest(self):
        await self.checkpoint()
        messages = await self.client.get_messages(self.peer, limit=12)
        return next((m for m in messages if not m.out and (m.raw_text or m.buttons)), None)

    async def await_update(self, previous):
        old = signature(previous)
        deadline = asyncio.get_running_loop().time() + self.timeout
        while asyncio.get_running_loop().time() < deadline:
            msg = await self.latest()
            if msg and (previous is None or msg.id >= previous.id) and signature(msg) != old:
                return msg
            await self.pause(.8)
        return None

    async def request_start(self, *, allow_existing_menu=False):
        old = await self.latest()
        await self.checkpoint()
        await self.client.send_message(self.peer, "/start", parse_mode=None)
        msg = await self.await_update(old)
        if msg is None:
            if old is not None and subscription_gate(old):
                return old
            if allow_existing_menu and old is not None and old.buttons:
                return old
            raise ShahmError("لم تصل قائمة جديدة من البوت بعد /start. افتحه يدويًا ثم حاول مجددًا.")
        return msg

    async def start(self):
        msg, _ = await self.ensure_subscriptions(await self.request_start())
        return msg

    async def ensure_subscriptions(self, msg):
        recovered = False
        joined = set()
        for _ in range(3):
            if not subscription_gate(msg):
                return msg, recovered
            recovered = True
            targets = subscription_targets(msg, self.profile.username)
            if not targets:
                raise ShahmError("ظهرت قائمة اشتراك إجباري دون روابط قنوات صالحة. راجع البوت يدويًا.")
            for target in targets:
                key = (target[0], target[1].lower() if target[0] == "public" else target[1])
                if key in joined:
                    continue
                try:
                    state = await self.join(target)
                except errors.FloodWaitError:
                    raise
                except errors.RPCError:
                    raise ShahmError("تعذر الانضمام إلى إحدى قنوات الاشتراك الإجباري. راجع صلاحية رابطها داخل البوت.") from None
                if state == "requested":
                    raise ShahmError("إحدى قنوات الاشتراك الإجباري تتطلب موافقة الإدارة. انتظر قبول طلب الانضمام ثم أعد المحاولة.")
                joined.add(key)
                await self.pause(self.delay)
            fresh = None
            for index, (_, _, button) in enumerate(flattened_buttons(msg)):
                caption = normalized_text(button.text)
                if not any(word in caption for word in ("تحقق", "اشتركت", "انضممت")):
                    continue
                url = getattr(button, "url", None)
                if url:
                    command = verification_start(url, self.profile.username)
                    if command is None:
                        continue
                    await self.checkpoint()
                    await self.client.send_message(self.peer, command, parse_mode=None)
                    fresh = await self.await_update(msg)
                else:
                    raw = getattr(button, "button", None)
                    if not isinstance(raw, (types.KeyboardButtonCallback, types.KeyboardButton)):
                        continue
                    fresh, _ = await self.click(msg, index)
                break
            else:
                raise ShahmError("لم أجد زر تحقق صالحًا في قائمة الاشتراك الإجباري. راجع البوت يدويًا.")
            await self.pause(self.delay)
            # Restart menu navigation only; transfer payloads are never resent here.
            msg = fresh if fresh is not None and subscription_gate(fresh) else await self.request_start(allow_existing_menu=True)
        if subscription_gate(msg):
            raise ShahmError("لم يؤكد البوت اكتمال الاشتراك بعد ثلاث محاولات تحقق. راجع قائمة القنوات داخله ثم أعد المحاولة.")
        return msg, recovered

    async def click(self, msg, index):
        buttons = flattened_buttons(msg)
        if not 0 <= index < len(buttons):
            raise ShahmError(f"الزر {index} غير موجود؛ المتاح {len(buttons)} زرًا. اعرض الأزرار لتحديث التسلسل.")
        r, c, button = buttons[index]
        raw = getattr(button, "button", None)
        if not isinstance(raw, (types.KeyboardButtonCallback, types.KeyboardButton)) or getattr(raw, "requires_password", False):
            raise ShahmError("اختر زر قائمة أو Callback عاديًا.")
        await self.checkpoint()
        result = await msg.click(r, c)
        reply = result.message if isinstance(result, types.messages.BotCallbackAnswer) else ""
        return await self.await_update(msg), reply or ""

    async def sequence(self, msg, values):
        msg, _ = await self.ensure_subscriptions(msg)
        position = restarts = 0
        while position < len(values):
            fresh, reply = await self.click(msg, values[position])
            if fresh is None:
                raise ShahmError("لم تتغير قائمة البوت بعد الضغط. راجع الأزرار قبل إعادة المحاولة.")
            msg, recovered = await self.ensure_subscriptions(fresh)
            if recovered:
                restarts += 1
                if restarts > 3:
                    raise ShahmError("تكرر طلب الاشتراك أثناء فتح القائمة. راجع البوت قبل إعادة المحاولة.")
                position = 0
            else:
                position += 1
            await self.pause(self.delay)
        return msg

    async def named(self, msg, names):
        for i, (_, _, button) in enumerate(flattened_buttons(msg)):
            if not getattr(button, "url", None) and any(n in (button.text or "") for n in names):
                return await self.click(msg, i)
        raise ShahmError("زر التحقق غير موجود. راجع الأزرار في البوت.")

    async def join(self, target):
        kind, value = target
        await self.checkpoint()
        try:
            if kind == "private":
                await self.client(functions.messages.ImportChatInviteRequest(value))
            else:
                entity = await self.client.get_entity(value)
                if not isinstance(entity, types.Channel):
                    raise ShahmError("رابط المهمة لا يشير إلى قناة أو مجموعة.")
                await self.checkpoint()
                await self.client(functions.channels.JoinChannelRequest(entity))
            return "joined"
        except errors.UserAlreadyParticipantError:
            return "already"
        except errors.InviteRequestSentError:
            return "requested"

    async def collect(self, limit=50):
        stats = {"attempts": 0, "joined": 0, "verified": 0, "skipped": 0}
        reason = "بلغت الدورة حد المهام المحدد."
        msg = await self.sequence(await self.start(), self.profile.start_buttons)
        seen = set()
        for _ in range(limit):
            await self.checkpoint()
            msg, recovered = await self.ensure_subscriptions(msg)
            if recovered:
                msg = await self.sequence(msg, self.profile.start_buttons)
            if any(s in (msg.raw_text or "") for s in DONE_PHRASES):
                reason = "لا توجد مهام متاحة حاليًا."
                break
            target = find_channel_target(msg)
            if not target:
                reason = "توقفت الدورة عند مهمة غير معروفة؛ راجع البوت يدويًا."
                break
            key = (target[0], target[1].lower() if target[0] == "public" else target[1])
            if key in seen:
                reason = "توقفت الدورة لأن البوت يعرض المهمة نفسها."
                break
            seen.add(key)
            stats["attempts"] += 1
            try:
                state = await self.join(target)
                if state == "requested":
                    stats["skipped"] += 1
                    reason = "طلب الانضمام ينتظر موافقة القناة."
                    break
                stats["joined"] += state == "joined"
                await self.pause(self.delay)
                fresh, reply = await self.named(msg, self.profile.verify_texts)
                if fresh is not None and subscription_gate(fresh):
                    menu, _ = await self.ensure_subscriptions(fresh)
                    msg = await self.sequence(menu, self.profile.start_buttons)
                    seen.discard(key)
                    continue
                text = reply + "\n" + ((fresh.raw_text or "") if fresh else "")
                stats["verified"] += verified_status(text) == "confirmed"
                if fresh is None:
                    reason = "وصل رد التحقق دون مهمة جديدة."
                    break
                msg = fresh
            except ShahmError as exc:
                stats["skipped"] += 1
                reason = str(exc)
                break
            await self.pause(self.delay)
        return stats, reason

    async def transfer(self, values):
        values = transfer_values(self.profile, values)
        msg = await self.sequence(await self.start(), (self.profile.transfer_button,))
        for value in values:
            await self.checkpoint()
            await self.client.send_message(self.peer, value, parse_mode=None)
            msg = await self.await_update(msg)
            if msg is None:
                raise ShahmError("لم يؤكد البوت استلام بيانات التحويل؛ تحقق يدويًا قبل إعادة المحاولة.")
            if subscription_gate(msg):
                await self.ensure_subscriptions(msg)
                raise ShahmError("اكتمل التعامل مع طلب الاشتراك أثناء إدخال بيانات التحويل. راجع حالة التحويل داخل البوت قبل إرساله مجددًا.")
            await self.pause(self.delay)
        return "استجابة البوت لطلب التحويل؛ راجع النتيجة داخل البوت:\n" + (msg.raw_text or "[رسالة دون نص]")[:1500]

    async def gift(self):
        msg = await self.start()
        reply = ""
        i = restarts = 0
        while i < len(self.profile.gift_buttons):
            fresh, reply = await self.click(msg, self.profile.gift_buttons[i])
            if fresh is None:
                if reply and i == len(self.profile.gift_buttons) - 1:
                    break
                raise ShahmError("لم يصل رد جديد على طلب الهدية. راجع البوت.")
            msg, recovered = await self.ensure_subscriptions(fresh)
            if recovered:
                restarts += 1
                if restarts > 3:
                    raise ShahmError("تكرر طلب الاشتراك أثناء طلب الهدية. راجع البوت قبل إعادة المحاولة.")
                i = 0
            else:
                i += 1
            await self.pause(self.delay)
        return "رد البوت على طلب الهدية:\n" + (reply or msg.raw_text or "[رسالة دون نص]")[:1500]

    async def buttons(self):
        msg = await self.latest()
        if msg is None or not msg.buttons:
            msg = await self.start()
        else:
            msg, _ = await self.ensure_subscriptions(msg)
        lines = [f"أزرار {self.profile.username} (تبدأ من 0):"]
        for i, (_, _, b) in enumerate(flattened_buttons(msg)):
            lines.append(f"{i}: {b.text}{' [رابط]' if getattr(b, 'url', None) else ''}")
        return "\n".join(lines)[:3000]

    async def redeem_code(self, code, before_send, button_text="استخدام كود"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code):
            raise ShahmError("صيغة الكود غير صالحة.")
        msg = await self.start()
        names = tuple(normalized_text(s) for s in (button_text, "استخدام كود", "استخدام الكود"))
        for _ in range(3):
            index = next((i for i, (_, _, b) in enumerate(flattened_buttons(msg))
                          if not getattr(b, 'url', None) and any(n in normalized_text(b.text) for n in names)), None)
            if index is None:
                raise ShahmError("زر استخدام الكود غير موجود في قائمة البوت.")
            fresh, _ = await self.click(msg, index)
            if fresh is None:
                raise ShahmError("لم تصل رسالة طلب إدخال الكود.")
            msg, recovered = await self.ensure_subscriptions(fresh)
            if recovered:
                continue
            await self.checkpoint()
            # Persist this boundary before the remote call, so a restart never replays it.
            await before_send()
            await self.client.send_message(self.peer, code, parse_mode=None)
            reply = await self.await_update(msg)
            if reply is None:
                return 'unknown', "أُرسل الكود دون وصول رد مؤكد. راجع البوت المستهدف."
            text = reply.raw_text or ''
            if subscription_gate(reply):
                return 'unknown', "ظهر طلب اشتراك بعد إرسال الكود؛ لم يُعد إرسال الكود تلقائيًا."
            normalized = normalized_text(text)
            failed = any(s in normalized for s in ("غير صالح", "انتهي", "انتهت", "مستخدم", "لم يتم", "لا يوجد", "خطا")) or (
                'تم استخدام' in normalized and any(s in normalized for s in ('مسبقا','سابقا','بالفعل'))
            )
            confirmed = not failed and (verified_status(text) == 'confirmed' or any(s in normalized for s in ('تم استخدام الكود بنجاح','تم شحن','تم تفعيل الكود')))
            return 'confirmed' if confirmed else 'failed' if failed else 'unknown', text[:1200] or '[رسالة دون نص]'
        raise ShahmError("تكرر طلب الاشتراك أثناء فتح استخدام الكود.")

    async def press(self, index):
        msg = await self.latest()
        if msg is None:
            msg = await self.start()
        else:
            msg, _ = await self.ensure_subscriptions(msg)
        for _ in range(3):
            fresh, reply = await self.click(msg, index)
            if fresh is None and not reply:
                raise ShahmError("لم يصل رد جديد بعد الضغط؛ راجع البوت قبل إعادة المحاولة.")
            if fresh is not None and subscription_gate(fresh):
                msg, _ = await self.ensure_subscriptions(fresh)
                continue
            return "رد البوت بعد الضغط:\n" + (reply or fresh.raw_text or "[رسالة دون نص]")[:1500]
        raise ShahmError("تكرر طلب الاشتراك أثناء الضغط. راجع البوت قبل إعادة المحاولة.")
