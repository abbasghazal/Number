"""Apply the fixed 2FA policy now; report counts without exposing credentials."""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    from helpers import process_due_twofa_changes
    from managed_twofa import migrate, expedite_legacy_requests, reconcile, summary
    from postgres import connect, close_pool
    from utils.asyncio_compat import to_thread

    try:
        with connect() as db:
            migrate(db)
        await to_thread(expedite_legacy_requests)
        while True:
            # A pass includes both persisted requests and older account tables.
            completed, failed = await process_due_twofa_changes()
            old_completed, old_failed = await reconcile()
            print(json.dumps({'completed': completed + old_completed, 'deferred': failed + old_failed}), flush=True)
            if not any((completed, failed, old_completed, old_failed)):
                break
        print(json.dumps({'state_counts': dict(await summary())}), flush=True)
    finally:
        close_pool()


if __name__ == '__main__':
    asyncio.run(main())
