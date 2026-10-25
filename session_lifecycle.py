"""Retire a confirmed revoked installation by authorization key, never by phone alone."""

import logging

from postgres import connect
from utils.asyncio_compat import to_thread

LOG = logging.getLogger(__name__)
REVOKED_ERRORS = frozenset(
    {
        "AuthKeyInvalidError",
        "AuthKeyUnregisteredError",
        "AuthKeyDuplicatedError",
        "SessionExpiredError",
        "SessionRevokedError",
        "UnauthorizedError",
    }
)


def migrate(c):
    c.execute("""CREATE TABLE IF NOT EXISTS account_session_retirements(
        phone TEXT NOT NULL,session_key TEXT NOT NULL,reason TEXT NOT NULL,
        removed_rows INTEGER NOT NULL,retired_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(phone,session_key))""")


def require_fresh_session(db, session):
    from account_health import session_key

    if db.execute(
        "SELECT 1 FROM account_session_retirements WHERE session_key=? LIMIT 1", (session_key(session),)
    ).fetchone():
        raise ValueError("الجلسة القديمة منتهية. أنشئ جلسة جديدة صالحة لإضافة الحساب مجددًا.")


def clear_operational_tasks(db, phone, *, reason="session_retired"):
    """Remove phone-bound execution state without deleting financial history.

    Phone numbers are reused after a sale or after Telegram revokes an
    authorization.  Anything executable that is keyed only by ``phone`` must
    therefore be cleared before the number can be installed/listed again;
    otherwise a fresh owner/session could inherit the previous owner's jobs.
    """
    phone = str(phone)
    db.execute("DELETE FROM account_management_grants WHERE phone=?", (phone,))
    db.execute("DELETE FROM promotion_pool WHERE phone=?", (phone,))
    db.execute("DELETE FROM funding_auto_accounts WHERE phone=?", (phone,))
    db.execute(
        "UPDATE funding_auto_jobs SET status='failed',result=?,updated_at=CURRENT_TIMESTAMP "
        "WHERE phone=? AND status IN ('pending','executing')",
        (reason, phone),
    )
    db.execute(
        "UPDATE funding_auto_memberships SET status='closed',last_result=?,lease_until=NULL,last_checked=CURRENT_TIMESTAMP "
        "WHERE phone=? AND status='active'",
        (reason, phone),
    )
    db.execute("DELETE FROM super_account_targets WHERE phone=?", (phone,))
    db.execute("DELETE FROM active_posting_tasks WHERE phone=?", (phone,))

    # Automation rules and per-account job assignments are operational state.
    db.execute("DELETE FROM automation_rules WHERE phone=?", (phone,))
    jobs = db.execute(
        "DELETE FROM automation_job_accounts WHERE phone=? RETURNING job_id", (phone,)
    ).fetchall()
    if jobs:
        db.execute(
            """UPDATE automation_jobs j SET status='cancelled',cancel_requested=TRUE
            WHERE job_id=ANY(?) AND status IN ('paused','running','scheduled','pending')
            AND NOT EXISTS(SELECT 1 FROM automation_job_accounts a WHERE a.job_id=j.job_id)""",
            ([r[0] for r in jobs],),
        )
    db.execute("DELETE FROM automation_events WHERE phone=?", (phone,))
    db.execute("DELETE FROM automation_manual WHERE phone=?", (phone,))
    db.execute("DELETE FROM automation_waits WHERE phone=?", (phone,))

    # Search/join result rows are task state.  Ask any still-running parent job
    # to stop cooperatively before removing the account-specific result.
    search_jobs = db.execute(
        "SELECT DISTINCT job_id FROM search_join_results WHERE phone=?", (phone,)
    ).fetchall()
    if search_jobs:
        db.execute(
            "UPDATE workflow_jobs SET cancel_requested=TRUE WHERE job_id=ANY(?) AND status='running'",
            ([r[0] for r in search_jobs],),
        )
    db.execute("DELETE FROM search_join_results WHERE phone=?", (phone,))

    # Paid promotion order/account rows are kept for accounting, but an
    # unfinished execution must never continue with a replacement session.
    db.execute(
        """UPDATE promotion_items SET status='failed',result=?,updated_at=CURRENT_TIMESTAMP
        WHERE phone=? AND status IN ('pending','checking','executing','unknown')""",
        (reason, phone),
    )
    db.execute("DELETE FROM promotion_slots WHERE phone=?", (phone,))
    db.execute("DELETE FROM monitored_code_accounts WHERE phone=?", (phone,))

    # Miscellaneous account-scoped workers.
    db.execute("DELETE FROM active_accounts WHERE phone=?", (phone,))
    # During a completed sale the managed 2FA rotation belongs to the bot's
    # intake flow and must be allowed to finish on the newly-created inventory
    # row.  A revoked session, on the other hand, can never complete it.
    if reason != "resold":
        db.execute("DELETE FROM scheduled_twofa_changes WHERE phone=?", (phone,))
    db.execute("DELETE FROM account_bio_state WHERE phone=?", (phone,))
    db.execute("UPDATE code_monitor_config SET enabled=FALSE,monitor_phone=NULL WHERE monitor_phone=?", (phone,))


def retire_sync(session, *, phone=None, reason="not_authorized"):
    from account_health import session_key
    from helpers import decrypt_session
    from managed_twofa import state_key
    from workflow import enqueue

    if not session or reason not in REVOKED_ERRORS | {"not_authorized", "revoked"}:
        return False
    fingerprint = session_key(session)
    target_keys = {fingerprint}

    def matches(encrypted):
        value = decrypt_session(encrypted) if encrypted else None
        return bool(value and session_key(value) in target_keys)

    with connect() as db:
        condition = " WHERE phone=?" if phone is not None else ""
        params = (str(phone),) if phone is not None else ()
        candidates = db.execute(
            "SELECT phone,session FROM stored_accounts"
            + condition
            + " UNION ALL SELECT phone,session FROM purchased_accounts"
            + condition
            + " UNION ALL SELECT phone,session FROM accounts"
            + condition
            + " UNION ALL SELECT phone,session FROM pending_sales"
            + condition,
            params + params + params + params,
        ).fetchall()
        phones = sorted({r[0] for r in candidates if matches(r[1])})
        retired = bool(
            db.execute(
                "SELECT 1 FROM account_session_retirements WHERE session_key=?"
                + (" AND phone=?" if phone is not None else "")
                + " LIMIT 1",
                (fingerprint, *params),
            ).fetchone()
        )
        for current_phone in phones:
            # Intake and ownership changes use this same per-phone transaction lock.
            db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (current_phone,))
            target_keys = {fingerprint}
            copies = {
                table: db.execute(
                    f"SELECT session,user_id FROM {table} WHERE phone=? FOR UPDATE", (current_phone,)
                ).fetchone()
                for table in ("stored_accounts", "purchased_accounts")
            }
            installed, purchased = copies["stored_accounts"], copies["purchased_accounts"]
            pending_sale = db.execute(
                "SELECT session,seller_id FROM pending_sales WHERE phone=? FOR UPDATE", (current_phone,)
            ).fetchone()
            if purchased and matches(purchased[0]) and installed and installed[1] != purchased[1]:
                # A purchased owner already superseded this former owner's hidden
                # installation. Removing the current row must not revive that grant.
                former = decrypt_session(installed[0])
                if former:
                    target_keys.add(session_key(former))
            owners, removed = set(), 0
            if pending_sale and matches(pending_sale[0]) and pending_sale[1]:
                owners.add(pending_sale[1])
            for table in ("stored_accounts", "purchased_accounts"):
                row = copies[table]
                if row and matches(row[0]):
                    db.execute(f"DELETE FROM {table} WHERE phone=?", (current_phone,))
                    removed += 1
                    if row[1] and session_key(decrypt_session(row[0])) == fingerprint:
                        owners.add(row[1])
            # Remove only operational copies containing the same dead authorization.
            for table, identity in (
                ("pending_sales", "phone"),
                ("pending_purchases", "purchase_id"),
                ("active_accounts", "phone"),
                ("scheduled_twofa_changes", "phone"),
            ):
                rows = db.execute(
                    f"SELECT {identity},session FROM {table} WHERE phone=? FOR UPDATE", (current_phone,)
                ).fetchall()
                for item, encrypted in rows:
                    if matches(encrypted):
                        db.execute(f"DELETE FROM {table} WHERE {identity}=?", (item,))
                        removed += 1
            archive = db.execute(
                "SELECT session FROM sold_accounts WHERE phone=? FOR UPDATE", (current_phone,)
            ).fetchone()
            if archive and matches(archive[0]):
                db.execute("UPDATE sold_accounts SET session=NULL WHERE phone=?", (current_phone,))
            auctions = db.execute(
                "SELECT auction_id,session FROM auctions WHERE phone=? FOR UPDATE", (current_phone,)
            ).fetchall()
            for auction, encrypted in auctions:
                if matches(encrypted):
                    # Bids are charged only on settlement; a cancelled auction cannot settle.
                    db.execute(
                        "UPDATE auctions SET session=NULL,twofa=NULL,status='cancelled' WHERE auction_id=?", (auction,)
                    )

            inventory = db.execute("SELECT session FROM accounts WHERE phone=? FOR UPDATE", (current_phone,)).fetchone()
            if inventory and matches(inventory[0]):
                # Lease and sale history is independent of the live inventory row.
                db.execute(
                    "UPDATE rental_leases SET status='cancelled' WHERE phone=? AND status='active'", (current_phone,)
                )
                db.execute(
                    "UPDATE deferred_purchases SET status='cancelled',last_error='session_revoked' WHERE phone=? AND status='pending'",
                    (current_phone,),
                )
                db.execute("DELETE FROM account_reservations WHERE phone=?", (current_phone,))
                db.execute("DELETE FROM accounts WHERE phone=?", (current_phone,))
                removed += 1
            star_orders = db.execute(
                "SELECT order_id,inventory_session FROM star_purchase_orders WHERE phone=? FOR UPDATE", (current_phone,)
            ).fetchall()
            for order, encrypted in star_orders:
                if matches(encrypted):
                    db.execute(
                        """UPDATE star_purchase_orders SET inventory_session='',last_error='session_revoked',
                        status=CASE WHEN status IN ('pending','checkout') THEN 'cancelled'
                        WHEN status='paid' THEN 'refund_pending' ELSE status END WHERE order_id=?""",
                        (order,),
                    )

            if not removed:
                retired = retired or bool(
                    db.execute(
                        "SELECT 1 FROM account_session_retirements WHERE phone=? AND session_key=?",
                        (current_phone, fingerprint),
                    ).fetchone()
                )
                continue  # A fresh session replaced the scanned authorization.

            # A new authorization for this phone must never inherit the old
            # authorization's permissions or tasks, even when another live
            # copy for the same phone exists temporarily.
            clear_operational_tasks(db, current_phone, reason="session_revoked")
            db.execute(
                "DELETE FROM managed_twofa_state WHERE phone=? AND session_key=?",
                (current_phone, state_key(current_phone, session)),
            )
            db.execute("UPDATE account_health SET inventory_session=NULL WHERE session_key=?", (fingerprint,))
            db.execute(
                """INSERT INTO account_session_retirements(phone,session_key,reason,removed_rows)
                VALUES(?,?,?,?) ON CONFLICT DO NOTHING""",
                (current_phone, fingerprint, reason, removed),
            )
            for owner in owners:
                enqueue(
                    db,
                    owner,
                    f"session-retired:{current_phone}:{fingerprint}",
                    f"انتهت صلاحية جلسة الحساب +{current_phone} وأُزيل تسجيلها القديم وبيانات تشغيلها. يمكنك تنصيب الحساب أو تقديمه للبيع مجددًا بجلسة جديدة صالحة.",
                )
                db.execute(
                    "INSERT INTO audit_log(user_id,action) VALUES(?,?)",
                    (owner, f"account_session_retired:{current_phone}:{fingerprint}"),
                )
            retired = True
        return retired


async def retire(session, *, phone=None, reason="not_authorized"):
    return await to_thread(retire_sync, session, phone=phone, reason=reason)


async def on_rpc_error(session, error, *, phone=None):
    """Cleanup failure must not mask the Telegram error or turn a timeout into deletion."""
    reason = type(error).__name__
    if not session or reason not in REVOKED_ERRORS:
        return
    try:
        await retire(session, phone=phone, reason=reason)
    except Exception:
        LOG.warning("Revoked installation cleanup deferred to the health monitor")
