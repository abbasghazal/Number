import asyncio
import logging
from telethon import TelegramClient
from config import API_ID, API_HASH, TOKEN, DB_PATH
from database import init_db
from handlers import register_handlers
from helpers import set_global_bot

# إعداد نظام تسجيل الأخطاء
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def background_tasks():
    while True:
        await asyncio.sleep(3600)  # كل ساعة
        # يمكن إضافة مهام خلفية أخرى هنا

async def main():
    try:
        # Initialize the database
        init_db()
        
        # Create Telegram client
        client = TelegramClient('bot_session', API_ID, API_HASH)
        await client.start(bot_token=TOKEN)
        
        # Set global bot instance for handlers
        from handlers import bot as handlers_bot
        from config import bot as config_bot
        handlers_bot = client
        config_bot = client
        
        # Register event handlers
        register_handlers(client)
        
        logger.info("✅ Bot started successfully!")
        logger.info(f"👤 Bot username: @{(await client.get_me()).username}")
        logger.info("🔍 Listening for events...")
        
        # Run until disconnected
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"حدث خطأ جسيم: {str(e)}")

if __name__ == '__main__':
    asyncio.run(main())