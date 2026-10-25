"""A server-owned conversation transport for existing bot handlers, without duplicating business logic."""

import asyncio
import contextlib
import re
import secrets
import time

from telethon import errors

from workflow import query
from web_auth import digest
from service_catalog import ROOTS

SENSITIVE = (
    "num:new_sale",
    "rig_",
    "add_template",
    "edit_template",
    "sell",
    "submit_sale",
    "num:offer:",
    "add_auction",
    "stars:buy:",
    "pay:stars",
    "get_code:",
    "ops:twofa:reveal:",
    "ops:twofa:confirm:",
    "extract_session",
    "new_session_",
    "install_session",
    "install_sessions",
    "new_session",
    "create_session",
    "get_verification_code",
    "change_profile_photo",
    "auto:template",
    "save_post",
    "store_existing",
    "zip_",
    "daily_backup",
    "ops:backup:",
    "license_export",
    "sell_account",
    "num:sell",
    "install_account",
    "install_phone",
    "ops:restore:",
    "change_2fa",
    "set_2fa",
    "twofa_password",
    "ops:twofa:set:",
    "v:",
)
_sessions = {}


async def handoff(user, payload, client):
    token = secrets.token_urlsafe(24)
    await query(
        "INSERT INTO platform_handoffs(token_hash,user_id,payload,expires_at) VALUES(?,?,?,CURRENT_TIMESTAMP+INTERVAL '10 minutes')",
        (digest(token), user, payload),
        commit=True,
    )
    username = getattr(client, "_miniapp_username", None)
    if not username:
        username = (await client.get_me()).username
    return f"https://t.me/{username}?start=web_{token}"


class Message:
    def __init__(self, session, text="", buttons=None, identity=None):
        self.session = session
        self.id = identity or session.next_id()
        self.raw_text = self.text = self.message = text or ""
        self.buttons = buttons
        self.media = self.file = self.document = self.photo = None
        self.out = True

    async def edit(self, text=None, **kwargs):
        return await self.session.show(self.text if text is None else text, kwargs.get("buttons"), self.id)

    async def delete(self):
        self.session.messages = [m for m in self.session.messages if m["id"] != self.id]

    async def download_media(self, *args, **kwargs):
        raise ValueError("تُستكمل عمليات ملفات الحسابات داخل تيليجرام.")


class Conversation:
    def __init__(self, client):
        self._client = client
        self.chat_id = client.session.user
        self.queue = asyncio.Queue(maxsize=1)
        self._pending_responses = {}
        self.future = None

    async def __aenter__(self):
        from handlers.base import active_conversations

        if self.chat_id in active_conversations:
            raise errors.AlreadyInConversationError()
        self._client.session.conversation = self
        return self

    async def __aexit__(self, *args):
        from handlers.base import active_conversations

        if active_conversations.get(self.chat_id) is self:
            active_conversations.pop(self.chat_id, None)
        if self._client.session.conversation is self:
            self._client.session.conversation = None
            self._client.session.prompt = None

    async def send_message(self, *args, **kwargs):
        return await self._client.send_message(self.chat_id, *args, **kwargs)

    async def send_file(self, *args, **kwargs):
        return await self._client.send_file(self.chat_id, *args, **kwargs)

    async def get_response(self, message=None, timeout=300):
        self._client.session.prompt = secrets.token_urlsafe(16)
        self._client.session.changed.set()
        self.future = asyncio.create_task(self.queue.get())
        try:
            return await asyncio.wait_for(self.future, timeout)
        finally:
            self._client.session.prompt = None
            self.future = None

    def cancel(self):
        if self.future:
            self.future.cancel()

    def cancel_all(self):
        self.cancel()


class Client:
    def __init__(self, session, real):
        self.session = session
        self.real = real

    def __getattr__(self, name):
        return getattr(self.real, name)

    async def __call__(self, *args, **kwargs):
        return await self.real(*args, **kwargs)

    def conversation(self, user, *args, **kwargs):
        if int(user) != self.session.user:
            raise PermissionError("لا يمكن فتح محادثة لمستخدم آخر.")
        return Conversation(self)

    async def send_message(self, peer, message=None, *args, **kwargs):
        if peer == self.session.user or getattr(peer, "user_id", None) == self.session.user:
            if isinstance(message, Message):
                message = message.text
            return await self.session.show(str(message or ""), kwargs.get("buttons"))
        return await self.real.send_message(peer, message, *args, **kwargs)

    async def edit_message(self, peer, message, text=None, *args, **kwargs):
        if peer == self.session.user:
            identity = getattr(message, "id", message)
            previous = next((m["text"] for m in self.session.messages if m["id"] == identity), "")
            return await self.session.show(previous if text is None else text, kwargs.get("buttons"), identity)
        return await self.real.edit_message(peer, message, text, *args, **kwargs)

    async def send_file(self, peer, file, *args, **kwargs):
        result = await self.real.send_file(peer, file, *args, **kwargs)
        if peer == self.session.user:
            await self.session.show("أُرسل الملف إلى محادثتك الخاصة مع البوت في تيليجرام.")
        return result

    async def delete_messages(self, peer, ids, *args, **kwargs):
        if peer == self.session.user:
            values = ids if isinstance(ids, (list, tuple)) else [ids]
            self.session.messages = [m for m in self.session.messages if m["id"] not in values]
            return None
        return await self.real.delete_messages(peer, ids, *args, **kwargs)


class Event:
    def __init__(self, session, payload, message_id=None):
        self.chat_id = self.sender_id = session.user
        self.client = session.client
        self.data = payload.encode()
        self.message_id = message_id or session.counter
        self.is_private = True
        self.is_group = self.is_channel = False
        self.raw_text = ""
        self._answered = False
        self.session = session
        self.message = Message(session, identity=self.message_id)

    async def edit(self, *args, **kwargs):
        return await self.client.edit_message(self.chat_id, self.message_id, *args, **kwargs)

    async def respond(self, *args, **kwargs):
        return await self.client.send_message(self.chat_id, *args, **kwargs)

    async def reply(self, *args, **kwargs):
        return await self.respond(*args, **kwargs)

    async def answer(self, text=None, **kwargs):
        self._answered = True
        if text:
            return await self.respond(text)

    async def get_message(self):
        return Message(self.session, identity=self.message_id)

    async def get_sender(self):
        return await self.client.real.get_entity(self.sender_id)

    async def get_chat(self):
        return await self.get_sender()


class Session:
    def __init__(self, user, real, transport="web"):
        self.user = user
        self.transport = transport
        self.allow_sensitive = transport == "api"
        self.client = Client(self, real)
        self.counter = 0
        self.messages = []
        self.issued = {}
        self.changed = asyncio.Event()
        self.task = None
        self.tasks = set()
        self.section = None
        self.conversation = None
        self.prompt = None
        self.touched = time.monotonic()

    def next_id(self):
        self.counter += 1
        return self.counter

    async def show(self, text, buttons=None, identity=None):
        from keyboards import ensure_button_colors
        buttons=ensure_button_colors(buttons)
        # The normal Mini App transport redacts credentials.  The paid API has
        # an isolated server-to-server console namespace, so authenticated API
        # clients can complete the same sensitive workflows without weakening
        # the browser console.
        text = str(text)
        if not self.allow_sensitive:
            text = re.sub(
                r"(?<![A-Za-z0-9_])1[A-Za-z0-9_-]{340,}(?![A-Za-z0-9_-])", "[بيانات خاصة تُعرض في تيليجرام]", text
            )
            text = re.sub(r"\d{7,12}:[A-Za-z0-9_-]{30,}", "[بيانات خاصة]", text)
            text = re.sub(
                r"(?im)((?:كلمة (?:المرور|التحقق)|password|two.?fa)\s*[:：])[^\n]*", r"\1 [تُعرض في تيليجرام]", text
            )
        identity = identity or self.next_id()
        rows = []
        for row in buttons or []:
            row = row if isinstance(row, (list, tuple)) else [row]
            result = []
            for wrapped in row:
                raw = getattr(wrapped, "button", wrapped)
                if not hasattr(raw, "text"):
                    continue
                style = getattr(raw, "style", None)
                color = next(
                    (c for c in ("success", "danger", "primary") if style and getattr(style, "bg_" + c, False)),
                    "primary",
                )
                if getattr(raw, "data", None):
                    token = secrets.token_urlsafe(18)
                    self.issued[token] = (bytes(raw.data).decode(), identity)
                    result.append(dict(text=raw.text, token=token, style=color))
                elif getattr(raw, "url", None) and str(raw.url).startswith(("https://", "tg://")):
                    result.append(dict(text=raw.text, url=raw.url, style=color))
            if result:
                rows.append(result)
        entry = dict(id=identity, text=text[:16000], buttons=rows)
        self.messages = [m for m in self.messages if m["id"] != identity][-19:] + [entry]
        if len(self.issued) > 1000:
            keep = {b["token"] for m in self.messages for r in m["buttons"] for b in r if "token" in b}
            self.issued = {k: v for k, v in self.issued.items() if k in keep}
        self.changed.set()
        return Message(self, text, buttons, identity)

    def snapshot(self):
        self.touched = time.monotonic()
        return dict(
            messages=self.messages,
            prompt=self.prompt,
            busy=any(not task.done() for task in self.tasks),
            section=self.section,
        )


def get(user, client, transport="web"):
    if transport not in {"web", "api"}:
        raise ValueError("مسار الواجهة غير صالح.")
    key = (user, transport)
    session = _sessions.get(key)
    if session is None:
        if len(_sessions) >= 2048:
            expired = [key for key, s in _sessions.items() if not any(not task.done() for task in s.tasks)]
            if not expired:
                raise ValueError("كل مسارات العمل مشغولة؛ حاول بعد قليل.")
            _sessions.pop(min(expired, key=lambda key: _sessions[key].touched))
        session = _sessions[key] = Session(user, client, transport)
    return session


async def start(user, client, section, transport="web"):
    if section not in ROOTS:
        raise ValueError("القسم غير متاح.")
    from service_catalog import require
    await require(user,section)
    session = get(user, client, transport)
    if any(not task.done() for task in session.tasks):
        return session.snapshot()
    payload = ROOTS[section]
    if section == "import" and not session.allow_sensitive:
        return dict(
            handoff=await handoff(user, payload, client),
            reason="يُستكمل إدخال بيانات الجلسات بأمان في محادثة تيليجرام الخاصة.",
        )
    session.messages = []
    session.issued = {}
    session.section = section
    return await launch(session, payload)


async def launch(session, payload, message_id=None):
    if payload.startswith(SENSITIVE) and not session.allow_sensitive:
        return dict(
            handoff=await handoff(session.user, payload, session.client.real),
            reason="تُستكمل هذه العملية الخاصة في تيليجرام لحماية الجلسات وبيانات الحساب.",
        )
    from handlers.callback_router import callback_handler

    session.changed.clear()

    async def run():
        try:
            await callback_handler(Event(session, payload, message_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            await session.show("تعذر إكمال العملية الآن. يمكنك العودة للقائمة والمحاولة مجددًا.")
        finally:
            session.changed.set()

    # Inline callbacks must be allowed to resolve the running wizard, not replace its task.
    task = asyncio.create_task(run(), name=f"web-console-{session.user}")
    session.tasks.add(task)
    task.add_done_callback(session.tasks.discard)
    if not session.task or session.task.done():
        session.task = task
    await asyncio.sleep(0)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(session.changed.wait(), 0.7)
    return session.snapshot()


async def click(user, client, token, transport="web"):
    session = get(user, client, transport)
    item = session.issued.pop(str(token), None)
    if not item:
        raise ValueError("انتهت صلاحية الزر أو استُخدم. استخدم القائمة الحالية.")
    return await launch(session, *item)


async def answer(user, client, prompt, text, transport="web"):
    session = get(user, client, transport)
    if (
        not prompt
        or prompt != session.prompt
        or not session.conversation
        or not isinstance(text, str)
        or not 1 <= len(text.strip()) <= 4096
    ):
        raise ValueError("انتهت صلاحية حقل الإدخال؛ استخدم الخطوة الحالية.")
    session.prompt = None
    await session.conversation.queue.put(Message(session, text.strip()))
    await asyncio.sleep(0)
    return session.snapshot()


async def stop():
    tasks = [task for s in _sessions.values() for task in s.tasks if not task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _sessions.clear()


async def resume_handoff(event, argument):
    if not argument.startswith("web_"):
        return False
    token = argument[4:]
    row = await query(
        "UPDATE platform_handoffs SET consumed=TRUE WHERE token_hash=? AND user_id=? AND NOT consumed AND expires_at>CURRENT_TIMESTAMP RETURNING payload",
        (digest(token), event.chat_id),
        commit=True,
        fetchone=True,
    )
    if not row:
        await event.respond("انتهى رابط الإكمال أو لا ينتمي لحسابك. افتح العملية من الموقع مجددًا.")
        return True
    from handlers.registry import _TextMenuEvent
    from handlers.callback_router import callback_handler

    class Handoff(_TextMenuEvent):
        data = row[0].encode()

    await callback_handler(Handoff(event))
    return True
