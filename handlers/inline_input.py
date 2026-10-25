"""Race inline choices against typed replies in an active Telethon conversation."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import secrets
from types import SimpleNamespace

from keyboards import Button, FixedButtonRows


PREFIX = 'input:'
pending = {}


@dataclass
class Prompt:
    conversation: object
    labels: tuple
    result: asyncio.Future
    message_id: int | None = None


async def ask(conv, message, labels=(), *, timeout=300, columns=2):
    if columns not in (1, 2):
        raise ValueError('Unsupported input layout')
    token = secrets.token_urlsafe(12)
    state = Prompt(conv, tuple(str(label) for label in labels), asyncio.get_running_loop().create_future())
    pending[token] = state
    buttons = [Button.inline(label, data=f'{PREFIX}{token}:{index}') for index, label in enumerate(state.labels)]
    rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
    rows.append([Button.inline('إلغاء العملية', data='cancel_operation')])
    if columns == 1:
        rows = FixedButtonRows(rows)
    response = None
    sent = None
    try:
        sent = await conv.send_message(message, buttons=rows, parse_mode=None)
        state.message_id = sent.id
        # get_response returns a Future on Telethon, and a coroutine in mocks.
        response = asyncio.ensure_future(conv.get_response(sent, timeout=timeout))
        done, _ = await asyncio.wait((state.result, response), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise asyncio.TimeoutError
        # Prefer an actual message when both inputs arrive in the same turn.
        if response in done:
            return response.result()
        return SimpleNamespace(raw_text=state.result.result(), file=None)
    finally:
        pending.pop(token, None)
        # Telethon keeps canceled get_response futures in this mapping. Remove
        # our entry before the next text arrives and tries to resolve it.
        responses = getattr(conv, '_pending_responses', None)
        if sent is not None and isinstance(responses, dict):
            responses.pop(sent.id, None)
        for future in (state.result, response):
            if future is not None:
                if not future.done():
                    future.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await future
        if sent is not None:
            with contextlib.suppress(Exception):
                await conv._client.edit_message(conv.chat_id, sent.id, buttons=None)


async def callback(event):
    from .base import active_conversations
    try:
        _, token, index = event.data.decode().split(':')
        state = pending.get(token)
        if state is None:
            raise ValueError
        conv = state.conversation
        if (event.sender_id != conv.chat_id or event.chat_id != conv.chat_id
                or event.client is not conv._client
                or active_conversations.get(conv.chat_id) is not conv
                or event.message_id != state.message_id
                or state.result.done()):
            raise ValueError
        index = int(index)
        if not 0 <= index < len(state.labels):
            raise ValueError
    except (ValueError, AttributeError, UnicodeDecodeError):
        return await event.answer('انتهت صلاحية هذا الاختيار. استخدم أزرار الخطوة الحالية.', alert=True)
    state.result.set_result(state.labels[index])
    await event.answer()
