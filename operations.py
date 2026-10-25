"""Operational services shared by the bot handlers and background workers."""
import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
import workflow
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict

from config import (
    BACKUP_DIR,
    BACKUP_RETENTION_DAYS,
    DATABASE_URL,
    PG_DUMP_PATH,
)
from helpers import execute_query

logger = logging.getLogger(__name__)
_backup_in_progress = False


def backup_connection_environment(dsn=None):
    """Translate the application's libpq DSN into child-only libpq variables.

    PGDATABASE alone is a database name, not a reliable way to pass a full
    connection URI. Parsing with libpq also handles percent-encoded passwords
    and quoted keyword/value connection strings correctly.
    """
    dsn = DATABASE_URL if dsn is None else dsn
    if not dsn:
        raise RuntimeError("Database backup requires DATABASE_URL.")
    try:
        parameters = conninfo_to_dict(dsn)
    except Exception:
        raise RuntimeError("Invalid DATABASE_URL for database backup.") from None
    variable_names = {
        option.keyword.decode(): option.envvar.decode()
        for option in pq.Conninfo.get_defaults() if option.envvar
    }
    environment = os.environ.copy()
    for name, value in parameters.items():
        variable = variable_names.get(name)
        if variable is None:
            # Do not silently drop connection settings or expose secrets such
            # as sslpassword in process arguments or exception messages.
            raise RuntimeError(f"Database backup cannot pass connection option {name} through the environment.")
        environment[variable] = value
    # Match the application's bounded connection establishment.
    environment["PGCONNECT_TIMEOUT"] = "5"
    return environment


def find_pg_dump():
    """Find pg_dump on Linux/macOS PATH or a normal PostgreSQL Windows install."""
    if PG_DUMP_PATH and Path(PG_DUMP_PATH).is_file():
        return PG_DUMP_PATH
    executable = shutil.which("pg_dump")
    if executable:
        return executable
    if os.name == "nt":
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL"
        candidates = sorted(root.glob("*/bin/pg_dump.exe"), reverse=True)
        if candidates:
            return str(candidates[0])
    raise FileNotFoundError(
        "pg_dump was not found. Set PG_DUMP_PATH to the full pg_dump executable path."
    )


async def create_database_backup():
    """Create a PostgreSQL custom-format dump using the server client utility."""
    destination_dir = Path(BACKUP_DIR)
    destination_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(destination_dir, 0o700)
    destination = destination_dir / f"bot-{datetime.now():%Y%m%d-%H%M%S-%f}.dump"
    pg_dump = find_pg_dump()
    # PGDATABASE, PGUSER, PGPASSWORD, SSL settings, etc. are derived from the
    # same connection string used by the application, only for this child.
    environment = backup_connection_environment()
    def backup():
        try:
            subprocess.run(
                [
                    pg_dump,
                    "--no-password",
                    "--format=custom",
                    f"--file={destination}",
                ],
                check=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=environment,
                timeout=600,
            )
        except subprocess.CalledProcessError:
            # stderr may contain server/connection details. Keep credentials
            # out of the exception logged by the background worker.
            raise RuntimeError(
                "Database backup failed. Check DATABASE_URL credentials, database access, "
                "and pg_dump/server version compatibility. Interactive password input is disabled."
            ) from None
    await asyncio.get_running_loop().run_in_executor(None, backup)
    with contextlib.suppress(OSError):
        os.chmod(destination, 0o600)
    await prune_old_backups()
    logger.info("Database backup created successfully")
    return destination


@workflow.tracked('backup', owner='developer_id', cancellable=False)
async def create_and_send_database_backup(client, developer_id):
    """Send a data-only recovery bundle; encryption keys remain off-backup."""
    global _backup_in_progress
    if _backup_in_progress:
        raise RuntimeError("A database backup is already in progress")
    _backup_in_progress = True
    backup_id = None
    backup_path = None
    try:
        if workflow.enabled:
            row = await execute_query("INSERT INTO backup_runs DEFAULT VALUES RETURNING backup_id", commit=True, fetchone=True)
            backup_id = row[0]
        backup_path = await create_database_backup()
        if backup_id:
            await execute_query("UPDATE backup_runs SET status='success',path=?,size_bytes=? WHERE backup_id=?",
                                (str(backup_path.resolve()),backup_path.stat().st_size,backup_id), commit=True)
        with tempfile.TemporaryDirectory(prefix="bot-recovery-") as temp_dir:
            temp_path = Path(temp_dir)
            instructions_path = temp_path / "RESTORE.txt"
            bundle_path = temp_path / f"bot-recovery-{datetime.now():%Y%m%d-%H%M%S}.tar"

            instructions_path.write_text(
                "Restore database.dump with pg_restore. Before starting the bot, "
                "obtain SESSION_ENCRYPTION_KEY from the server's secure environment "
                "or secret store. This backup intentionally does not contain keys.\n",
                encoding="utf-8",
            )
            with contextlib.suppress(OSError):
                os.chmod(instructions_path, 0o600)

            def build_bundle():
                # PostgreSQL custom dumps are already compressed; a plain TAR
                # avoids recompressing a potentially large database.
                with tarfile.open(bundle_path, "w") as archive:
                    archive.add(backup_path, arcname="database.dump")
                    archive.add(instructions_path, arcname="RESTORE.txt")
                os.chmod(bundle_path, 0o600)

            build_bundle()
            await client.send_file(
                developer_id,
                str(bundle_path),
                caption=(
                    f"🔐 حزمة استعادة كاملة\n📅 {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                    "تحتوي بيانات قاعدة البيانات وتعليمات الاستعادة فقط."
                ),
            )
        logger.info("Database backup sent to developer %s", developer_id)
        if backup_id:
            await execute_query('UPDATE backup_runs SET delivered=TRUE WHERE backup_id=?', (backup_id,), commit=True)
        return backup_path
    except Exception:
        if backup_id:
            message = 'فشل إرسال النسخة؛ الملف محفوظ على الخادم.' if backup_path else 'فشل إنشاء النسخة؛ راجع إعدادات قاعدة البيانات وأداة pg_dump.'
            await execute_query("UPDATE backup_runs SET status=?,error=? WHERE backup_id=?",
                                ('success' if backup_path else 'failed',message,backup_id), commit=True)
            await workflow.queue_notice(developer_id, f'backup-failed:{backup_id}', message)
        raise
    finally:
        _backup_in_progress = False


async def prune_old_backups():
    threshold = datetime.now() - timedelta(days=BACKUP_RETENTION_DAYS)
    for backup in Path(BACKUP_DIR).glob("bot-*.dump"):
        if datetime.fromtimestamp(backup.stat().st_mtime) < threshold:
            backup.unlink(missing_ok=True)


async def get_operational_stats():
    queries = {
        "users": "SELECT COUNT(*) FROM users",
        "active_today": "SELECT COUNT(*) FROM users WHERE last_active >= CURRENT_TIMESTAMP - INTERVAL '1 day'",
        "open_tickets": "SELECT COUNT(*) FROM support_tickets WHERE status IN ('open', 'in_progress')",
        "active_auctions": "SELECT COUNT(*) FROM auctions WHERE status='active'",
        "posting_tasks": "SELECT COUNT(*) FROM active_posting_tasks WHERE active=1",
        "withdrawals": "SELECT COUNT(*) FROM withdrawal_requests WHERE status='pending'",
    }
    result = {}
    for name, query in queries.items():
        row = await execute_query(query, fetchone=True)
        result[name] = row[0] if row else 0
    size_row = await execute_query("SELECT pg_database_size(current_database())", fetchone=True)
    result["database_size"] = size_row[0] if size_row else 0
    return result


async def create_support_ticket(user_id, message):
    from support_center import create
    return await create(user_id, message)


async def close_support_ticket(ticket_id, admin_id):
    from support_center import change
    await change(admin_id, ticket_id, 'close')


async def get_user_activity(user_id, limit=12):
    return await execute_query(
        "SELECT action, created_at FROM audit_log WHERE user_id=? ORDER BY event_id DESC LIMIT ?",
        (user_id, limit), fetchall=True,
    ) or []


async def expire_auctions(client):
    """Close expired auctions and notify their seller and winning bidder."""
    auctions = await execute_query(
        "SELECT auction_id, phone, seller_id, current_bid, current_bidder FROM auctions "
        "WHERE status='active' AND end_time IS NOT NULL AND end_time<=CURRENT_TIMESTAMP",
        fetchall=True,
    ) or []
    for auction_id, phone, seller_id, bid, bidder in auctions:
        # This worker is the sole closer; repeat the active condition for safety.
        await execute_query(
            "UPDATE auctions SET status='ended' WHERE auction_id=? AND status='active'",
            (auction_id,), commit=True,
        )
        try:
            from outbound_queue import queue_broadcast
            if bidder:
                await queue_broadcast([
                    bidder, seller_id
                ], f"🏷️ انتهى المزاد #{auction_id} للرقم +{phone} بأعلى عرض {bid}$.")
            else:
                await queue_broadcast([seller_id], f"🏷️ انتهى المزاد #{auction_id} للرقم +{phone} بدون عروض.")
        except Exception:
            logger.exception("Failed to notify auction completion: %s", auction_id)
