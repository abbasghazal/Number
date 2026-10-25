"""خدمات مستقلة للتفاعل مع منشورات تيليجرام.

تُبقي هذه الوحدة الدوال التي يمكن إعادة استخدامها خارج معالجات البوت. أما
لوحات الأزرار فتوجد في :mod:`keyboards`، ومعالجات محادثات المستخدم في
``handlers/account_services.py``.
"""

import asyncio
import logging
from typing import Iterable, Union

from telethon import functions, types
from session_guard import TelegramClient
from telethon.sessions import StringSession
from account_health import session_is_usable
from telethon.tl.functions.messages import (
    GetDiscussionMessageRequest,
    GetMessagesViewsRequest,
)

logger = logging.getLogger(__name__)


def _session_from_account(account: Union[str, tuple, list]) -> str:
    """يستخرج StringSession من نص مباشر أو من صف قاعدة بيانات."""
    return account if isinstance(account, str) else account[1]


def extract_entity_from_url(url: str):
    """يعيد اسم/معرّف القناة ورقم الرسالة من رابط ``t.me``."""
    try:
        parts = url.strip().split("/")
        if len(parts) < 5:
            return None, None
        if parts[3] == "c":
            channel_id = int(parts[4])
            channel_id = -1000000000000 - channel_id if channel_id < 0 else int(f"-100{channel_id}")
            return channel_id, int(parts[5])
        return parts[3], int(parts[4])
    except (IndexError, ValueError):
        return None, None


async def resolve_message_target_from_url(client: TelegramClient, url: str):
    entity, message_id = extract_entity_from_url(url)
    if entity is None or message_id is None:
        return None, None
    return await client.get_entity(entity), message_id


async def _with_client(session: str, api_id: int, api_hash: str) -> TelegramClient:
    if not await session_is_usable(session):
        raise ValueError("Account excluded by health check")
    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    return client


async def react_to_message(session: str, url: str, emoji: str, api_id: int, api_hash: str) -> bool:
    """يضيف تفاعلاً واحدًا إلى منشور باستخدام جلسة واحدة."""
    client = None
    try:
        client = await _with_client(session, api_id, api_hash)
        entity, message_id = await resolve_message_target_from_url(client, url)
        if entity is None:
            return False
        await client(functions.messages.SendReactionRequest(
            peer=entity, msg_id=message_id,
            reaction=[types.ReactionEmoji(emoticon=emoji)], big=True,
        ))
        return True
    except Exception:
        logger.exception("Could not react to message")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()


async def vote_in_poll(session: str, url: str, option_number: int, api_id: int, api_hash: str) -> bool:
    """يصوّت لخيار مرقّم من 1 في استفتاء."""
    if option_number < 1:
        return False
    client = None
    try:
        client = await _with_client(session, api_id, api_hash)
        entity, message_id = await resolve_message_target_from_url(client, url)
        if entity is None:
            return False
        await client(functions.messages.SendVoteRequest(
            peer=entity, msg_id=message_id, options=[bytes([option_number - 1])],
        ))
        return True
    except Exception:
        logger.exception("Could not vote in poll")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()


async def increment_views_count(session: str, url: str, api_id: int, api_hash: str) -> bool:
    """يسجّل مشاهدة واحدة للمنشور من جلسة واحدة."""
    client = None
    try:
        client = await _with_client(session, api_id, api_hash)
        entity, message_id = await resolve_message_target_from_url(client, url)
        if entity is None:
            return False
        await client(GetMessagesViewsRequest(peer=entity, id=[message_id], increment=True))
        return True
    except Exception:
        logger.exception("Could not increment views")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()


async def comment_on_message(session: str, url: str, text: str, api_id: int, api_hash: str) -> bool:
    """يرسل تعليقًا واحدًا ويدعم مجموعات النقاش المرتبطة بالقنوات."""
    client = None
    try:
        client = await _with_client(session, api_id, api_hash)
        entity, message_id = await resolve_message_target_from_url(client, url)
        if entity is None:
            return False
        try:
            discussion = await client(GetDiscussionMessageRequest(peer=entity, msg_id=message_id))
            if discussion.messages:
                message = discussion.messages[0]
                await client.send_message(message.peer_id, text, reply_to=message.id)
                return True
        except Exception:
            logger.debug("Discussion message unavailable; trying direct reply", exc_info=True)
        await client.send_message(entity, text, reply_to=message_id)
        return True
    except Exception:
        logger.exception("Could not comment on message")
        return False
    finally:
        if client and client.is_connected():
            await client.disconnect()


async def _run_for_accounts(accounts: Iterable, count: int, action, delay: float = 1.0) -> int:
    """يشغّل إجراءً على أول ``count`` جلسات ويعيد عدد العمليات الناجحة."""
    accounts = list(accounts)
    if count <= 0 or count > len(accounts):
        return 0
    success = 0
    for account in accounts[:count]:
        try:
            if await action(_session_from_account(account)):
                success += 1
        except Exception:
            logger.exception("Account action failed")
        if delay:
            await asyncio.sleep(delay)
    return success


async def mass_react(accounts, url: str, emoji: str, count: int, api_id: int, api_hash: str) -> int:
    return await _run_for_accounts(accounts, count, lambda session: react_to_message(session, url, emoji, api_id, api_hash))


async def mass_vote(accounts, url: str, option: int, count: int, api_id: int, api_hash: str) -> int:
    return await _run_for_accounts(accounts, count, lambda session: vote_in_poll(session, url, option, api_id, api_hash))


async def mass_comment(accounts, url: str, text: str, count: int, api_id: int, api_hash: str) -> int:
    return await _run_for_accounts(accounts, count, lambda session: comment_on_message(session, url, text, api_id, api_hash))


async def increment_views(accounts, url: str, count: int, api_id: int, api_hash: str) -> int:
    return await _run_for_accounts(accounts, count, lambda session: increment_views_count(session, url, api_id, api_hash))
