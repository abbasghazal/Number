"""Safe, shared intake for phone-derived and StringSession account submissions.

The module intentionally never logs or returns a session value.  Callers only
receive account metadata and a user-safe reason when validation fails.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from session_guard import TelegramClient
from telethon import functions
from telethon.errors import (
    AuthKeyInvalidError, AuthKeyUnregisteredError, FloodWaitError, RPCError,
    SessionExpiredError, SessionRevokedError, UnauthorizedError,
    UserDeactivatedBanError, UserDeactivatedError,
)
from telethon.tl.functions.account import GetPasswordRequest
from telethon.sessions import StringSession

from config import API_ID, API_HASH
from helpers import get_country, account_exists_anywhere

MAX_SESSION_LENGTH = 4096
MAX_TXT_BYTES = 512 * 1024
MAX_TXT_SESSIONS = 1000
_PHONE = re.compile(r"^\d{7,20}$")
_TELEGRAM_DC_TARGETS: set[tuple[int, str, int]] = set()
_TELEGRAM_DC_TARGETS_AT = 0.0


async def _telegram_dc_targets():
    """Read the current DC endpoint list through the trusted bot connection."""
    global _TELEGRAM_DC_TARGETS, _TELEGRAM_DC_TARGETS_AT
    if _TELEGRAM_DC_TARGETS and asyncio.get_running_loop().time() - _TELEGRAM_DC_TARGETS_AT < 300:
        return _TELEGRAM_DC_TARGETS
    from helpers import bot
    if bot is None:
        return set()
    config = await bot(functions.help.GetConfigRequest())
    _TELEGRAM_DC_TARGETS = {
        (int(option.id), ipaddress.ip_address(option.ip_address).compressed, int(option.port))
        for option in config.dc_options
        if not getattr(option, 'cdn', False) and not getattr(option, 'media_only', False)
    }
    _TELEGRAM_DC_TARGETS_AT = asyncio.get_running_loop().time()
    return _TELEGRAM_DC_TARGETS


@dataclass(frozen=True)
class ImportedAccount:
    phone: str
    session: str
    calling_code: str
    name: str
    username: str | None
    telegram_id: int


@dataclass(frozen=True)
class ImportResult:
    account: ImportedAccount | None = None
    reason: str | None = None
    duplicate: bool = False


async def validate_phone_submission(phone: str, *, required_calling_code: str | None = None, check_existing=True):
    """Apply the shared country, format, and duplicate rules before login."""
    phone = (phone or "").replace("+", "").replace(" ", "")
    if not _PHONE.fullmatch(phone):
        return None, "رقم الهاتف غير صالح."
    country = await get_country(next((code for code in await _country_codes() if phone.startswith(code)), ""))
    if not country:
        return None, "دولة هذا الحساب غير مدعومة."
    if required_calling_code and country[0] != required_calling_code:
        return None, "الرقم لا يتبع الدولة المختارة."
    if check_existing and await account_exists_anywhere(phone):
        return None, "الحساب موجود أو استُخدم سابقًا في النظام."
    return country, None


def split_sessions(content: bytes) -> list[str]:
    """Read an in-memory UTF-8 TXT safely; no raw file is persisted."""
    if len(content) > MAX_TXT_BYTES:
        raise ValueError("حجم الملف أكبر من الحد المسموح.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("الملف يجب أن يكون TXT بترميز UTF-8.") from exc
    unique: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        value = line.strip()
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    if len(unique) > MAX_TXT_SESSIONS:
        raise ValueError(f"الحد الأقصى هو {MAX_TXT_SESSIONS} جلسة في الملف.")
    return unique


def parse_session_file(content: bytes, *, allow_current_password: bool = False) -> list[tuple[str, str | None]]:
    """Parse one UTF-8 session file under the project-wide upload limits.

    A current 2FA password is optional and is accepted only after an explicit
    ``|`` separator.  It is never logged or written to disk by this parser.
    """
    if len(content) > MAX_TXT_BYTES:
        raise ValueError("حجم الملف أكبر من الحد المسموح.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("الملف يجب أن يكون TXT بترميز UTF-8.") from exc

    entries: list[tuple[str, str | None]] = []
    seen_sessions: set[str] = set()
    for raw_line in text.splitlines():
        session_text, separator, current_password = raw_line.strip().partition("|")
        session_text = "".join(session_text.replace("`", "").split())
        if not session_text:
            continue
        if len(session_text) > MAX_SESSION_LENGTH:
            raise ValueError("صيغة الجلسة أو طولها غير صالح.")
        if separator and not allow_current_password:
            raise ValueError("ملف الجلسات لا يقبل كلمة تحقق بخطوتين في هذا المسار.")
        if session_text in seen_sessions:
            continue
        seen_sessions.add(session_text)
        entries.append((session_text, current_password.strip() if separator else None))
        if len(entries) > MAX_TXT_SESSIONS:
            raise ValueError(f"الحد الأقصى هو {MAX_TXT_SESSIONS} جلسة في الملف.")
    return entries


async def import_string_session(value: str, *, required_calling_code: str | None = None, check_existing=True) -> ImportResult:
    """Validate one StringSession and resolve it to an eligible account."""
    value = (value or "").strip()
    if not value or len(value) > MAX_SESSION_LENGTH:
        return ImportResult(reason="صيغة الجلسة أو طولها غير صالح.")
    client = None
    try:
        # Parsing first avoids opening a connection for obviously invalid input.
        session = StringSession(value)
        address = ipaddress.ip_address(session.server_address)
        targets = await _telegram_dc_targets()
        if (not address.is_global or (int(session.dc_id), address.compressed, int(session.port)) not in targets):
            return ImportResult(reason="عنوان خادم الجلسة غير مسموح به.")
        client = TelegramClient(session, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return ImportResult(reason="الجلسة غير مخوّلة أو منتهية الصلاحية.")
        me = await client.get_me()
        phone = str(getattr(me, "phone", "") or "").strip()
        if not _PHONE.fullmatch(phone):
            return ImportResult(reason="تعذر استخراج رقم صالح من الجلسة.")
        if getattr(me, "bot", False):
            return ImportResult(reason="لا يمكن عرض حساب بوت للبيع.")
        if getattr(me, "deleted", False) or getattr(me, "restriction_reason", None):
            return ImportResult(reason="الحساب مقيّد أو غير مؤهل للبيع.")
        country, reason = await validate_phone_submission(phone, required_calling_code=required_calling_code, check_existing=check_existing)
        if not country:
            return ImportResult(reason=reason, duplicate=reason == "الحساب موجود أو استُخدم سابقًا في النظام.")
        # This request is deliberately only a capability check.  We never ask
        # for, receive, or persist a 2FA password when a session is submitted.
        await client(GetPasswordRequest())
        name = " ".join(filter(None, (getattr(me, "first_name", None), getattr(me, "last_name", None)))).strip()
        return ImportResult(ImportedAccount(phone, value, country[0], name, getattr(me, "username", None), me.id))
    except FloodWaitError:
        return ImportResult(reason="الحساب في فترة انتظار Telegram (FloodWait).")
    except (AuthKeyInvalidError, AuthKeyUnregisteredError, SessionExpiredError, SessionRevokedError,
            UnauthorizedError, UserDeactivatedError, UserDeactivatedBanError):
        return ImportResult(reason="الجلسة غير صالحة أو الحساب معطّل.")
    except (RPCError, OSError, asyncio.TimeoutError):
        return ImportResult(reason="تعذر التحقق من الجلسة الآن؛ حاول لاحقًا.")
    except Exception:
        # Do not include exception text: libraries can embed the session in it.
        return ImportResult(reason="تعذر قراءة الجلسة. تأكد من صيغتها.")
    finally:
        if client:
            await client.disconnect()


async def _country_codes() -> list[str]:
    from helpers import get_countries
    # Longest prefix wins (calling codes can overlap).
    return sorted((row[0] for row in await get_countries()), key=len, reverse=True)


async def import_batch(values: Iterable[str], *, required_calling_code: str | None = None,
                       concurrency: int = 3, check_existing: bool = True,
                       on_progress: Callable[[int, int], Awaitable[None]] | None = None) -> list[ImportResult]:
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 5)))

    async def one(value: str) -> ImportResult:
        async with semaphore:
            return await import_string_session(
                value,
                required_calling_code=required_calling_code,
                check_existing=check_existing,
            )
    # Do not create one task per line: a large upload should stay bounded both
    # in active Telegram connections and in scheduled asyncio tasks.
    iterator = iter(values)
    results: list[ImportResult] = []
    processed = 0
    while True:
        chunk = []
        try:
            for _ in range(25):
                chunk.append(next(iterator))
        except StopIteration:
            pass
        if not chunk:
            break
        results.extend(await asyncio.gather(*(one(value) for value in chunk)))
        processed += len(chunk)
        if on_progress:
            await on_progress(processed, len(results))
    # Different valid session strings can resolve to the same Telegram account.
    # The validation tasks run concurrently, so deduplicate their resolved
    # phone numbers before callers persist any result.
    seen_phones: set[str] = set()
    deduplicated: list[ImportResult] = []
    for result in results:
        if result.account and result.account.phone in seen_phones:
            deduplicated.append(ImportResult(
                reason="الحساب مكرر داخل الملف.", duplicate=True
            ))
        else:
            if result.account:
                seen_phones.add(result.account.phone)
            deduplicated.append(result)
    return deduplicated
