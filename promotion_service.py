"""Owned account consent, Telegram checks and durable promotion execution."""

import asyncio
import contextlib
import logging
import re
from urllib.parse import urlparse

from telethon import errors, functions, types

import account_access
import automation
import promotion_store as store
from account_health import session_is_usable
from managed_twofa import state_key
from session_guard import SessionBusyError
from shahm_engine import parse_telegram_link
from utils.asyncio_compat import to_thread
from workflow import query
from promotion_targets import PaidInviteRequired as PaidInviteRequired,validate_paid_target

LOG = logging.getLogger(__name__)
EMOJIS = ("👍", "❤️", "🔥", "👏", "🎉", "😍", "😁", "🤩")
_workers = {}
_worker_kinds = {}


def target_input(raw, kind):
    raw = raw.strip()
    if kind in {"members", "search"}:
        if re.fullmatch(r"-100[0-9]{5,15}", raw):
            return int(raw), 0
        target = parse_telegram_link(raw)
        if not target or target[0] != "public":
            raise ValueError("أدخل @معرف الهدف العام أو معرفه الرقمي -100….")
        return target[1], 0
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"t.me", "telegram.me"}
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("أدخل رابط منشور تيليجرام صالحًا.")
    parts = parsed.path.strip("/").split("/")
    if len(parts) == 2 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", parts[0]) and parts[1].isdigit():
        return parts[0], int(parts[1])
    if len(parts) == 3 and parts[0] == "c" and parts[1].isdigit() and parts[2].isdigit():
        return -1000000000000 - int(parts[1]), int(parts[2])
    raise ValueError("أدخل رابط منشور مثل https://t.me/channel/123.")


async def is_member(bot, target, user):
    try:
        result = await bot(functions.channels.GetParticipantRequest(target, user))
        participant = result.participant
        return not isinstance(participant, types.ChannelParticipantLeft) and not (
            isinstance(participant, types.ChannelParticipantBanned) and participant.left
        )
    except errors.UserNotParticipantError:
        return False


async def validate_target(bot, user, raw, kind, invite=None):
    """Point funding requires requester ownership and bot membership-verification rights."""
    peer, message = target_input(raw, kind)
    try:
        entity = await bot.get_entity(peer)
        if not isinstance(entity, types.Channel):
            raise ValueError("الهدف يجب أن يكون قناة أو مجموعة كبيرة.")
        participant = (await bot(functions.channels.GetParticipantRequest(entity, user))).participant
        if not isinstance(participant, (types.ChannelParticipantCreator, types.ChannelParticipantAdmin)):
            raise ValueError("يجب أن تكون مالكًا أو مشرفًا في الهدف.")
        bot_participant = (await bot(functions.channels.GetParticipantRequest(entity, "me"))).participant
        if not isinstance(bot_participant, (types.ChannelParticipantCreator, types.ChannelParticipantAdmin)):
            raise ValueError("أضف البوت مشرفًا في الهدف ليتمكن من التحقق من الاشتراكات.")
        if message and not await bot.get_messages(entity, ids=message):
            raise ValueError("المنشور غير موجود.")
    except errors.RPCError:
        raise ValueError("تعذر التحقق من الهدف؛ أضف البوت مشرفًا وتحقق من المعرف وصلاحياتك.") from None
    username = getattr(entity, "username", None)
    if username:
        link = "https://t.me/" + username
    else:
        try:
            link = (
                await bot(
                    functions.messages.ExportChatInviteRequest(
                        peer=entity, title="تمويل ورشق البوت", request_needed=False
                    )
                )
            ).link
        except errors.RPCError:
            raise ValueError("امنح البوت صلاحية دعوة المستخدمين لإنشاء رابط مرتبط بالهدف الخاص.") from None
    if not link or not parse_telegram_link(link):
        raise ValueError("تعذر إنشاء رابط دعوة للهدف الخاص.")
    return {"id": -1000000000000 - entity.id, "link": link, "title": entity.title[:150], "message_id": message}


async def revalidate_paid_target(bot,user,kind,target):
    link=target['link']
    message=target.get('message_id',0)
    private=parse_telegram_link(link)
    raw=(f'https://t.me/c/{abs(target["id"])-1000000000000}/{message}' if private and private[0]=='private'
         else link+'/'+str(message)) if kind=='reactions' else link
    return await validate_paid_target(bot,user,raw,kind,invite=link if private and private[0]=='private' else None,expected_id=target['id'])


async def set_pool(user, phones, enabled):
    raise ValueError("مصادر الرشق المدفوع مركزية ويشغّلها المطور فقط.")


async def pool():
    """Return the central account pool used by periodic point collection too."""
    from funding_auto_collect import collector_accounts

    accounts = await collector_accounts()
    waits = {
        phone
        for (phone,) in await query(
            "SELECT phone FROM automation_waits WHERE until_at>CURRENT_TIMESTAMP", fetchall=True
        ) or []
    }
    result = []
    for phone, account in accounts.items():
        if phone in waits or not await session_is_usable(account[1]):
            continue
        owner, fingerprint = account[2], state_key(phone, account[1])
        # Keep the durable order records fingerprinted, but source membership
        # is determined exclusively by the developer-controlled central pool.
        await query(
            """INSERT INTO promotion_pool(phone,owner_id,session_key,enabled) VALUES(?,?,?,TRUE)
               ON CONFLICT(phone) DO UPDATE SET owner_id=excluded.owner_id,session_key=excluded.session_key,
               enabled=TRUE,updated_at=CURRENT_TIMESTAMP""",
            (phone, owner, fingerprint), commit=True,
        )
        result.append((phone, owner, fingerprint))
    return result


async def checkpoint(user, account):
    if not await automation.access_active(user) or not await account_access.authorized(user, account, owned_only=True):
        raise PermissionError()


async def account_for(phone, owner, fingerprint):
    from funding_auto_collect import collector_accounts

    account = (await collector_accounts()).get(str(phone))
    if (
        not account
        or account[2] != owner
        or state_key(phone, account[1]) != fingerprint
        or not await session_is_usable(account[1])
    ):
        raise PermissionError()
    return account


async def resolved_peer(client, order):
    target_id, link, msg = order
    target = parse_telegram_link(link)
    if target and target[0] == "public":
        entity = await client.get_entity(target[1])
        if not isinstance(entity, types.Channel) or -1000000000000 - entity.id != target_id:
            raise ValueError("target_changed")
        return entity
    if target and target[0] == "private":
        invite = await client(functions.messages.CheckChatInviteRequest(target[1]))
        if isinstance(invite, (types.ChatInviteAlready, types.ChatInvitePeek)):
            if not isinstance(invite.chat, types.Channel) or -1000000000000 - invite.chat.id != target_id:
                raise ValueError("target_changed")
            return invite.chat
        # The invite's immutable target was checked before the paid quote.
        return None
    raise ValueError("invalid_target")


async def has_reaction(client, entity, message, emoji):
    msg = await client.get_messages(entity, ids=message)
    if not msg:
        raise ValueError("message_missing")
    selected = [
        r
        for r in getattr(getattr(msg, "reactions", None), "results", [])
        if getattr(r, "chosen_order", None) is not None
    ]
    return any(isinstance(r.reaction, types.ReactionEmoji) and r.reaction.emoticon == emoji for r in selected), bool(
        selected
    )


async def perform_item(key, phone, owner, fingerprint, reconcile=False):
    row = await query(
        "SELECT kind,target_id,target,message_id,emoji,user_id,cancel_requested FROM promotion_orders WHERE order_id=?",
        (key,),
        fetchone=True,
    )
    if not row:
        return
    kind, target_id, link, message, emoji, user, cancelled = row
    if not reconcile and not await to_thread(store.reserve_item, key, phone):
        return
    sending = False
    try:
        if not reconcile and (cancelled or not await automation.access_active(user)):
            raise PermissionError()
        account = await account_for(phone, owner, fingerprint)
        async with automation.connection(account) as client:
            me = await client.get_me()
            if not me or me.bot or str(me.phone).lstrip("+") != phone:
                raise PermissionError()
            entity = await resolved_peer(client, (target_id, link, message))
            if kind in {"members", "search"}:
                exists = entity is not None and await is_member(client, entity, "me")
                if reconcile:
                    prior = await query(
                        "SELECT result FROM promotion_items WHERE order_id=? AND phone=?", (key, phone), fetchone=True
                    )
                    if not exists and prior and prior[0] == "join_requested":
                        return
                    return await to_thread(
                        store.finish_item, key, phone, "success" if exists else "failed", "reconciled"
                    )
                if exists:
                    return await to_thread(store.finish_item, key, phone, "failed", "already_member")
            else:
                if entity is None:
                    raise ValueError("private_post_not_accessible")
                matched, selected = await has_reaction(client, entity, message, emoji)
                if reconcile:
                    return await to_thread(
                        store.finish_item, key, phone, "success" if matched else "failed", "reconciled"
                    )
                if selected:
                    return await to_thread(store.finish_item, key, phone, "failed", "existing_reaction")
            await account_for(phone, owner, fingerprint)
            if not await automation.access_active(user):
                raise PermissionError()
            stop = await query("SELECT cancel_requested FROM promotion_orders WHERE order_id=?", (key,), fetchone=True)
            if not stop or stop[0]:
                raise PermissionError()
            await query(
                "UPDATE promotion_items SET status='executing',updated_at=CURRENT_TIMESTAMP WHERE order_id=? AND phone=?",
                (key, phone),
                commit=True,
            )
            sending = True
            if kind == "search":
                from search_join import search_and_join

                phrase = await query(
                    "SELECT search_phrase FROM promotion_orders WHERE order_id=?", (key,), fetchone=True
                )

                async def guarded():
                    await account_for(phone, owner, fingerprint)
                    stop = await query(
                        "SELECT cancel_requested FROM promotion_orders WHERE order_id=?", (key,), fetchone=True
                    )
                    if not stop or stop[0]:
                        raise PermissionError()

                outcome = await search_and_join(client, phrase[0], abs(target_id) - 1000000000000, guarded)
                if outcome not in {"joined", "already", "unknown", "requested"}:
                    return await to_thread(store.finish_item, key, phone, "failed", outcome)
                if outcome == "requested":
                    return await to_thread(store.finish_item, key, phone, "unknown", "join_requested")
                confirmed = outcome == "joined"
            elif kind == "members":
                if entity is None:
                    target = parse_telegram_link(link)
                    updates = await client(functions.messages.ImportChatInviteRequest(target[1]))
                    entity = next(
                        (
                            chat
                            for chat in updates.chats
                            if isinstance(chat, types.Channel) and -1000000000000 - chat.id == target_id
                        ),
                        None,
                    )
                    if entity is None:
                        raise ValueError("target_not_confirmed")
                else:
                    await client(functions.channels.JoinChannelRequest(entity))
                confirmed = await is_member(client, entity, "me")
            else:
                await client(
                    functions.messages.SendReactionRequest(
                        peer=entity, msg_id=message, reaction=[types.ReactionEmoji(emoticon=emoji)]
                    )
                )
                confirmed, _ = await has_reaction(client, entity, message, emoji)
            await to_thread(
                store.finish_item,
                key,
                phone,
                "success" if confirmed else "unknown",
                "confirmed" if confirmed else "awaiting_verification",
            )
    except errors.FloodWaitError as exc:
        await automation.save_wait(phone, exc.seconds + 1)
        await to_thread(store.finish_item, key, phone, "unknown" if sending or reconcile else "failed", "flood_wait")
    except errors.InviteRequestSentError:
        await to_thread(store.finish_item, key, phone, "unknown", "join_requested")
    except asyncio.CancelledError:
        await to_thread(store.finish_item, key, phone, "unknown" if sending or reconcile else "failed", "interrupted")
        raise
    except (errors.RPCError, PermissionError, SessionBusyError, ValueError) as exc:
        # RPC errors are explicit rejections; a read failure after mutation stays ambiguous.
        await to_thread(
            store.finish_item, key, phone, "unknown" if sending or reconcile else "failed", type(exc).__name__
        )
    except Exception as exc:
        LOG.warning("Promotion item deferred (%s)", type(exc).__name__)
        await to_thread(store.finish_item, key, phone, "unknown" if sending or reconcile else "failed", "network_error")


async def run_order(key):
    deferred = False
    try:
        cfg = await query(
            "SELECT delay_seconds,error_limit,cooldown_seconds,attempt_multiplier FROM promotion_execution_config WHERE id=1",
            fetchone=True,
        )
        delay, error_limit, cooldown, multiplier = cfg or (2, 5, 300, 3)
        while True:
            order = await query(
                "SELECT quantity,cancel_requested,consecutive_errors,status FROM promotion_orders WHERE order_id=?",
                (key,),
                fetchone=True,
            )
            if not order or order[3] not in ("running", "review"):
                break
            rows = (
                await query(
                    "SELECT phone,owner_id,session_key,status FROM promotion_items WHERE order_id=? AND status IN ('pending','unknown') ORDER BY CASE WHEN status='unknown' THEN 0 ELSE 1 END,phone",
                    (key,),
                    fetchall=True,
                )
                or []
            )
            errors_in_row = order[2]
            for phone, owner, fingerprint, status in rows:
                control = await query("SELECT enabled FROM promotion_execution_config WHERE id=1", fetchone=True)
                if control is not None and not control[0]:
                    deferred = True
                    return
                await perform_item(key, phone, owner, fingerprint, status == "unknown")
                result = await query(
                    "SELECT status FROM promotion_items WHERE order_id=? AND phone=?", (key, phone), fetchone=True
                )
                errors_in_row = errors_in_row + 1 if result and result[0] == "failed" else 0
                if errors_in_row >= error_limit:
                    await query(
                        "UPDATE promotion_orders SET next_attempt=CURRENT_TIMESTAMP+?*INTERVAL '1 second',consecutive_errors=0 WHERE order_id=?",
                        (cooldown, key),
                        commit=True,
                    )
                    deferred = True
                    return
                await asyncio.sleep(delay)
            counts = dict(
                await query(
                    "SELECT status,COUNT(*) FROM promotion_items WHERE order_id=? GROUP BY status",
                    (key,),
                    fetchall=True,
                )
                or []
            )
            held = counts.get("success", 0) + counts.get("unknown", 0)
            attempted = sum(counts.values())
            if held >= order[0] or order[1] or order[3] == "review" or attempted >= order[0] * multiplier:
                break
            existing = {
                r[0]
                for r in await query("SELECT phone FROM promotion_items WHERE order_id=?", (key,), fetchall=True) or []
            }
            candidates = [a for a in await pool() if a[0] not in existing][
                : min(order[0] - held, order[0] * multiplier - attempted)
            ]
            if not candidates:
                break
            for phone, owner, fingerprint in candidates:
                await query(
                    "INSERT INTO promotion_items(order_id,phone,owner_id,session_key) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                    (key, phone, owner, fingerprint),
                    commit=True,
                )
    finally:
        if not deferred:
            await to_thread(store.settle, key)


async def dispatch():
    await query(
        "UPDATE promotion_orders SET status='expired' WHERE status='quote' AND expires_at<=CURRENT_TIMESTAMP",
        commit=True,
    )
    cfg = await query(
        "SELECT enabled,max_orders,members_limit,reactions_limit,search_limit FROM promotion_execution_config WHERE id=1",
        fetchone=True,
    )
    if not cfg or not cfg[0]:
        return
    limits = dict(zip(("members", "reactions", "search"), cfg[2:]))
    for key, kind in (
        await query(
            "SELECT order_id,kind FROM promotion_orders WHERE status IN ('running','review') AND next_attempt<=CURRENT_TIMESTAMP ORDER BY updated_at,created_at LIMIT 50",
            fetchall=True,
        )
        or []
    ):
        if key in _workers:
            continue
        if len(_workers) >= cfg[1] or sum(v == kind for v in _worker_kinds.values()) >= limits[kind]:
            continue
        task = asyncio.create_task(run_order(key), name=f"promotion-{key}")
        _workers[key] = task
        _worker_kinds[key] = kind

        def finished(done, key=key):
            _workers.pop(key, None)
            _worker_kinds.pop(key, None)
            if not done.cancelled() and done.exception():
                LOG.error("Promotion worker failed (%s)", type(done.exception()).__name__)

        task.add_done_callback(finished)


async def recover():
    await query("UPDATE promotion_items SET status='pending' WHERE status='checking'", commit=True)
    await query(
        "UPDATE promotion_items SET status='unknown',result='interrupted' WHERE status='executing'", commit=True
    )


async def stop():
    tasks = list(_workers.values())
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    _workers.clear()
    _worker_kinds.clear()
