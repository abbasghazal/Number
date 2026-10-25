"""Persistent group/channel destinations available to the bot broadcaster."""
import logging

from telethon.tl import types

from helpers import execute_query


logger = logging.getLogger(__name__)


async def remember_event_chat(event):
    """Store a group or channel when Telegram delivers an update from it."""
    if not (getattr(event, "is_group", False) or getattr(event, "is_channel", False)):
        return

    try:
        chat = await event.get_chat()
        if isinstance(chat, types.Channel):
            kind = "channel"
            chat_id = int(chat.id)
            access_hash = getattr(chat, "access_hash", None)
        elif isinstance(chat, types.Chat):
            kind = "group"
            chat_id = int(chat.id)
            access_hash = None
        else:
            return

        title = getattr(chat, "title", None) or getattr(chat, "username", None)
        await execute_query(
            """INSERT INTO broadcast_destinations(kind,chat_id,access_hash,title,last_seen)
               VALUES(?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(kind,chat_id) DO UPDATE SET
                   access_hash=COALESCE(excluded.access_hash,broadcast_destinations.access_hash),
                   title=COALESCE(excluded.title,broadcast_destinations.title),
                   last_seen=CURRENT_TIMESTAMP""",
            (kind, chat_id, access_hash, title),
            commit=True,
        )
    except Exception:
        # Recording a destination must never interrupt the bot's normal
        # message handling.
        logger.exception("Could not register broadcast destination")


async def destinations():
    """Return input peers for every group/channel that the bot has recorded."""
    rows = await execute_query(
        "SELECT kind,chat_id,access_hash FROM broadcast_destinations ORDER BY last_seen DESC",
        fetchall=True,
    ) or []
    peers = []
    for kind, chat_id, access_hash in rows:
        if kind == "channel":
            if access_hash is None:
                continue
            peers.append(types.InputPeerChannel(int(chat_id), int(access_hash)))
        elif kind == "group":
            peers.append(types.InputPeerChat(int(chat_id)))
    return peers
