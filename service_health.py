"""Readiness reflects database, Telegram and background-worker progress."""
import asyncio
import time
import logging

from config import ADMIN_ID
from workflow import query

logger = logging.getLogger(__name__)
client = None
ready = False
workers = {}
heartbeats = {}
errors = {}
limits = {'background': 3900, 'payments': 240, 'twofa': 240, 'account_health': 240,
          'platform': 240, 'storefront': 240}
_probe_cache = None
_probe_at = 0


def tick(name, error=None):
    heartbeats[name] = time.monotonic()
    errors[name] = bool(error)


def register(name, task):
    workers[name] = task
    tick(name)
    def report_failure(done):
        if not done.cancelled():
            error = done.exception()
            if error is not None:
                logger.error('Background worker %s stopped',name,exc_info=(type(error),error,error.__traceback__))
    task.add_done_callback(report_failure)


async def snapshot(*, fresh=False):
    global _probe_cache, _probe_at
    now = time.monotonic()
    if fresh or _probe_cache is None or now-_probe_at > 10:
        try:
            await asyncio.wait_for(query('SELECT 1', fetchone=True), 5)
            db_ok = True
        except Exception:
            db_ok = False
        _probe_cache, _probe_at = db_ok, time.monotonic()
    state = {}
    for name, task in workers.items():
        age = max(0, now-heartbeats.get(name, now))
        state[name] = {'ok': not task.done() and (name == 'http' or age <= limits.get(name, 240)) and not errors.get(name),
                       'age_seconds': round(age), 'failed': task.done() or bool(errors.get(name))}
    telegram = bool(client and client.is_connected())
    return {'ok': ready and bool(_probe_cache) and telegram and all(r['ok'] for r in state.values()),
            'database': bool(_probe_cache), 'telegram': telegram, 'ready': ready, 'workers': state}


async def monitor():
    last_state = None
    last_alert = 0
    while True:
        try:
            state = await snapshot(fresh=True)
            failing = [name for name, item in state['workers'].items() if not item['ok']]
            if not state['database']:
                failing.append('database')
            if not state['telegram']:
                failing.append('telegram')
            current = ','.join(sorted(failing))
            now = time.monotonic()
            if ready and current != last_state and (not current or now-last_alert >= 60):
                text = 'عادت الخدمة للعمل بصورة سليمة.' if not current else 'تنبيه تشغيل: تعطل أو تأخر ' + current
                if current or last_state:
                    try:
                        await asyncio.wait_for(client.send_message(ADMIN_ID, text, parse_mode=None), 15)
                    except Exception:
                        logger.warning('Operational alert delivery failed; will retry')
                    else:
                        last_state, last_alert = current, now
                else:
                    last_state = current
        except Exception:
            logger.exception('Service monitoring pass failed')
        await asyncio.sleep(30)
