import asyncio
import contextlib
import logging
import os
from telethon import TelegramClient
from config import API_ID, API_HASH, TOKEN, DB_PATH, validate_config
from database import init_db
from handlers import register_handlers, license_notification_task
from helpers import set_global_bot

# إعداد نظام تسجيل الأخطاء
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def health_response(reader, writer):
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=2)
        parts = request_line.decode("latin-1", errors="ignore").split()
        path = parts[1] if len(parts) > 1 else "/"
        status = "200 OK" if path in ("/", "/health", "/healthz") else "404 Not Found"
        body = "ok\n" if status.startswith("200") else "not found\n"
        response = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n"
            "\r\n"
            f"{body}"
        )
        writer.write(response.encode("utf-8"))
        await writer.drain()
    except Exception as exc:
        logger.debug("Health check response failed: %s", exc)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

async def run_health_server():
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "10000"))
    server = await asyncio.start_server(health_response, host, port)
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    logger.info("🌐 Health server listening on %s", sockets)
    async with server:
        await server.serve_forever()

async def background_tasks():
    while True:
        await asyncio.sleep(3600)  # كل ساعة
        # يمكن إضافة مهام خلفية أخرى هنا

async def main():
    client = None
    health_task = None
    try:
        validate_config()
        # Initialize the database
        init_db()

        # Render and most web-service hosts require an HTTP port to be open.
        health_task = asyncio.create_task(run_health_server())
        
        # Create Telegram client
        client = TelegramClient('bot_session', API_ID, API_HASH)
        await client.start(bot_token=TOKEN)
        
        # Set global bot instance for helpers and handlers
        set_global_bot(client)
        
        # Register event handlers
        register_handlers(client)

        asyncio.create_task(license_notification_task(client))
        
        logger.info("✅ Bot started successfully!")
        logger.info(f"👤 Bot username: @{(await client.get_me()).username}")
        logger.info("🔍 Listening for events...")
        
        # Run until disconnected
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"حدث خطأ جسيم: {str(e)}")
    finally:
        if client and client.is_connected():
            await client.disconnect()
        if health_task:
            health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await health_task

if __name__ == '__main__':
    asyncio.run(main())
