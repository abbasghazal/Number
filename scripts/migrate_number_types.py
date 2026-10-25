"""Back up and migrate; optionally copy legacy prices to ONE explicitly chosen pair."""

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", type=int, help="Country ID for optional legacy price import")
    parser.add_argument("--type", choices=["spam", "special", "clean", "guaranteed"])
    parser.add_argument("--actor", type=int, help="Configured finance administrator ID")
    parser.add_argument("--warranty-days", type=int)
    parser.add_argument("--coverage")
    parser.add_argument("--compensation")
    args = parser.parse_args()
    if any((args.country, args.type, args.actor)) and not all((args.country, args.type, args.actor)):
        parser.error("--country, --type and --actor must be supplied together")
    from migration_backup import ensure_backup
    from database import init_db
    from postgres import connect, close_pool
    from number_catalog import configure_sync

    try:
        backup = await ensure_backup()
        if backup:
            print("Pre-migration backup:", backup)
        init_db()
        if args.country:
            with connect() as db:
                row = db.execute(
                    "SELECT price,sell_price,stars_price FROM countries WHERE country_id=?", (args.country,)
                ).fetchone()
            if not row:
                raise ValueError("Country ID does not exist")
            policy = (
                {"days": args.warranty_days, "coverage": args.coverage, "compensation": args.compensation}
                if args.warranty_days
                else None
            )
            configure_sync(args.actor, args.country, args.type, row[0], row[1], True, True, policy, row[2])
            print("Imported legacy prices for the selected country/type only. Inventory still requires classification.")
        else:
            print("Migration complete. Requested per-type dollar defaults are installed; later administrator edits are preserved. Unclassified numbers still require review.")
    finally:
        close_pool()


if __name__ == "__main__":
    asyncio.run(main())
