"""Button-driven conversation choices, with validated typed alternatives."""

import asyncio
import re
import time
import unicodedata
from contextlib import asynccontextmanager

from keyboards import remove_plain_button_emojis
from .inline_input import ask


CANCEL = "إلغاء العملية"
NEXT = "الصفحة التالية"
PREVIOUS = "الصفحة السابقة"
COUNTRY_INPUTS = {}


def normalize(text):
    text = remove_plain_button_emojis(unicodedata.normalize("NFKC", str(text)))
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ـ": ""}))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).casefold().split())


@asynccontextmanager
async def input_session(conv):
    """Keep the shared wizard interface; each inline prompt cleans up itself."""
    yield conv


async def read(conv, message, labels=()):
    while True:
        try:
            response = await ask(conv, message, labels)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            raise ValueError("أُلغيت العملية.") from None
        text = (response.raw_text or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        if normalize(text) in {normalize(v) for v in (CANCEL, "إلغاء", "الغاء", "رجوع")} or command in {"/cancel", "/start"}:
            raise ValueError("أُلغيت العملية.")
        if text and not response.file:
            return text
        message = "أرسل الرد كنص أو اختر زرًا."


async def choose(conv, message, options, *, custom=False, page_size=16):
    """Return a stored value; captions and legacy numeric choices remain accepted."""
    options = list(options)
    if not options:
        raise ValueError("لا توجد اختيارات متاحة.")
    page = 0
    error = ""
    while True:
        visible = options[page * page_size:(page + 1) * page_size]
        labels = [label for _, label in visible]
        if page:
            labels.append(PREVIOUS)
        if (page + 1) * page_size < len(options):
            labels.append(NEXT)
        text = await read(conv, error + message, labels)
        if text == NEXT and (page + 1) * page_size < len(options):
            page += 1
            continue
        if text == PREVIOUS and page:
            page -= 1
            continue
        matches = [value for value, label in options if normalize(text) == normalize(label)]
        if len(matches) == 1:
            return matches[0]
        if custom:
            return text
        if text.isdecimal() and 1 <= int(text) <= len(options):
            return options[int(text) - 1][0]
        error = "اختيار غير صالح. اختر من الأزرار.\n"


async def confirm(conv, message):
    return await choose(conv, message, [(True, "تأكيد"), (False, "إلغاء")])


async def preset(conv, message, values):
    value = await choose(conv, message, [(str(v), str(v)) for v in values] + [(None, "إدخال قيمة أخرى")])
    return await read(conv, message) if value is None else value


def match_countries(text, rows):
    """Match names before dial codes; ambiguous matches must be resolved by the user."""
    wanted = normalize(text)
    if not wanted:
        return []
    exact = [r for r in rows if wanted == normalize(r[1]) or wanted == normalize(country_label(r))]
    if exact:
        return exact
    if re.fullmatch(r"\+?[0-9٠-٩۰-۹]+", wanted):
        code = str(int(wanted.lstrip("+")))
        return [r for r in rows if len(r) > 2 and r[2] is not None and str(r[2]).lstrip("+") == code]
    return [r for r in rows if wanted in normalize(r[1])]


def country_label(row):
    return f"{row[1]} (+{str(row[2]).lstrip('+')})" if len(row) > 2 and row[2] is not None else str(row[1])


async def choose_country(conv, rows):
    rows = list(rows)
    if not rows:
        raise ValueError("لا توجد دول متاحة.")
    candidates = rows
    while True:
        labels = [country_label(r) for r in candidates]
        duplicates = {normalize(label) for label in labels if sum(normalize(other) == normalize(label) for other in labels) > 1}
        text = await choose(
            conv, "🌍 اختر الدولة من الأزرار أو اكتب اسمها:",
            [(r[0], label + (f" · #{r[0]}" if normalize(label) in duplicates else "")) for r, label in zip(candidates, labels)],
            custom=True,
        )
        if not isinstance(text, str):
            return next(r for r in candidates if r[0] == text)
        matches = match_countries(text, rows)
        if len(matches) == 1:
            return matches[0]
        candidates = matches or rows
        await conv.send_message(
            "توجد أكثر من دولة مطابقة. اختر الاسم الكامل من الأزرار." if matches else "لم أجد هذه الدولة. اكتب اسمها كما يظهر في القائمة.",
            parse_mode=None,
        )


async def choose_accounts(conv, phones, *, groups=None, allow_count=False):
    """Toggle only accounts supplied by the caller's authorized account query."""
    phones = list(dict.fromkeys(str(p).lstrip("+") for p in phones))
    if not phones:
        raise ValueError("لا توجد حسابات متاحة.")
    groups = groups or {}
    selected = set()
    page = 0
    while True:
        visible = phones[page * 12:(page + 1) * 12]
        options = [(p, f"{'محدد · ' if p in selected else ''}+{p}") for p in visible]
        options += [("all", "الكل"), ("done", "تأكيد الاختيار"), ("reset", "مسح الاختيار")]
        if page:
            options.append(("previous", PREVIOUS))
        if (page + 1) * 12 < len(phones):
            options.append(("next", NEXT))
        if allow_count:
            options.append(("count", "اختيار حسب العدد"))
        options += [(f"group:{g}", f"مجموعة: {g}") for g in groups]
        value = await choose(
            conv, f"اختر الحسابات ثم اضغط تأكيد الاختيار. المحدد: {len(selected)}\nيمكنك أيضًا كتابة الأرقام بفواصل.",
            options, custom=True, page_size=100,
        )
        if value == "all":
            return phones
        if value == "done":
            if selected:
                return [p for p in phones if p in selected]
            await conv.send_message("اختر حسابًا واحدًا على الأقل.", parse_mode=None)
        elif value == "reset":
            selected.clear()
        elif value == "next":
            page = min(page + 1, (len(phones) - 1) // 12)
        elif value == "previous":
            page = max(0, page - 1)
        elif allow_count and (value == "count" or value.startswith("عدد:")):
            raw = await read(conv, f"أدخل عدد الحسابات من 1 إلى {len(phones)}:") if value == "count" else value.split(":", 1)[1]
            if raw.isdecimal() and 1 <= int(raw) <= len(phones):
                return phones[:int(raw)]
            await conv.send_message("العدد خارج النطاق.", parse_mode=None)
        elif value.startswith(("group:", "مجموعة:")):
            name = value.split(":", 1)[1].strip()
            selected.update(set(groups.get(name, ())) & set(phones))
        elif value in visible:
            selected.symmetric_difference_update({value})
        else:
            requested = {p.strip().lstrip("+") for p in value.replace("،", ",").split(",")}
            if requested and requested <= set(phones):
                return [p for p in phones if p in requested]
            await conv.send_message("بعض الأرقام غير متاحة لك. اختر الحسابات من الأزرار.", parse_mode=None)


def remember_country(user, action, kind, rows):
    # Bound abandoned selections and avoid carrying state into unrelated menus.
    for key, state in list(COUNTRY_INPUTS.items()):
        if state[3] <= time.monotonic():
            COUNTRY_INPUTS.pop(key, None)
    COUNTRY_INPUTS[user] = (action, kind, list(rows), time.monotonic() + 600)


def pending_country(user):
    state = COUNTRY_INPUTS.get(user)
    if state and state[3] <= time.monotonic():
        COUNTRY_INPUTS.pop(user, None)
        return None
    return state


def clear_country(user):
    COUNTRY_INPUTS.pop(user, None)


async def country_text(event):
    """Translate a country name into the same guarded action used by its button."""
    from .base import active_conversations
    from .registry import _TextMenuEvent
    from .callback_router import callback_handler
    from ui_config import PanelEvent

    if event.chat_id in active_conversations or event.sender_id != event.chat_id:
        return
    state = pending_country(event.chat_id)
    if not state:
        return
    action, kind, rows, _ = state
    text = (event.raw_text or "").strip()
    if text.startswith("/") or not text:
        return
    matches = match_countries(text, rows)
    if len(matches) != 1:
        from .number_handlers import country_panel
        return await country_panel(PanelEvent(_TextMenuEvent(event)), action, kind, matches or rows,
                                   hint="حدد الدولة من النتائج المتشابهة." if matches else "لم أجد الدولة. اختر من الأزرار أو أعد كتابة اسمها.",
                                   remember=False)
    proxy = _TextMenuEvent(event)
    proxy.data = f"num:{action}:{kind}:{matches[0][0]}".encode()
    await callback_handler(PanelEvent(proxy))
