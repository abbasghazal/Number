"""Developer-operated periodic point collection for bot-managed accounts.

Only the developer can start the collector.  Its pool is rebuilt from the
developer's accounts, unsold inventory, pending-sale accounts, and active
auction accounts, so the worker always follows the bot's current inventory.
"""

import asyncio
import logging

from telethon import errors, functions, types

from config import DEVELOPER_ID
import account_access
import automation
import promotion_store as store
from account_health import session_is_usable
from managed_twofa import state_key
from postgres import connect
from session_guard import SessionBusyError
from shahm_engine import parse_telegram_link
from helpers import decrypt_session
from workflow import query, enqueue

LOG = logging.getLogger(__name__)
_workers = {}


async def _collector_accounts():
    """Return every account source explicitly entrusted to the bot collector."""
    if not DEVELOPER_ID:
        return {}

    owner = int(DEVELOPER_ID)
    # Do not use generic management grants here: this pool is deliberately
    # limited to the developer's accounts and accounts currently in bot stock.
    result = {a[0]: a for a in await account_access.accounts(owner, owned_only=True)}
    rows = await query(
        """SELECT a.phone,a.session,NULL,?,'inventory' FROM accounts a
           WHERE NOT a.is_sold
             AND NOT EXISTS(SELECT 1 FROM stored_accounts s WHERE s.phone=a.phone)
             AND NOT EXISTS(SELECT 1 FROM purchased_accounts p WHERE p.phone=a.phone)
           UNION ALL
           SELECT phone,session,twofa,seller_id,'pending_sale' FROM pending_sales p
           WHERE NOT EXISTS(SELECT 1 FROM stored_accounts s WHERE s.phone=p.phone AND s.user_id<>p.seller_id)
             AND NOT EXISTS(SELECT 1 FROM purchased_accounts a WHERE a.phone=p.phone AND a.user_id<>p.seller_id)
           UNION ALL
           SELECT phone,session,twofa,seller_id,'auction' FROM auctions x
           WHERE status IN ('active','processing')
             AND NOT EXISTS(SELECT 1 FROM stored_accounts s WHERE s.phone=x.phone AND s.user_id<>x.seller_id)
             AND NOT EXISTS(SELECT 1 FROM purchased_accounts a WHERE a.phone=x.phone AND a.user_id<>x.seller_id)
        """,
        (owner,), fetchall=True,
    ) or []
    for phone, encrypted, encrypted_twofa, account_owner, source in rows:
        phone = str(phone)
        if phone in result:
            continue
        session = decrypt_session(encrypted)
        if session:
            result[phone] = (phone, session, int(account_owner), decrypt_session(encrypted_twofa), source)
    return result


async def collector_accounts():
    """Public read-only view of the central developer-controlled account pool."""
    return await _collector_accounts()


def migrate(c):
    for statement in [
        """CREATE TABLE IF NOT EXISTS funding_auto_config(
            owner_id BIGINT PRIMARY KEY,enabled BOOLEAN NOT NULL DEFAULT FALSE,
            batch_size INTEGER NOT NULL DEFAULT 15 CHECK(batch_size BETWEEN 1 AND 15),
            cooldown_seconds INTEGER NOT NULL DEFAULT 300 CHECK(cooldown_seconds>=60),
            joined_in_batch INTEGER NOT NULL DEFAULT 0,
            next_run_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_result TEXT,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS funding_auto_global_config(
            id INTEGER PRIMARY KEY CHECK(id=1),enabled BOOLEAN NOT NULL DEFAULT FALSE,
            updated_by BIGINT,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        "INSERT INTO funding_auto_global_config(id,enabled) VALUES(1,FALSE) ON CONFLICT DO NOTHING",
        """CREATE TABLE IF NOT EXISTS funding_auto_accounts(
            owner_id BIGINT NOT NULL,phone TEXT NOT NULL,session_key TEXT NOT NULL,account_owner_id BIGINT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,last_used_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(owner_id,phone))""",
        """CREATE TABLE IF NOT EXISTS funding_auto_jobs(
            job_id BIGSERIAL PRIMARY KEY,owner_id BIGINT NOT NULL,phone TEXT NOT NULL,session_key TEXT NOT NULL,
            campaign_id BIGINT NOT NULL REFERENCES funding_campaigns(campaign_id),target_id BIGINT NOT NULL,target TEXT NOT NULL,
            reward BIGINT NOT NULL,cost BIGINT NOT NULL,status TEXT NOT NULL DEFAULT 'pending'
              CHECK(status IN ('pending','executing','success','failed','unknown')),
            result TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(phone,campaign_id))""",
        "CREATE INDEX IF NOT EXISTS funding_auto_jobs_pending_idx ON funding_auto_jobs(status,created_at)",
        """CREATE TABLE IF NOT EXISTS funding_auto_memberships(
            membership_id BIGSERIAL PRIMARY KEY,owner_id BIGINT NOT NULL,phone TEXT NOT NULL,session_key TEXT NOT NULL,
            target_id BIGINT NOT NULL,target TEXT NOT NULL,campaign_id BIGINT NOT NULL,reward BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','penalized')),
            joined_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            next_check TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP+INTERVAL '5 minutes',
            lease_until TIMESTAMPTZ,last_checked TIMESTAMPTZ,last_result TEXT,penalized_at TIMESTAMPTZ,
            UNIQUE(phone,target_id))""",
        "CREATE INDEX IF NOT EXISTS funding_auto_membership_due_idx ON funding_auto_memberships(next_check) WHERE status='active'",
        "ALTER TABLE funding_auto_config ADD COLUMN IF NOT EXISTS auto_include_owned BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE funding_auto_accounts ADD COLUMN IF NOT EXISTS account_owner_id BIGINT",
        "UPDATE funding_auto_accounts SET account_owner_id=owner_id WHERE account_owner_id IS NULL",
        "ALTER TABLE funding_auto_accounts ALTER COLUMN account_owner_id SET NOT NULL",
        "ALTER TABLE funding_auto_memberships DROP CONSTRAINT IF EXISTS funding_auto_memberships_status_check",
        "ALTER TABLE funding_auto_memberships ADD CONSTRAINT funding_auto_memberships_status_check CHECK(status IN ('active','penalized','closed'))",
    ]:
        c.execute(statement)


async def _sync_collector_accounts():
    """Synchronize the central pool with the bot's current account sources."""
    if not DEVELOPER_ID:
        return {}
    owner = int(DEVELOPER_ID)
    cfg = await query(
        "SELECT enabled FROM funding_auto_config WHERE owner_id=?",
        (owner,), fetchone=True,
    )
    if not cfg or not cfg[0]:
        return {}
    accounts = await _collector_accounts()
    if accounts:
        await query(
            "UPDATE funding_auto_accounts SET enabled=FALSE,updated_at=CURRENT_TIMESTAMP "
            "WHERE owner_id=? AND NOT (phone=ANY(?))",
            (owner, list(accounts)), commit=True,
        )
    else:
        await query(
            "UPDATE funding_auto_accounts SET enabled=FALSE,updated_at=CURRENT_TIMESTAMP WHERE owner_id=?",
            (owner,), commit=True,
        )
    for phone, account in accounts.items():
        await query(
            """INSERT INTO funding_auto_accounts(owner_id,phone,session_key,account_owner_id,enabled) VALUES(?,?,?,?,TRUE)
               ON CONFLICT(owner_id,phone) DO UPDATE SET session_key=excluded.session_key,
               account_owner_id=excluded.account_owner_id,enabled=TRUE,updated_at=CURRENT_TIMESTAMP""",
            (owner, phone, state_key(phone, account[1]), account[2]), commit=True,
        )
    return accounts


async def global_status():
    row = await query(
        "SELECT enabled,updated_by,updated_at FROM funding_auto_global_config WHERE id=1",
        fetchone=True,
    )
    return row or (False, None, None)


async def configure_global(actor, enabled):
    """Developer-only master switch for the central bot account pool."""
    if not DEVELOPER_ID or int(actor) != int(DEVELOPER_ID):
        raise PermissionError("هذه العملية متاحة للمطور فقط.")
    owner = int(DEVELOPER_ID)
    await query(
        """INSERT INTO funding_auto_global_config(id,enabled,updated_by) VALUES(1,?,?)
           ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,updated_by=excluded.updated_by,
           updated_at=CURRENT_TIMESTAMP""",
        (bool(enabled), int(actor)), commit=True,
    )
    if enabled:
        await query(
            """INSERT INTO funding_auto_config(owner_id,enabled,batch_size,cooldown_seconds,next_run_at,last_result,auto_include_owned)
               VALUES(?,TRUE,15,300,CURRENT_TIMESTAMP,'developer_started',TRUE)
               ON CONFLICT(owner_id) DO UPDATE SET enabled=TRUE,next_run_at=CURRENT_TIMESTAMP,
               last_result='developer_started',updated_at=CURRENT_TIMESTAMP""",
            (owner,), commit=True,
        )
        await _sync_collector_accounts()
    else:
        await query(
            "UPDATE funding_auto_config SET enabled=FALSE,last_result='developer_stopped',updated_at=CURRENT_TIMESTAMP WHERE owner_id=?",
            (owner,), commit=True,
        )
        await query(
            "UPDATE funding_auto_jobs SET status='failed',result='collector_stopped',updated_at=CURRENT_TIMESTAMP WHERE owner_id=? AND status='pending'",
            (owner,), commit=True,
        )
        await stop()
    return await global_status()


async def configure(owner, phones, enabled, *, include_all_owned=False):
    """Backward-compatible entry point; operation is now developer-only."""
    if not DEVELOPER_ID or int(owner) != int(DEVELOPER_ID):
        raise PermissionError("التجميع الدوري يُدار من المطور فقط.")
    return await configure_global(owner, enabled)


async def status(owner):
    cfg = await query("SELECT enabled,joined_in_batch,batch_size,cooldown_seconds,next_run_at,last_result,auto_include_owned FROM funding_auto_config WHERE owner_id=?", (owner,), fetchone=True)
    rows = await query("SELECT phone,enabled,last_used_at FROM funding_auto_accounts WHERE owner_id=? ORDER BY phone", (owner,), fetchall=True) or []
    return cfg or (False,0,15,300,None,None,False), rows


async def _eligible_accounts(owner):
    if not DEVELOPER_ID or int(owner) != int(DEVELOPER_ID):
        return []
    await _sync_collector_accounts()
    rows = await query("""SELECT a.phone,a.session_key,a.account_owner_id FROM funding_auto_accounts a WHERE a.owner_id=? AND a.enabled AND NOT EXISTS(SELECT 1 FROM automation_waits w WHERE w.phone=a.phone AND w.until_at>CURRENT_TIMESTAMP) ORDER BY a.last_used_at NULLS FIRST,a.updated_at,a.phone""", (owner,), fetchall=True) or []
    owned = await _collector_accounts()
    out = []
    for phone, fingerprint, account_owner in rows:
        account = owned.get(phone)
        if account and account[2] == account_owner and state_key(phone, account[1]) == fingerprint and await session_is_usable(account[1]):
            out.append((account, fingerprint))
    return out


def _reserve(controller, phone, fingerprint, account_owner):
    with connect() as db:
        cfg = db.execute(
            """SELECT a.enabled,a.next_run_at<=CURRENT_TIMESTAMP,f.enabled,g.enabled
               FROM funding_auto_config a
               CROSS JOIN funding_config f
               CROSS JOIN funding_auto_global_config g
               WHERE a.owner_id=? AND f.id=1 AND g.id=1 FOR UPDATE OF a""",
            (controller,),
        ).fetchone()
        if not cfg or cfg != (True, True, True, True):
            return None
        if not db.execute("SELECT 1 FROM funding_auto_accounts WHERE owner_id=? AND phone=? AND session_key=? AND account_owner_id=? AND enabled FOR SHARE", (controller,phone,fingerprint,account_owner)).fetchone():
            return None
        existing = db.execute(
            """SELECT job_id,campaign_id,target_id,target,reward,cost FROM funding_auto_jobs
               WHERE phone=? AND session_key=? AND status='pending'
               ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED""",
            (phone,fingerprint),
        ).fetchone()
        if existing:
            job,campaign,target_id,target,reward,cost = existing
            return (job,campaign,target_id,target,reward,cost)
        row = db.execute(
            """SELECT c.campaign_id,c.target_id,c.target,c.reward,c.cost
               FROM funding_campaigns c
               WHERE c.status='active' AND c.delivered<c.quantity
                 AND NOT EXISTS(SELECT 1 FROM funding_auto_memberships m WHERE m.phone=? AND m.target_id=c.target_id)
                 AND NOT EXISTS(SELECT 1 FROM funding_auto_jobs j WHERE j.phone=? AND j.campaign_id=c.campaign_id)
                 AND c.delivered
                     +(SELECT COUNT(*) FROM funding_tasks t WHERE t.campaign_id=c.campaign_id AND (t.status='pending' OR (t.status='reserved' AND t.expires_at>CURRENT_TIMESTAMP)))
                     +(SELECT COUNT(*) FROM funding_auto_jobs j2 WHERE j2.campaign_id=c.campaign_id AND j2.status IN ('pending','executing'))
                     < c.quantity
               ORDER BY c.campaign_id
               LIMIT 1 FOR UPDATE SKIP LOCKED""",
            (phone, phone),
        ).fetchone()
        if not row:
            return None
        job = db.execute(
            """INSERT INTO funding_auto_jobs(owner_id,phone,session_key,campaign_id,target_id,target,reward,cost)
               VALUES(?,?,?,?,?,?,?,?) RETURNING job_id""",
            (account_owner, phone, fingerprint, *row),
        ).fetchone()[0]
        db.execute("UPDATE funding_auto_accounts SET last_used_at=CURRENT_TIMESTAMP WHERE owner_id=? AND phone=?", (controller,phone))
        return (job, *row)


def _finish_failure(job, result, unknown=False, retry=False):
    with connect() as db:
        status = 'pending' if retry else 'unknown' if unknown else 'failed'
        db.execute("UPDATE funding_auto_jobs SET status=?,result=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status IN ('pending','executing')", (status, result, job))


def _finish_success(job):
    with connect() as db:
        item = db.execute("SELECT owner_id,phone,session_key,campaign_id,target_id,target,reward,cost,status FROM funding_auto_jobs WHERE job_id=? FOR UPDATE", (job,)).fetchone()
        if not item or item[8] != 'executing':
            return False
        owner,phone,fingerprint,campaign,target_id,target,reward,cost,_ = item
        c = db.execute("SELECT quantity,delivered,status FROM funding_campaigns WHERE campaign_id=? FOR UPDATE", (campaign,)).fetchone()
        if not c or c[2] not in ('active','pausing') or c[1] >= c[0]:
            db.execute("UPDATE funding_auto_jobs SET status='failed',result='campaign_filled_after_join',updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (job,))
            return False
        inserted = db.execute(
            """INSERT INTO funding_auto_memberships(owner_id,phone,session_key,target_id,target,campaign_id,reward)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(phone,target_id) DO NOTHING RETURNING membership_id""",
            (owner,phone,fingerprint,target_id,target,campaign,reward),
        ).fetchone()
        if not inserted:
            db.execute("UPDATE funding_auto_jobs SET status='failed',result='membership_already_rewarded',updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (job,))
            return False
        store.point_change(db, owner, reward, 'auto_task_reward', f'auto:{job}')
        db.execute("INSERT INTO funding_margin(task_id,campaign_id,amount) VALUES(?,?,?) ON CONFLICT DO NOTHING", (f'auto:{job}',campaign,cost-reward))
        db.execute("UPDATE funding_campaigns SET delivered=delivered+1,status=CASE WHEN delivered+1>=quantity THEN 'completed' ELSE status END,updated_at=CURRENT_TIMESTAMP WHERE campaign_id=?", (campaign,))
        db.execute("UPDATE funding_auto_jobs SET status='success',result='confirmed',updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (job,))
        return True


async def _resolve_target(client, target, target_id, join=False):
    parsed = parse_telegram_link(target)
    if not parsed:
        raise ValueError('invalid_target')
    kind, value = parsed
    if kind == 'public':
        entity = await client.get_entity(value)
        if not isinstance(entity, types.Channel):
            raise ValueError('invalid_target')
        if join:
            try:
                await client(functions.channels.JoinChannelRequest(entity))
            except errors.UserAlreadyParticipantError:
                raise ValueError('already_member')
        return entity
    if join:
        try:
            updates = await client(functions.messages.ImportChatInviteRequest(value))
        except errors.UserAlreadyParticipantError:
            raise ValueError('already_member')
        entity = next((x for x in updates.chats if isinstance(x, types.Channel) and -1000000000000-x.id == target_id), None)
        if not entity:
            raise ValueError('target_not_confirmed')
        return entity
    invite = await client(functions.messages.CheckChatInviteRequest(value))
    entity = getattr(invite, 'chat', None)
    if not isinstance(entity, types.Channel):
        raise ValueError('target_unavailable')
    return entity


async def _member(client, entity):
    try:
        participant = (await client(functions.channels.GetParticipantRequest(entity, 'me'))).participant
        return not isinstance(participant, types.ChannelParticipantLeft) and not (isinstance(participant, types.ChannelParticipantBanned) and participant.left)
    except errors.UserNotParticipantError:
        return False


async def _membership_state(client, target, target_id):
    parsed = parse_telegram_link(target)
    if not parsed:
        raise ValueError('invalid_target')
    kind, value = parsed
    if kind == 'public':
        entity = await client.get_entity(value)
        if not isinstance(entity, types.Channel) or -1000000000000-entity.id != target_id:
            raise ValueError('target_changed')
        return await _member(client, entity)
    invite = await client(functions.messages.CheckChatInviteRequest(value))
    entity = getattr(invite, 'chat', None)
    if isinstance(entity, types.Channel):
        if -1000000000000-entity.id != target_id:
            raise ValueError('target_changed')
        return await _member(client, entity)
    if isinstance(invite, types.ChatInvite):
        return False
    raise ValueError('target_unavailable')


async def _perform(owner, account, fingerprint, reserved):
    job,campaign,target_id,target,reward,cost = reserved
    await query("UPDATE funding_auto_jobs SET status='executing',updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status='pending'", (job,), commit=True)
    sending = False
    try:
        if not DEVELOPER_ID or int(owner) != int(DEVELOPER_ID):
            raise PermissionError()
        current = (await _collector_accounts()).get(str(account[0]))
        if not current or current[1] != account[1] or current[2] != account[2] or state_key(account[0], account[1]) != fingerprint:
            raise PermissionError()
        enabled = await query("""SELECT 1 FROM funding_auto_config c JOIN funding_auto_accounts a ON a.owner_id=c.owner_id
            WHERE c.owner_id=? AND c.enabled AND a.phone=? AND a.session_key=?
              AND a.account_owner_id=? AND a.enabled""",
            (owner, account[0], fingerprint, account[2]), fetchone=True)
        if not enabled:
            raise PermissionError()
        async with automation.connection(account) as client:
            parsed = parse_telegram_link(target)
            if parsed and parsed[0] == 'public':
                entity = await client.get_entity(parsed[1])
                if await _member(client, entity):
                    raise ValueError('already_member')
            sending = True
            entity = await _resolve_target(client, target, target_id, join=True)
            if -1000000000000-entity.id != target_id or not await _member(client, entity):
                raise ValueError('membership_not_confirmed')
        return await asyncio.to_thread(_finish_success, job)
    except errors.FloodWaitError as exc:
        await automation.save_wait(account[0], exc.seconds + 1)
        await asyncio.to_thread(_finish_failure, job, 'flood_wait', sending, not sending)
    except SessionBusyError:
        await asyncio.to_thread(_finish_failure, job, 'account_busy', sending, not sending)
    except asyncio.CancelledError:
        await asyncio.to_thread(_finish_failure, job, 'interrupted', sending, not sending)
        raise
    except PermissionError as exc:
        await asyncio.to_thread(_finish_failure, job, str(exc) or 'permission_denied')
    except ValueError as exc:
        await asyncio.to_thread(_finish_failure, job, str(exc) or 'invalid_operation')
    except errors.RPCError as exc:
        await asyncio.to_thread(_finish_failure, job, type(exc).__name__, sending, not sending)
    except Exception:
        LOG.exception('Automatic funding join failed')
        await asyncio.to_thread(_finish_failure, job, 'network_error', sending, not sending)
    return False


async def _run_owner(owner):
    successes = 0
    attempts = 0
    try:
        while attempts < 30:
            cfg = await query("SELECT enabled,joined_in_batch,batch_size,cooldown_seconds,next_run_at<=CURRENT_TIMESTAMP FROM funding_auto_config WHERE owner_id=?", (owner,), fetchone=True)
            if not cfg or not cfg[0] or not cfg[4]:
                return
            joined,batch,cooldown = cfg[1],cfg[2],cfg[3]
            if joined >= batch:
                await query("UPDATE funding_auto_config SET joined_in_batch=0,next_run_at=CURRENT_TIMESTAMP+?*INTERVAL '1 second',last_result='cooldown',updated_at=CURRENT_TIMESTAMP WHERE owner_id=?", (cooldown,owner), commit=True)
                return
            accounts = await _eligible_accounts(owner)
            if not accounts:
                await query("UPDATE funding_auto_config SET next_run_at=CURRENT_TIMESTAMP+INTERVAL '1 minute',last_result='no_eligible_accounts',updated_at=CURRENT_TIMESTAMP WHERE owner_id=?", (owner,), commit=True)
                return
            reserved = None
            chosen = None
            for account,fingerprint in accounts:
                reserved = await asyncio.to_thread(_reserve, owner, account[0], fingerprint, account[2])
                if reserved:
                    chosen = (account,fingerprint)
                    break
            if not reserved:
                await query("UPDATE funding_auto_config SET next_run_at=CURRENT_TIMESTAMP+INTERVAL '30 seconds',last_result='no_tasks',updated_at=CURRENT_TIMESTAMP WHERE owner_id=?", (owner,), commit=True)
                return
            attempts += 1
            ok = await _perform(owner, chosen[0], chosen[1], reserved)
            if ok:
                successes += 1
                row = await query("UPDATE funding_auto_config SET joined_in_batch=joined_in_batch+1,last_result='joined',updated_at=CURRENT_TIMESTAMP WHERE owner_id=? RETURNING joined_in_batch,batch_size,cooldown_seconds", (owner,), commit=True, fetchone=True)
                if row and row[0] >= row[1]:
                    await query("UPDATE funding_auto_config SET joined_in_batch=0,next_run_at=CURRENT_TIMESTAMP+?*INTERVAL '1 second',last_result='cooldown',updated_at=CURRENT_TIMESTAMP WHERE owner_id=?", (row[2],owner), commit=True)
                    with connect() as db:
                        enqueue(db, owner, f'fund-auto-batch:{owner}:{reserved[0]}', 'اكتملت دفعة التجميع التلقائي: 15 اشتراكًا. سيتوقف التجميع 5 دقائق ثم يستأنف تلقائيًا.')
                    return
            await asyncio.sleep(2)
    finally:
        _workers.pop(owner, None)


async def _check_watch(row):
    membership,owner,phone,fingerprint,target_id,target,reward = row
    try:
        account = (await _collector_accounts()).get(str(phone))
        consent = await query(
            "SELECT 1 FROM funding_auto_accounts WHERE owner_id=? AND phone=? AND session_key=? AND enabled",
            (int(DEVELOPER_ID), phone, fingerprint), fetchone=True,
        )
        if not consent or not account or state_key(phone, account[1]) != fingerprint:
            await query(
                "UPDATE funding_auto_memberships SET status='closed',lease_until=NULL,last_checked=CURRENT_TIMESTAMP,last_result='participation_ended' WHERE membership_id=? AND status='active'",
                (membership,), commit=True,
            )
            return
        if not await session_is_usable(account[1]):
            raise PermissionError()
        async with automation.connection(account) as client:
            member = await _membership_state(client,target,target_id)
        if member:
            await query("UPDATE funding_auto_memberships SET next_check=CURRENT_TIMESTAMP+INTERVAL '5 minutes',lease_until=NULL,last_checked=CURRENT_TIMESTAMP,last_result='member' WHERE membership_id=? AND status='active'", (membership,), commit=True)
            return
        with connect() as db:
            current = db.execute("SELECT status FROM funding_auto_memberships WHERE membership_id=? FOR UPDATE", (membership,)).fetchone()
            if not current or current[0] != 'active':
                return
            store.point_change(db, owner, -(reward*2), 'auto_membership_penalty', f'auto-membership:{membership}', allow_negative=True)
            db.execute("UPDATE funding_auto_memberships SET status='penalized',penalized_at=CURRENT_TIMESTAMP,last_checked=CURRENT_TIMESTAMP,last_result='left',lease_until=NULL WHERE membership_id=?", (membership,))
            enqueue(db, owner, f'auto-membership-penalty:{membership}', f'رُصدت مغادرة حساب +{phone} لهدف تم جمع نقاطه تلقائيًا. خُصمت {reward*2} نقطة.')
    except asyncio.CancelledError:
        raise
    except Exception:
        await query("UPDATE funding_auto_memberships SET next_check=CURRENT_TIMESTAMP+INTERVAL '5 minutes',lease_until=NULL,last_result='verification_unavailable' WHERE membership_id=? AND status='active'", (membership,), commit=True)


async def monitor_memberships():
    rows = await query(
        """UPDATE funding_auto_memberships SET lease_until=CURRENT_TIMESTAMP+INTERVAL '2 minutes'
           WHERE membership_id IN (SELECT membership_id FROM funding_auto_memberships WHERE status='active' AND next_check<=CURRENT_TIMESTAMP
           AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP) ORDER BY next_check LIMIT 10 FOR UPDATE SKIP LOCKED)
           RETURNING membership_id,owner_id,phone,session_key,target_id,target,reward""",
        commit=True, fetchall=True,
    ) or []
    for row in rows:
        await _check_watch(row)


async def recover():
    await query("UPDATE funding_auto_jobs SET status='unknown',result='interrupted_during_join',updated_at=CURRENT_TIMESTAMP WHERE status='executing'", commit=True)


async def stop(owner=None):
    tasks = [task for task_owner, task in list(_workers.items()) if owner is None or task_owner == owner]
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def dispatch():
    global_cfg = await global_status()
    owner = int(DEVELOPER_ID) if DEVELOPER_ID else None
    if global_cfg[0] and owner:
        cfg = await query(
            "SELECT 1 FROM funding_auto_config WHERE owner_id=? AND enabled AND next_run_at<=CURRENT_TIMESTAMP",
            (owner,), fetchone=True,
        )
        if cfg and owner not in _workers:
            task = asyncio.create_task(_run_owner(owner), name=f'fund-auto-{owner}')
            _workers[owner] = task
            task.add_done_callback(lambda done, owner=owner: (LOG.error('Auto funding worker failed: %s', type(done.exception()).__name__) if not done.cancelled() and done.exception() else None))
    await monitor_memberships()
