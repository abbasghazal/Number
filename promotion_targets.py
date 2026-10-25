"""Read-only paid-promotion target checks, independent of point-funding rights."""

import asyncio

from telethon import errors, functions, types
from shahm_engine import parse_telegram_link


class PaidInviteRequired(ValueError):
    pass


class TargetMismatch(ValueError):
    pass


async def validate_paid_target(bot, user, raw, kind, invite=None, *, expected_id=None):
    from promotion_service import target_input, pool, account_for
    import automation
    from session_guard import SessionBusyError

    if kind not in ("members", "reactions", "search"):
        raise ValueError("خدمة الرشق غير صالحة.")
    original = parse_telegram_link(str(raw).strip())
    if kind in ("members", "search") and original and original[0] == "private":
        if kind == "search":
            raise ValueError("الرشق عبر البحث يحتاج معرف هدف عام.")
        peer, message = None, 0
        invite = str(raw).strip()
    else:
        peer, message = target_input(str(raw), kind)
    private = parse_telegram_link(invite) if invite else None
    if invite and (not private or private[0] != "private"):
        raise ValueError("أرسل رابط دعوة خاصًا صالحًا من تيليجرام.")
    if not isinstance(peer, int) and peer is not None:
        private = None  # A public target always keeps its canonical public link.

    async def inspect(reader):
        if private:
            preview = await reader(functions.messages.CheckChatInviteRequest(private[1]))
            if not isinstance(preview, (types.ChatInviteAlready, types.ChatInvitePeek)):
                raise PaidInviteRequired(
                    "تعذر تحديد معرف الهدف الخاص دون الانضمام. يلزم حساب تنفيذ يمكنه الوصول إليه أو معاينة تكشف معرفه."
                )
            entity = preview.chat
        else:
            entity = await reader.get_entity(peer)
        if isinstance(entity, types.ChannelForbidden):
            raise ValueError("الهدف غير متاح لهذا الحساب.")
        if not isinstance(entity, types.Channel):
            raise TargetMismatch("الهدف يجب أن يكون قناة أو مجموعة كبيرة.")
        identity = -1000000000000 - entity.id
        if expected_id is not None and identity != expected_id or isinstance(peer, int) and identity != peer:
            raise TargetMismatch("تغير الهدف أو أن رابط الدعوة لا يطابق معرفه؛ اطلب معاينة جديدة.")
        username = getattr(entity, "username", None)
        if kind == "search" and not username:
            raise TargetMismatch("الرشق عبر البحث يحتاج معرف هدف عام.")
        if not username and not private:
            raise PaidInviteRequired("الهدف خاص؛ أرسل رابط دعوته الصالح. لا يحتاج البوت صلاحية مشرف.")
        if message:
            post = await reader.get_messages(entity, ids=message)
            if not post or isinstance(post, types.MessageEmpty):
                raise TargetMismatch("المنشور غير موجود أو غير متاح لحساب التنفيذ.")
        link = "https://t.me/" + username if username else "https://t.me/+" + private[1]
        return dict(id=identity, link=link, title=entity.title[:150], message_id=message)

    if not private:
        try:
            return await asyncio.wait_for(inspect(bot), 15)
        except TargetMismatch:
            raise
        except PaidInviteRequired:
            raise
        except (errors.RPCError, ValueError, TimeoutError, asyncio.TimeoutError, OSError):
            pass
    last_invite_error = None
    for phone, owner, fingerprint in (await pool())[:3]:
        try:
            account = await account_for(phone, owner, fingerprint)
            async with automation.connection(account) as reader:
                return await asyncio.wait_for(inspect(reader), 15)
        except TargetMismatch:
            raise
        except PaidInviteRequired as exc:
            last_invite_error = exc
        except errors.FloodWaitError as exc:
            await automation.save_wait(phone, exc.seconds + 1)
        except (
            errors.RPCError,
            ValueError,
            TimeoutError,
            asyncio.TimeoutError,
            OSError,
            SessionBusyError,
            PermissionError,
        ):
            continue
    if last_invite_error:
        raise last_invite_error
    if isinstance(peer, int) and not private and kind != "search":
        raise PaidInviteRequired("تعذر الوصول إلى الهدف بمعرفه الرقمي؛ أرسل رابط دعوته الخاص أو استخدم معرفه العام.")
    raise ValueError(
        "تعذر قراءة الهدف بحساب متاح. استخدم @معرفًا عامًا أو رابط دعوة يمكن لحساب تنفيذ الوصول إليه. لا يلزم جعل البوت مشرفًا."
    )
