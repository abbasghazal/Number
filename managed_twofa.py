"""Apply the configured fixed 2FA password to managed Telegram accounts."""
import asyncio
import hashlib
import re

from postgres import connect
from workflow import query

PASSWORD_TABLES = ('stored_accounts', 'purchased_accounts', 'accounts', 'pending_sales',
                   'pending_purchases', 'auctions')
SESSION_TABLES = ('sold_accounts', 'active_accounts')
POLICY_VERSION = 'fixed-twofa-v3'


def migrate(c):
    c.execute('''CREATE TABLE IF NOT EXISTS managed_twofa_state (
        session_key TEXT PRIMARY KEY,phone TEXT NOT NULL,status TEXT NOT NULL,
        last_error TEXT,next_run_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS managed_twofa_credentials (
        phone TEXT PRIMARY KEY,password TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    c.execute("ALTER TABLE scheduled_twofa_changes ADD COLUMN IF NOT EXISTS legacy_password TEXT")


def state_key(phone, session):
    return hashlib.sha256(f'{phone}:{session}'.encode()).hexdigest()


def retry_delay(reason):
    match = re.search(r'retry_after=(\d+)', reason or '')
    if match:
        return max(60, int(match[1]))
    if 'مشغول' in (reason or ''):
        return 60
    return 3600


def save_state(db, phone, session, status, reason=None):
    delay = 21600 if status == 'completed' else retry_delay(reason)
    db.execute('''INSERT INTO managed_twofa_state(session_key,phone,status,last_error,next_run_at)
        VALUES(?,?,?,?,CURRENT_TIMESTAMP + ? * INTERVAL '1 second')
        ON CONFLICT(session_key) DO UPDATE SET status=excluded.status,last_error=excluded.last_error,
        next_run_at=excluded.next_run_at,updated_at=CURRENT_TIMESTAMP''',
        (state_key(phone, session), phone, status, reason, delay))


def expedite_legacy_requests():
    """Advance existing jobs once, retaining proposed passwords for crash recovery."""
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext('managed-twofa-policy'))")
        if db.execute('SELECT 1 FROM settings WHERE key=?', (POLICY_VERSION,)).fetchone():
            return
        from helpers import encrypt_session, generate_account_twofa_password
        for phone, in db.execute(
                'SELECT phone FROM scheduled_twofa_changes').fetchall():
            db.execute('''UPDATE scheduled_twofa_changes SET legacy_password=COALESCE(legacy_password,new_password),
                new_password=? WHERE phone=?''',
                (encrypt_session(generate_account_twofa_password()), phone))
        db.execute('UPDATE scheduled_twofa_changes SET due_at=CURRENT_TIMESTAMP,attempts=0,last_error=NULL')
        db.execute("UPDATE managed_twofa_state SET status='retrying',next_run_at=CURRENT_TIMESTAMP WHERE status='completed'")
        db.execute('INSERT INTO settings(key,value) VALUES(?,?)', (POLICY_VERSION, 'ready'))


def _read_candidates(rows, jobs):
    from helpers import decrypt_session
    credentials = {}
    sessions = {}
    # All saved passwords, including old pending proposals, remain candidates.
    for phone, encrypted_session, *passwords in [*rows, *jobs]:
        phone = str(phone).lstrip('+')
        session = decrypt_session(encrypted_session)
        if not session:
            continue
        sessions.setdefault(state_key(phone, session), (phone, session))
        for encrypted in passwords:
            password = decrypt_session(encrypted)
            if password and password not in ('لا يوجد', 'no password'):
                values = credentials.setdefault(phone, [])
                if password not in values:
                    values.append(password)
    return sessions, credentials


def _managed_password(phone):
    from helpers import decrypt_session, encrypt_session, generate_account_twofa_password
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f'managed-twofa:{phone}',))
        row = db.execute('SELECT password FROM managed_twofa_credentials WHERE phone=? FOR UPDATE', (phone,)).fetchone()
        password = generate_account_twofa_password()
        existing = decrypt_session(row[0]) if row else None
        # Persist the configured value before connecting so a restart can retry
        # the same change after Telegram accepts it but the DB commit fails.
        if existing != password or (row and not str(row[0]).startswith('enc:')):
            encrypted = encrypt_session(password)
            db.execute('''INSERT INTO managed_twofa_credentials(phone,password) VALUES(?,?)
                ON CONFLICT(phone) DO UPDATE SET password=excluded.password,updated_at=CURRENT_TIMESTAMP''',
                (phone, encrypted))
        return password


async def reconcile(limit=20):
    from helpers import set_managed_twofa, encrypt_session
    from utils.asyncio_compat import to_thread
    from platform_services import record_twofa_outcome

    rows = await query(' UNION ALL '.join(
        [f'SELECT phone,session,twofa FROM {table}' for table in PASSWORD_TABLES]
        + [f'SELECT phone,session,NULL AS twofa FROM {table}' for table in SESSION_TABLES]), fetchall=True) or []
    job_rows = await query('SELECT phone,session,current_password,legacy_password,new_password,attempts FROM scheduled_twofa_changes', fetchall=True) or []
    jobs = [r[:5] for r in job_rows]
    pending = {str(r[0]).lstrip('+') for r in job_rows if r[0] and r[5] < 3}
    sessions, credentials = _read_candidates(rows, jobs)
    states = await query('SELECT session_key,next_run_at>CURRENT_TIMESTAMP FROM managed_twofa_state ORDER BY updated_at', fetchall=True) or []
    blocked = dict(states)
    rank = {key: index for index, (key, _) in enumerate(states)}
    completed = failed = 0
    succeeded_phones = set()
    for key in sorted(sessions, key=lambda value: (value in rank, rank.get(value, -1))):
        phone, session = sessions[key]
        if phone in pending or phone in succeeded_phones or blocked.get(key):
            continue
        password = await to_thread(_managed_password, phone)
        changed, reason = await set_managed_twofa(session, password, credentials.get(phone), expected_phone=phone)
        with connect() as db:
            if changed:
                encrypted = encrypt_session(password)
                for table in PASSWORD_TABLES:
                    db.execute(f'UPDATE {table} SET twofa=? WHERE phone=?', (encrypted, phone))
                db.execute('''INSERT INTO managed_twofa_credentials(phone,password) VALUES(?,?)
                    ON CONFLICT(phone) DO UPDATE SET password=excluded.password,updated_at=CURRENT_TIMESTAMP''',
                    (phone, encrypted))
                record_twofa_outcome(db, phone, 'completed')
                db.execute('DELETE FROM scheduled_twofa_changes WHERE phone=? AND attempts>=3', (phone,))
            save_state(db, phone, session, 'completed' if changed else 'retrying', reason)
        completed += bool(changed)
        failed += not changed
        if changed:
            succeeded_phones.add(phone)
        if completed + failed >= limit:
            break
        await asyncio.sleep(1)
    return completed, failed


async def summary():
    return await query('SELECT status,COUNT(*) FROM managed_twofa_state GROUP BY status', fetchall=True) or []
