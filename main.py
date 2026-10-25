import asyncio
import contextlib
import datetime
import logging
import os
import signal
from telethon import TelegramClient
from config import ADMIN_ID, API_ID, API_HASH, TOKEN, LOG_LEVEL, validate_config
from database import init_db
from postgres import close_pool
from outbound_queue import start_outbound_queue, stop_outbound_queue
from account_health import monitor as monitor_account_health, stop_manual_checks
import workflow
import service_health
from handlers import register_handlers
from helpers import (
    set_global_bot,
    migrate_sensitive_data,
    migrate_user_scoped_control_data,
    recover_stale_operations,
    ensure_all_accounts_in_mandatory_channel,
    register_auto_engagement_listener,
    resume_active_posting_tasks,
    stop_posting_workers,
)

# إعداد نظام تسجيل الأخطاء
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=getattr(logging, LOG_LEVEL, logging.INFO)
)
logger = logging.getLogger(__name__)


async def run_health_server():
    from miniapp_server import serve
    await serve()

async def background_tasks(client):
    from operations import create_and_send_database_backup, expire_auctions
    from helpers import expire_account_reservations, get_setting, set_setting
    from mandatory_subscription import resume_count_deliveries
    while True:
        service_health.tick('background')
        failed = False
        try:
            await ensure_all_accounts_in_mandatory_channel()
        except Exception:
            failed = True
            logger.exception("Mandatory-channel background task failed")
        try:
            await resume_count_deliveries(client)
        except Exception:
            failed = True
            logger.exception("Mandatory count-package worker failed")
        try:
            await expire_auctions(client)
        except Exception:
            failed = True
            logger.exception("Auction expiration task failed")
        try:
            await expire_account_reservations()
        except Exception:
            failed = True
            logger.exception("Account reservation cleanup failed")
        today = datetime.date.today().isoformat()
        backup_enabled = await get_setting("daily_backup_enabled")
        last_backup_date = await get_setting("last_daily_backup_date")
        if backup_enabled != "0" and last_backup_date != today:
            try:
                await create_and_send_database_backup(client, ADMIN_ID)
                await set_setting("last_daily_backup_date", today)
            except Exception:
                failed = True
                logger.exception("Daily database backup or delivery failed")
        service_health.tick('background', failed)
        await asyncio.sleep(60 * 60)

async def ton_payment_monitor_task(client):
    """Poll TON confirmations and recover direct Stars orders every minute."""
    from handlers.payment_handlers import monitor_ton_payments
    from stars_purchase import PaymentApiError, recover, reconcile
    while True:
        failed = False
        try:
            await recover(client)
            await reconcile(client)
        except PaymentApiError as exc:
            failed = True
            logger.warning('Payment API retry scheduled: %s (%s)', exc.message, exc.cause or 'Telegram response')
        except Exception:
            failed = True
            logger.exception('Direct Stars purchase monitor failed')
        try:
            await monitor_ton_payments(client)
            service_health.tick('payments', failed)
        except Exception:
            service_health.tick('payments', True)
            logger.exception("TON payment monitor failed")
        await asyncio.sleep(60)


async def twofa_rotation_monitor_task():
    """Apply new requests promptly and reconcile all existing account sources."""
    from helpers import process_due_twofa_changes
    from managed_twofa import expedite_legacy_requests, reconcile
    from utils.asyncio_compat import to_thread
    prepared = False
    next_scan = 0
    while True:
        try:
            if not prepared:
                await to_thread(expedite_legacy_requests)
                prepared = True
            completed, failed = await process_due_twofa_changes()
            if asyncio.get_running_loop().time() >= next_scan:
                old_completed, old_failed = await reconcile()
                completed += old_completed
                failed += old_failed
                next_scan = asyncio.get_running_loop().time() + 60
            service_health.tick('twofa')
            if completed or failed:
                logger.info("Scheduled 2FA rotations: %s completed, %s deferred", completed, failed)
        except Exception:
            service_health.tick('twofa', True)
            logger.exception("Scheduled 2FA rotation monitor failed")
        await asyncio.sleep(5)

async def platform_monitor_task(client):
    from platform_services import rental_reminders
    while True:
        try:
            await rental_reminders()
            from helpers import expire_username_auction_escrows
            try:
                await expire_username_auction_escrows()
            except Exception:
                logger.exception('Username-auction escrow expiry deferred')
            from miniapp_server import cleanup_expired_platform_data
            try:
                await cleanup_expired_platform_data()
            except Exception:
                logger.exception('Platform data cleanup deferred')
            from deferred_purchase import monitor as settle_deferred_purchases
            await settle_deferred_purchases()
            await workflow.deliver_notices(client)
            from handlers.operations_center import run_scheduled_operations
            await run_scheduled_operations(client)
            from automation import dispatch as dispatch_automation
            await dispatch_automation()
            service_health.tick('platform')
        except Exception:
            service_health.tick('platform', True)
            logger.exception('Platform notification monitor failed')
        await asyncio.sleep(60)


async def promotion_monitor_task():
    import promotion_service
    import code_monitor
    import funding_service
    import funding_auto_collect
    while True:
        try:
            await promotion_service.dispatch()
            await funding_service.dispatch(service_health.client)
            await funding_auto_collect.dispatch()
            await code_monitor.tick()
            service_health.tick('promotions')
        except Exception:
            service_health.tick('promotions', True)
            logger.exception('Promotion and code monitoring deferred')
        await asyncio.sleep(10)


async def storefront_monitor_task(client):
    from storefront import monitor_watches
    from miniapp_server import configure_menu
    configured = False
    while True:
        failed = False
        try:
            await monitor_watches()
        except Exception:
            failed = True
            logger.exception('Storefront subscriptions check failed')
        if not configured:
            try:
                configured = await configure_menu(client)
            except Exception:
                failed = True
                logger.exception('Mini App menu configuration deferred')
        service_health.tick('storefront', failed)
        await asyncio.sleep(60)


async def main():
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    for signo in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signo, main_task.cancel)
    client = None
    health_task = None
    background_task = None
    ton_monitor_task = None
    twofa_monitor_task = None
    health_monitor_task = None
    platform_task = None
    storefront_task = None
    service_monitor_task = None
    bio_task = None
    promotion_task = None
    try:
        validate_config()
        # Secrets and the bot session must not be readable by other local users.
        session_path = os.getenv('BOT_SESSION_PATH', 'bot_session')
        os.makedirs(os.path.dirname(os.path.abspath(session_path)), exist_ok=True)
        for private_path in (".env", session_path + '.session'):
            if os.path.exists(private_path):
                os.chmod(private_path, 0o600)
        # Initialize the database
        from migration_backup import ensure_backup
        await ensure_backup()
        init_db()
        migrate_sensitive_data()
        migrate_user_scoped_control_data()
        await recover_stale_operations()
        await workflow.initialize()
        from automation import recover as recover_automation
        await recover_automation()
        from promotion_service import recover as recover_promotions
        from code_monitor import recover as recover_codes
        from funding_auto_collect import recover as recover_funding_auto
        await recover_promotions()
        await recover_codes()
        await recover_funding_auto()

        # Create Telegram client
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.start(bot_token=TOKEN)
        from ui_config import style_transport
        style_transport(client)
        client._miniapp_username = (await client.get_me()).username
        service_health.client = client
        health_task = asyncio.create_task(run_health_server())
        
        # Set global bot instance for helpers and handlers
        set_global_bot(client)
        start_outbound_queue(client)
        
        # Register event handlers
        register_handlers(client)
        register_auto_engagement_listener(client)
        await resume_active_posting_tasks()

        # تشغيل المهام الخلفية الأساسية
        background_task = asyncio.create_task(background_tasks(client))
        ton_monitor_task = asyncio.create_task(ton_payment_monitor_task(client))
        twofa_monitor_task = asyncio.create_task(twofa_rotation_monitor_task())
        health_monitor_task = asyncio.create_task(monitor_account_health(client), name="account-health-monitor")
        platform_task = asyncio.create_task(platform_monitor_task(client), name='platform-notices')
        storefront_task = asyncio.create_task(storefront_monitor_task(client), name='storefront-monitor')
        from account_branding import monitor as monitor_account_bio
        bio_task = asyncio.create_task(monitor_account_bio(), name='account-bio-monitor')
        promotion_task = asyncio.create_task(promotion_monitor_task(), name='promotion-monitor')
        service_health.register('promotions', promotion_task)
        for name, task in [('background', background_task), ('payments', ton_monitor_task), ('twofa', twofa_monitor_task),
                           ('account_health', health_monitor_task), ('platform', platform_task),
                           ('storefront', storefront_task), ('account_bio', bio_task)]:
            service_health.register(name, task)
        service_health.register('http', health_task)
        service_health.ready = True
        service_monitor_task = asyncio.create_task(service_health.monitor(), name='service-monitor')
        
        logger.info("✅ Bot started successfully!")
        logger.info(f"👤 Bot username: @{(await client.get_me()).username}")
        logger.info("🔍 Listening for events...")
        
        # Run until disconnected
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        logger.info('Shutdown requested; stopping workers cleanly')
    except Exception:
        logger.exception("حدث خطأ جسيم أثناء بدء البوت")
    finally:
        service_health.ready = False
        from recurring_jobs import stop as stop_schedules
        for task in (service_monitor_task, storefront_task, bio_task, promotion_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if platform_task:
            platform_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await platform_task
        await stop_schedules()
        from automation import stop as stop_automation
        from search_join import stop as stop_search
        from shahm_service import stop as stop_shahm
        from promotion_service import stop as stop_promotions
        from code_monitor import stop as stop_codes
        from funding_auto_collect import stop as stop_funding_auto
        await stop_promotions()
        await stop_codes()
        await stop_funding_auto()
        await stop_shahm()
        await stop_automation()
        await stop_search()
        if health_monitor_task:
            health_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await health_monitor_task
        await stop_manual_checks()
        await stop_posting_workers()
        if client and client.is_connected():
            await client.disconnect()
        if health_task:
            health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await health_task
        if background_task:
            background_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await background_task
        if ton_monitor_task:
            ton_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ton_monitor_task
        if twofa_monitor_task:
            twofa_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await twofa_monitor_task
        await stop_outbound_queue()
        close_pool()

if __name__ == '__main__':
    asyncio.run(main())
