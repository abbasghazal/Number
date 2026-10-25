"""A single, rate-limited delivery queue for Telegram broadcasts."""
import asyncio
import logging
import os
from dataclasses import dataclass

from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)


@dataclass
class DeliveryReport:
    success: int = 0
    failed: int = 0


class OutboundQueue:
    def __init__(self, client, *, messages_per_second=15):
        self.client = client
        self.messages_per_second = max(float(messages_per_second), 0.1)
        self.delay = 1 / self.messages_per_second
        self.queue = asyncio.Queue(maxsize=100)
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._worker(), name="telegram-outbound-queue")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def broadcast(self, destinations, text, *, buttons=None, parse_mode=None):
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((list(destinations), text, buttons, parse_mode, future))
        return await future

    async def send(self, destination, text, *, buttons=None, parse_mode=None):
        return await self.broadcast([destination], text, buttons=buttons, parse_mode=parse_mode)

    async def _worker(self):
        while True:
            destinations, text, buttons, parse_mode, future = await self.queue.get()
            report = DeliveryReport()
            try:
                for destination in destinations:
                    try:
                        await self.client.send_message(destination, text, buttons=buttons, parse_mode=parse_mode)
                        report.success += 1
                        await asyncio.sleep(self.delay)
                    except FloodWaitError as exc:
                        logger.warning("Telegram flood wait: %ss", exc.seconds)
                        await asyncio.sleep(exc.seconds + 1)
                        for retry in range(3):
                            try:
                                await self.client.send_message(destination, text, buttons=buttons, parse_mode=parse_mode)
                                report.success += 1
                                await asyncio.sleep(self.delay)
                                break
                            except FloodWaitError as retry_exc:
                                await asyncio.sleep(retry_exc.seconds + 1)
                            except Exception:
                                if retry == 2:
                                    report.failed += 1
                                else:
                                    await asyncio.sleep(2 ** retry)
                    except Exception:
                        report.failed += 1
                if not future.done():
                    future.set_result(report)
            except Exception as exc:
                logger.exception("Outbound queue job failed")
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.queue.task_done()


outbound_queue = None


def start_outbound_queue(client):
    global outbound_queue
    outbound_queue = OutboundQueue(
        client, messages_per_second=float(os.getenv("OUTBOUND_MESSAGES_PER_SECOND", "5"))
    )
    outbound_queue.start()
    return outbound_queue


async def stop_outbound_queue():
    if outbound_queue:
        await outbound_queue.stop()


async def queue_broadcast(destinations, text, *, buttons=None, parse_mode=None):
    if outbound_queue is None:
        raise RuntimeError("Outbound queue has not started")
    return await outbound_queue.broadcast(destinations, text, buttons=buttons, parse_mode=parse_mode)


async def queue_message(destination, text, *, buttons=None, parse_mode=None):
    if outbound_queue is None:
        raise RuntimeError("Outbound queue has not started")
    return await outbound_queue.send(destination, text, buttons=buttons, parse_mode=parse_mode)
