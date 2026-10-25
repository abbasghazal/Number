"""Back up existing installations before enabling the number-type migration."""

from postgres import connect
from utils.asyncio_compat import to_thread


def required_sync():
    with connect() as db:
        if not db.execute("SELECT to_regclass('accounts')").fetchone()[0]:
            return False
        if not db.execute("SELECT to_regclass('settings')").fetchone()[0]:
            return True
        return not db.execute("SELECT 1 FROM settings WHERE key='number_types_v1'").fetchone()


async def ensure_backup():
    if await to_thread(required_sync):
        from operations import create_database_backup

        # Failure stops startup before any migration or payment reconciliation.
        return await create_database_backup()
    return None
