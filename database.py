from postgres import connect


def _migrate_auxiliary_schema(cursor):
    """Apply auxiliary schema updates without changing account or wallet data."""
    cursor.execute('''DROP TABLE IF EXISTS points_joined_channels, points_task_events,
        points_tasks, points_balances, points_daily_usage, points_cooldowns, points_bot_profiles''')
    cursor.execute("DELETE FROM settings WHERE key='points_manual_profiles_v1'")
    cursor.execute("DELETE FROM audit_log WHERE left(action,7)='points_'")


# Telegram user identifiers are 64-bit values.  Older PostgreSQL deployments
# created a number of these columns as INTEGER, which fails as soon as a user
# ID is outside the signed 32-bit range.  Keep this list explicit so counters
# and internal serial IDs are not altered accidentally.
TELEGRAM_ID_COLUMNS = {
    "user_id", "created_by", "referred_id", "referrer_id", "seller_id",
    "added_by", "assigned_to", "closed_by", "processed_by",
    "current_bidder", "used_by",
}

def _add_column_if_missing(cursor, table_name, column_name, column_definition):
    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_definition}")


def _migrate_telegram_id_columns(cursor):
    """Upgrade legacy PostgreSQL INTEGER Telegram-ID columns to BIGINT.

    This is deliberately data-type aware, making it idempotent and safe to run
    on every start.  Fresh installs already receive BIGINT through the SQL
    adapter; the migration protects existing VPS databases.
    """
    names = ", ".join(f"'{name}'" for name in sorted(TELEGRAM_ID_COLUMNS))
    cursor.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND data_type='integer' "
        f"AND column_name IN ({names})"
    )
    for table_name, column_name in cursor.fetchall():
        cursor.execute(
            f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" TYPE BIGINT'
        )


def _migrate_country_identity(cursor):
    """Give countries a stable identity without discarding existing rows.

    Calling codes are not unique globally (for example, several countries use
    ``+1``).  Older databases used that code as the primary key, which silently
    dropped such countries during seeding.  This migration preserves every
    existing row, introduces ``country_id`` as the canonical key, and adds an
    optional association on accounts for the next-stage UI migration.
    """
    cursor.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid='countries'::regclass "
        "AND contype='p'"
    )
    primary_keys = cursor.fetchall()
    cursor.execute("ALTER TABLE countries ADD COLUMN IF NOT EXISTS country_id BIGSERIAL")
    for (constraint_name,) in primary_keys:
        cursor.execute(f'ALTER TABLE countries DROP CONSTRAINT "{constraint_name}"')
    cursor.execute("ALTER TABLE countries ALTER COLUMN country_id SET NOT NULL")
    cursor.execute(
        "ALTER TABLE countries ADD CONSTRAINT countries_pkey PRIMARY KEY (country_id)"
    )
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_countries_name ON countries(name)")
    cursor.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS country_id BIGINT")
    cursor.execute(
        """UPDATE accounts AS account
           SET country_id=(
               SELECT country.country_id FROM countries AS country
               WHERE country.calling_code=account.calling_code
               ORDER BY country.country_id ASC LIMIT 1
           )
           WHERE account.country_id IS NULL"""
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_accounts_country_id ON accounts(country_id)"
    )


def _apply_pending_migrations(cursor):
    """Run idempotent, versioned schema migrations after their base tables exist."""
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version TEXT PRIMARY KEY,
               applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    migrations = (("20260905_country_identity", _migrate_country_identity),)
    for version, migration in migrations:
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,))
        if cursor.fetchone():
            continue
        migration(cursor)
        cursor.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))

def init_db():
    conn = connect()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS workflow_jobs (
        job_id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',total INTEGER,processed INTEGER NOT NULL DEFAULT 0,
        success INTEGER NOT NULL DEFAULT 0,failed INTEGER NOT NULL DEFAULT 0,message TEXT,
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,cancellable BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE INDEX IF NOT EXISTS workflow_jobs_owner_idx ON workflow_jobs(user_id,created_at DESC)''')
    c.execute('''CREATE TABLE IF NOT EXISTS operation_notices (
        notice_id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,dedupe_key TEXT UNIQUE NOT NULL,
        message TEXT NOT NULL,sent_at TIMESTAMP,next_attempt TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_notification_preferences (
        user_id BIGINT PRIMARY KEY,health BOOLEAN NOT NULL DEFAULT TRUE,rental BOOLEAN NOT NULL DEFAULT TRUE,
        twofa BOOLEAN NOT NULL DEFAULT TRUE,system BOOLEAN NOT NULL DEFAULT TRUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_notification_center (
        notice_id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,category TEXT NOT NULL,message TEXT NOT NULL,
        is_read BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS scheduled_operations (
        schedule_id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL DEFAULT '{}',
        due_at TIMESTAMP NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,error TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS ticket_messages (
        message_id BIGSERIAL PRIMARY KEY,ticket_id BIGINT NOT NULL,user_id BIGINT NOT NULL,message TEXT NOT NULL,
        is_staff BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS twofa_history (
        phone TEXT NOT NULL,user_id BIGINT NOT NULL,status TEXT NOT NULL,error TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(phone,user_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS account_labels (
        user_id BIGINT NOT NULL,phone TEXT NOT NULL,label TEXT NOT NULL DEFAULT '',
        folder TEXT NOT NULL DEFAULT '',tags TEXT NOT NULL DEFAULT '',PRIMARY KEY(user_id,phone))''')
    _add_column_if_missing(c, "account_labels", "tags", "TEXT NOT NULL DEFAULT ''")
    c.execute('''CREATE TABLE IF NOT EXISTS saved_account_filters (
        user_id BIGINT NOT NULL,name TEXT NOT NULL,filters TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(user_id,name))''')
    c.execute('''CREATE TABLE IF NOT EXISTS backup_runs (
        backup_id BIGSERIAL PRIMARY KEY,status TEXT NOT NULL DEFAULT 'running',path TEXT,size_bytes BIGINT,
        delivered BOOLEAN NOT NULL DEFAULT FALSE,error TEXT,restore_status TEXT DEFAULT 'untested',
        restore_details TEXT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,restored_at TIMESTAMP)''')

    c.execute('''CREATE TABLE IF NOT EXISTS account_health (
        session_key TEXT PRIMARY KEY,
        phone TEXT NOT NULL,
        inventory_session TEXT,
        status TEXT NOT NULL DEFAULT 'unknown',
        attempt TEXT NOT NULL DEFAULT 'unknown',
        details TEXT NOT NULL DEFAULT '{}',
        checked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        confirmed_at TIMESTAMPTZ,
        next_check_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS account_health_notices (
        session_key TEXT NOT NULL REFERENCES account_health(session_key) ON DELETE CASCADE,
        user_id BIGINT NOT NULL,
        status TEXT NOT NULL,
        PRIMARY KEY (session_key, user_id)
    )''')
    c.execute('''CREATE INDEX IF NOT EXISTS account_health_inventory_idx
                 ON account_health(phone) WHERE status IN
                 ('revoked', 'banned', 'deleted', 'restricted', 'invalid', 'mismatch')''')
    
    # إنشاء جدول المستخدمين
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                coins NUMERIC(18,2) DEFAULT 0,
                referral_points REAL DEFAULT 0,
                referral_balance NUMERIC(18,2) DEFAULT 0,
                join_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_active DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    # A bot account cannot call Telegram's GetDialogs API.  Keep a durable
    # list of the groups and channels from which it has received an update so
    # broadcasts can be sent without relying on that restricted API.
    c.execute('''CREATE TABLE IF NOT EXISTS broadcast_destinations (
                kind TEXT NOT NULL,
                chat_id BIGINT NOT NULL,
                access_hash BIGINT,
                title TEXT,
                last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (kind, chat_id)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS auto_engagement (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,          -- معرف القناة (username أو id)
                engagement_type TEXT NOT NULL,    -- 'react', 'vote', 'comment', 'views'
                emoji TEXT,                       -- الإيموجي المستخدم في التفاعل (إذا كان النوع 'react')
                vote_option INTEGER,              -- رقم الخيار (إذا كان النوع 'vote')
                comment_text TEXT,                -- نص التعليق (إذا كان النوع 'comment')
                is_active BOOLEAN DEFAULT 1,
                created_by INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_processed_msg_id INTEGER,   -- لتجنب إعادة معالجة نفس المنشور
                period TEXT,
                expires_at DATETIME,
                account_count INTEGER
                )''')
    _add_column_if_missing(c, "auto_engagement", "period", "TEXT")
    _add_column_if_missing(c, "auto_engagement", "expires_at", "TIMESTAMP")
    _add_column_if_missing(c, "auto_engagement", "account_count", "INTEGER")
    # Retire the old separate reaction/view jobs; new jobs always combine both.
    c.execute("UPDATE auto_engagement SET is_active=0 WHERE engagement_type<>'views_react'")
    c.execute('''CREATE TABLE IF NOT EXISTS engagement_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL UNIQUE,
                channel_name TEXT,
                created_by BIGINT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    # تجعل القنوات الموجودة في الإعدادات القديمة قابلة للاختيار في الواجهة الجديدة.
    c.execute('''INSERT INTO engagement_channels (channel_id)
                 SELECT DISTINCT channel_id FROM auto_engagement
                 ON CONFLICT (channel_id) DO NOTHING''')
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
                referred_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                rewarded_at DATETIME,
                FOREIGN KEY (referrer_id) REFERENCES users(user_id)
                )''')
    
    # إنشاء جدول الدول (مع تعديل الأسعار)
    c.execute('''CREATE TABLE IF NOT EXISTS countries (
                country_id BIGSERIAL PRIMARY KEY,
                calling_code TEXT NOT NULL,
                name TEXT NOT NULL,
                price NUMERIC(18,2),
                sell_price NUMERIC(18,2),
                is_active BOOLEAN DEFAULT 1
                )''')
    
    # إنشاء بقية الجداول (بدون تعديل)
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (
                phone TEXT PRIMARY KEY,
                session TEXT,
                calling_code TEXT,
                country_id BIGINT,
                twofa TEXT DEFAULT 'لا يوجد',
                seller_id INTEGER,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_sold BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1
                )''')
    _apply_pending_migrations(c)
    
    c.execute('''CREATE TABLE IF NOT EXISTS bad_guys (
                user_id INTEGER PRIMARY KEY
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS force_channels (
                channel_id TEXT PRIMARY KEY,
                channel_name TEXT,
                invite_link TEXT
                )''')
    # Developer-owned mandatory channels remain in force_channels.  User-paid
    # channels live separately so they can expire by time or completed joins.
    c.execute('''CREATE TABLE IF NOT EXISTS paid_force_channels (
                subscription_id BIGSERIAL PRIMARY KEY,
                channel_id TEXT NOT NULL,
                channel_name TEXT,
                owner_id BIGINT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('time','count')),
                price NUMERIC(18,2) NOT NULL CHECK(price > 0),
                expires_at DATETIME,
                join_limit INTEGER,
                joins_used INTEGER NOT NULL DEFAULT 0,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                completed_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    _add_column_if_missing(c, "paid_force_channels", "completed_at", "TIMESTAMP")
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS paid_force_channels_active_channel_idx
                 ON paid_force_channels(channel_id) WHERE active=TRUE''')
    c.execute('''CREATE TABLE IF NOT EXISTS paid_force_channel_members (
                subscription_id BIGINT NOT NULL REFERENCES paid_force_channels(subscription_id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(subscription_id, user_id)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS paid_force_channel_accounts (
                subscription_id BIGINT NOT NULL REFERENCES paid_force_channels(subscription_id) ON DELETE CASCADE,
                account_key TEXT NOT NULL,
                phone TEXT,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(subscription_id, account_key)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS force_count_prices (
                join_count INTEGER PRIMARY KEY CHECK(join_count > 0),
                price NUMERIC(18,2) NOT NULL CHECK(price > 0)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                admin_level INTEGER DEFAULT 1,
                added_by INTEGER,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')

    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')

    c.execute('''CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                assigned_to INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                closed_at DATETIME,
                closed_by INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    # These columns were added after the original tickets table.  They must
    # follow CREATE TABLE so a brand-new database can start as well.
    _add_column_if_missing(c, "support_tickets", "priority", "TEXT NOT NULL DEFAULT 'normal'")
    _add_column_if_missing(c, "support_tickets", "category", "TEXT NOT NULL DEFAULT 'general'")

    c.execute('''CREATE TABLE IF NOT EXISTS withdrawal_requests (
                request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount NUMERIC(18,2) NOT NULL CHECK(amount > 0),
                wallet TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                processed_at DATETIME,
                processed_by INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    c.execute("ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS rejection_reason TEXT")

    # Every payment is an order first; this makes provider callbacks idempotent
    # and prevents a callback replay from crediting a user twice.
    c.execute('''CREATE TABLE IF NOT EXISTS payment_orders (
                order_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                method TEXT NOT NULL,
                credits NUMERIC(18,2) NOT NULL CHECK(credits > 0),
                amount INTEGER NOT NULL CHECK(amount > 0),
                currency TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                provider_transaction_id TEXT UNIQUE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')

    # Immutable balance ledger. Every balance-changing operation must write a
    # row in the same transaction as the users.coins update.
    c.execute('''CREATE TABLE IF NOT EXISTS balance_ledger (
                entry_id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount NUMERIC(18,2) NOT NULL,
                balance_after NUMERIC(18,2) NOT NULL,
                operation TEXT NOT NULL,
                reference TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    # NULL means the old writer did not record a fee; do not invent historical
    # fees by applying today's commission rate to old transactions.
    c.execute("ALTER TABLE balance_ledger ADD COLUMN IF NOT EXISTS fee NUMERIC(18,2)")
    c.execute("ALTER TABLE balance_ledger ALTER COLUMN fee SET DEFAULT 0")
    c.execute('''CREATE INDEX IF NOT EXISTS balance_ledger_user_date_idx
                 ON balance_ledger(user_id, created_at DESC, entry_id DESC)''')
    c.execute('''CREATE INDEX IF NOT EXISTS balance_ledger_reference_idx
                 ON balance_ledger(user_id, operation, reference)''')
    c.execute('''CREATE INDEX IF NOT EXISTS payment_orders_user_date_idx
                 ON payment_orders(user_id, created_at DESC)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                description TEXT,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_purchases (
                purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                calling_code TEXT,
                name TEXT,
                price REAL,
                session TEXT,
                twofa TEXT,
                processing BOOLEAN DEFAULT 0,
                status TEXT DEFAULT 'pending',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS account_reservations (
                phone TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                reserved_until TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (phone) REFERENCES accounts(phone),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS stored_accounts (
                phone TEXT PRIMARY KEY,
                session TEXT,
                user_id INTEGER,
                twofa TEXT DEFAULT 'لا يوجد',
                storage_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS super_channels (
                channel_id TEXT PRIMARY KEY,
                title TEXT,
                invite_link TEXT,
                is_active BOOLEAN DEFAULT 1
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS posting_templates (
                template_id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_text TEXT,
                created_by INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (created_by) REFERENCES users(user_id)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_super_channels (
                user_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, channel_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS super_account_targets (
                user_id BIGINT NOT NULL,
                phone TEXT NOT NULL,
                target TEXT NOT NULL,
                peer_id BIGINT NOT NULL,
                access_hash BIGINT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, phone, target)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_posting_templates (
                template_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                template_text TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS posting_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                description TEXT
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS timed_names (
                user_id INTEGER PRIMARY KEY,
                active BOOLEAN DEFAULT 0,
                name_type TEXT,
                start_time DATETIME,
                end_time DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS auto_creation_tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                seconds_interval INTEGER,
                hours_duration INTEGER,
                remaining_runs INTEGER DEFAULT 10,
                active BOOLEAN DEFAULT 1,
                start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_run DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_sales (
                phone TEXT PRIMARY KEY,
                seller_id INTEGER,
                price REAL,
                calling_code TEXT,
                session TEXT,
                twofa TEXT,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (seller_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS sold_accounts (
                phone TEXT PRIMARY KEY,
                session TEXT,
                user_id INTEGER,
                seller_id INTEGER,
                price REAL,
                sold_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (seller_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS purchased_accounts (
                phone TEXT PRIMARY KEY,
                session TEXT,
                user_id INTEGER,
                seller_id INTEGER,
                calling_code TEXT,
                name TEXT,
                price REAL,
                twofa TEXT DEFAULT 'لا يوجد',
                purchased_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (seller_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS active_accounts (
                phone TEXT PRIMARY KEY,
                session TEXT,
                user_id INTEGER,
                activity TEXT,
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_activity DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS active_posting_tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                channel_id TEXT,
                all_channels BOOLEAN DEFAULT 0,
                seconds_interval INTEGER,
                repetitions INTEGER,
                remaining INTEGER,
                active BOOLEAN DEFAULT 1,
                last_post DATETIME,
                next_post DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS vip_users (
                user_id INTEGER PRIMARY KEY,
                vip_level INTEGER DEFAULT 1,
                start_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                end_date DATETIME,
                added_by INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (added_by) REFERENCES users(user_id)
                )''')

    c.execute('''CREATE TABLE IF NOT EXISTS rental_leases (
                lease_id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                renter_id BIGINT NOT NULL,
                price NUMERIC(18,2) NOT NULL CHECK(price >= 0),
                starts_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                ends_at DATETIME NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                FOREIGN KEY (phone) REFERENCES accounts(phone),
                FOREIGN KEY (renter_id) REFERENCES users(user_id)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS rental_renewal_quotes (
        quote_id TEXT PRIMARY KEY,lease_id BIGINT NOT NULL REFERENCES rental_leases(lease_id),
        user_id BIGINT NOT NULL,days INTEGER NOT NULL CHECK(days>0),
        amount NUMERIC(18,2) NOT NULL CHECK(amount>0),expected_end TIMESTAMP NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',expires_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP + INTERVAL '10 minutes')''')

    # Temporary encrypted credentials for a requested 2FA rotation.  The row
    # is removed as soon as the scheduled change succeeds.
    c.execute('''CREATE TABLE IF NOT EXISTS scheduled_twofa_changes (
                phone TEXT PRIMARY KEY,
                session TEXT NOT NULL,
                current_password TEXT,
                new_password TEXT,
                due_at TIMESTAMP NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )''')
    c.execute("ALTER TABLE scheduled_twofa_changes ADD COLUMN IF NOT EXISTS owner_id BIGINT")
    c.execute("ALTER TABLE scheduled_twofa_changes ADD COLUMN IF NOT EXISTS last_error TEXT")

    # إنشاء جدول المزادات
    c.execute('''CREATE TABLE IF NOT EXISTS auctions (
            auction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE,
            session TEXT,
            seller_id BIGINT,
            min_price REAL,
            current_bid REAL DEFAULT 0,
            current_bidder BIGINT,
            status TEXT DEFAULT 'active',
            start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            end_time DATETIME,
            twofa TEXT DEFAULT 'لا يوجد',
            FOREIGN KEY (seller_id) REFERENCES users(user_id),
            FOREIGN KEY (current_bidder) REFERENCES users(user_id)
            )''')

    # إنشاء جدول العروض
    c.execute('''CREATE TABLE IF NOT EXISTS auction_bids (
            bid_id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id INTEGER,
            user_id BIGINT,
            bid_amount REAL,
            bid_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (auction_id) REFERENCES auctions(auction_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
            )''')
    c.execute('''CREATE TABLE IF NOT EXISTS platform_auction_fees (
            fee_id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id INTEGER NOT NULL UNIQUE,
            amount REAL NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (auction_id) REFERENCES auctions(auction_id)
            )''')

    # مزادات المعرّفات العامة مستقلة عن مزادات الحسابات: لا نخزن جلسة أو
    # كلمة مرور عند بيع @username، وتبقى عملية نقل المعرّف بين الطرفين يدوية.
    c.execute('''CREATE TABLE IF NOT EXISTS username_auctions (
            auction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            seller_id BIGINT NOT NULL,
            min_price NUMERIC(18,2) NOT NULL,
            current_bid NUMERIC(18,2) NOT NULL,
            current_bidder BIGINT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (seller_id) REFERENCES users(user_id),
            FOREIGN KEY (current_bidder) REFERENCES users(user_id)
            )''')
    c.execute('''CREATE TABLE IF NOT EXISTS username_auction_bids (
            bid_id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id INTEGER NOT NULL,
            user_id BIGINT NOT NULL,
            bid_amount NUMERIC(18,2) NOT NULL,
            bid_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (auction_id) REFERENCES username_auctions(auction_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
            )''')
    _add_column_if_missing(c, "username_auctions", "transfer_channel_id", "BIGINT")
    _add_column_if_missing(c, "username_auctions", "transfer_channel_hash", "BIGINT")
    _add_column_if_missing(c, "username_auctions", "transfer_bound_at", "TIMESTAMP")
    # The original UNIQUE constraint blocked a username forever, including
    # after a completed auction. Keep uniqueness only while a listing is open.
    c.execute("ALTER TABLE username_auctions DROP CONSTRAINT IF EXISTS username_auctions_username_key")
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_username_auctions_open_username
                 ON username_auctions (username)
                 WHERE status IN ('active', 'awaiting_transfer', 'processing')""")
    c.execute('''CREATE TABLE IF NOT EXISTS username_auction_fees (
            fee_id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id INTEGER NOT NULL UNIQUE,
            amount NUMERIC(18,2) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (auction_id) REFERENCES username_auctions(auction_id)
            )''')
    c.execute('''CREATE TABLE IF NOT EXISTS username_auction_escrows (
            auction_id BIGINT PRIMARY KEY REFERENCES username_auctions(auction_id),
            buyer_id BIGINT NOT NULL REFERENCES users(user_id),
            amount NUMERIC(18,2) NOT NULL CHECK(amount>0),
            fee NUMERIC(18,2) NOT NULL CHECK(fee>=0),
            seller_amount NUMERIC(18,2) NOT NULL CHECK(seller_amount>=0),
            status TEXT NOT NULL DEFAULT 'held' CHECK(status IN ('held','review','completed','refunded')),
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''')
    c.execute("ALTER TABLE username_auction_escrows DROP CONSTRAINT IF EXISTS username_auction_escrows_status_check")
    c.execute("ALTER TABLE username_auction_escrows ADD CONSTRAINT username_auction_escrows_status_check CHECK(status IN ('held','review','completed','refunded'))")
    c.execute("CREATE INDEX IF NOT EXISTS username_auction_escrows_expiry_idx ON username_auction_escrows(expires_at) WHERE status='held'")

    c.execute('''CREATE TABLE IF NOT EXISTS licenses (
            license_key TEXT PRIMARY KEY,
            duration_days INTEGER NOT NULL,
            created_by INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            used_by INTEGER,
            used_at DATETIME,
            expires_at DATETIME,
            is_active BOOLEAN DEFAULT 1,
            is_revoked BOOLEAN DEFAULT 0
            )''')

    c.execute('''CREATE TABLE IF NOT EXISTS user_licenses (
            user_id INTEGER PRIMARY KEY,
            license_key TEXT,
            activated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME,
            is_valid BOOLEAN DEFAULT 1,
            reminded_3d BOOLEAN DEFAULT 0,
            reminded_1d BOOLEAN DEFAULT 0,
            expired_notified BOOLEAN DEFAULT 0,
            FOREIGN KEY (license_key) REFERENCES licenses(license_key)
            )''')

    # ترحيلات قواعد البيانات الموجودة
    _add_column_if_missing(c, "pending_purchases", "processing", "BOOLEAN DEFAULT 0")
    _add_column_if_missing(c, "purchased_accounts", "calling_code", "TEXT")
    _add_column_if_missing(c, "purchased_accounts", "name", "TEXT")
    _add_column_if_missing(c, "purchased_accounts", "seller_id", "INTEGER")
    _add_column_if_missing(c, "purchased_accounts", "calling_code", "TEXT")
    _add_column_if_missing(c, "purchased_accounts", "twofa", "TEXT DEFAULT 'لا يوجد'")
    _add_column_if_missing(c, "stored_accounts", "twofa", "TEXT DEFAULT 'لا يوجد'")
    _add_column_if_missing(c, "auctions", "twofa", "TEXT DEFAULT 'لا يوجد'")
    # قواعد البيانات المنشأة سابقاً استخدمت INTEGER لمعرّفات تيليجرام، بينما
    # قد تتجاوز هذه المعرّفات حد 32-bit. يشمل ذلك كل مسارات المستخدمين، لا
    # المزادات وحدها، وتبقى قيود المفاتيح الأجنبية سليمة.
    _migrate_telegram_id_columns(c)
    _add_column_if_missing(c, "sold_accounts", "calling_code", "TEXT")
    _add_column_if_missing(c, "sold_accounts", "name", "TEXT")
    _add_column_if_missing(c, "active_posting_tasks", "phone", "TEXT")
    _add_column_if_missing(c, "active_posting_tasks", "channel_id", "TEXT")
    _add_column_if_missing(c, "active_posting_tasks", "all_channels", "BOOLEAN DEFAULT 0")
    _add_column_if_missing(c, "active_posting_tasks", "posting_mode", "TEXT DEFAULT 'single_template'")
    _add_column_if_missing(c, "active_posting_tasks", "selected_template_id", "INTEGER")
    _add_column_if_missing(c, "user_licenses", "reminded_3d", "BOOLEAN DEFAULT 0")
    _add_column_if_missing(c, "user_licenses", "reminded_1d", "BOOLEAN DEFAULT 0")
    _add_column_if_missing(c, "user_licenses", "expired_notified", "BOOLEAN DEFAULT 0")
    # SQLite cannot add a column whose default is a non-constant expression.
    _add_column_if_missing(c, "pending_purchases", "updated_at", "DATETIME")
    _add_column_if_missing(c, "scheduled_twofa_changes", "new_password", "TEXT")
    _add_column_if_missing(c, "payment_orders", "wallet", "TEXT")
    c.execute("ALTER TABLE payment_orders ALTER COLUMN amount TYPE BIGINT")
    _add_column_if_missing(c, "users", "referral_points", "REAL DEFAULT 0")
    _add_column_if_missing(c, "users", "referral_balance", "REAL DEFAULT 0")
    # Monetary balances must never use floating point. Existing values are
    # rounded once during this migration to preserve the displayed cents.
    c.execute("ALTER TABLE users ALTER COLUMN coins TYPE NUMERIC(18,2) USING ROUND(coins::numeric, 2)")
    c.execute("ALTER TABLE users ALTER COLUMN referral_balance TYPE NUMERIC(18,2) USING ROUND(referral_balance::numeric, 2)")
    c.execute("ALTER TABLE payment_orders ALTER COLUMN credits TYPE NUMERIC(18,2) USING ROUND(credits::numeric, 2)")
    for table, column in (
        ("countries", "price"), ("countries", "sell_price"), ("withdrawal_requests", "amount"),
        ("pending_purchases", "price"), ("pending_sales", "price"), ("sold_accounts", "price"),
        ("purchased_accounts", "price"), ("auctions", "min_price"), ("auctions", "current_bid"),
        ("auction_bids", "bid_amount"), ("platform_auction_fees", "amount"),
    ):
        c.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE NUMERIC(18,2) USING ROUND({column}::numeric, 2)")
    c.execute("UPDATE pending_purchases SET updated_at=CURRENT_TIMESTAMP WHERE updated_at IS NULL")
    # A repeated tap must not create a second reservation for the same buyer
    # and account.  Legacy duplicates are reduced to the newest row first.
    c.execute("""DELETE FROM pending_purchases a USING pending_purchases b
                 WHERE a.user_id=b.user_id AND a.phone=b.phone
                   AND a.purchase_id < b.purchase_id""")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_purchases_user_phone ON pending_purchases(user_id, phone)")

    # فهارس الأداء
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_user_time ON audit_log(user_id, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets(status, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_support_tickets_user ON support_tickets(user_id, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_withdrawal_requests_status ON withdrawal_requests(status, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_payment_orders_user ON payment_orders(user_id, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_payment_orders_status ON payment_orders(status, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_balance_ledger_user_time ON balance_ledger(user_id, created_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_phone ON accounts(phone)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_account_reservations_expiry ON account_reservations(reserved_until)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stored_accounts_user ON stored_accounts(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stored_accounts_phone ON stored_accounts(phone)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_purchased_accounts_user ON purchased_accounts(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sold_accounts_user ON sold_accounts(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_auctions_status ON auctions(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_countries_calling_code ON countries(calling_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_active_posting_user_active ON active_posting_tasks(user_id, active)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_super_channels_user ON user_super_channels(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_posting_templates_user ON user_posting_templates(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_auto_creation_user_active ON auto_creation_tasks(user_id, active)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_licenses_status ON licenses(is_revoked, used_by, expires_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_licenses_valid ON user_licenses(is_valid, expires_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_rental_leases_renter_status ON rental_leases(renter_id, status, ends_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_twofa_due ON scheduled_twofa_changes(due_at)")
                
    # الإعدادات الأساسية (بدون تعديل)
    default_settings = [
        ('trust_channel', '', 'رابط القناة الرسمية'),
        ('rules_message', '''📜 **قوانين منصة Shahem**

• استخدام البوت يعني موافقتك على هذه القوانين.
• يُمنع استخدام الخدمات في الاحتيال أو الإزعاج أو انتهاك خصوصية الآخرين، وتتحمل مسؤولية استخدامك كاملة.
• عند شراء رقم أو حساب، تأكد من التفاصيل قبل التأكيد؛ العمليات المكتملة لا تُلغى إلا وفق قرار الإدارة.
• عند بيع حساب، يجب أن يكون الحساب صالحاً وقابلاً للدخول وخالياً من القيود أو المشاكل. للحماية، يجب تسجيل الخروج من الجلسات الأخرى عند الطلب.
• الرصيد مخصص لخدمات البوت فقط. رصيد الإحالات يُستخدم للشراء فقط ولا يمكن تحويله أو سحبه.
• التحويل والسحب يخضعان للحد الأدنى والعمولة المعروضة أثناء العملية، وتُراجع طلبات السحب من الإدارة.
• يمنع مشاركة الجلسات أو بيانات الحسابات أو استخدامها للإضرار بصاحبها أو بالآخرين.
• يحق للإدارة رفض أي عملية أو حظر أي حساب يخالف القوانين، دون تعويض عن العمليات المخالفة.
• للدعم أو الاستفسار استخدم زر «الدعم» من القائمة الرئيسية.

✅ نرجو استخدام المنصة بمسؤولية.''', 'رسالة القوانين'),
        ('transfer_minimum', '5', 'الحد الأدنى للتحويل'),
        ('mandatory_channel', '', 'القناة الإجبارية للاشتراك'),
        ('posting_active', '0', 'تفعيل نظام النشر التلقائي'),
        ('multi_posting', '0', 'النشر المتعدد'),
        ('auction_extend_minutes', '5', 'مدة تمديد المزاد عند العرض الأخير'),
        ('auction_platform_fee_percent', '5', 'نسبة عمولة المنصة من العرض الفائز في المزاد'),
        ('referral_reward_amount', '1', 'رصيد مكافأة الإحالة الواحدة للشراء فقط'),
        ('payment_stars_per_credit', '1', 'سعر شحن الرصيد الواحد بنجوم تيليجرام'),
        ('payment_ton_wallet', '', 'محفظة TON لاستقبال الدفعات'),
        ('payment_ton_per_credit', '1', 'سعر شحن الرصيد الواحد بعملة TON'),
        ('daily_backup_enabled', '1', 'إنشاء نسخة احتياطية يومية وإرسالها للمطور'),
        ('rental_daily_price', '1', 'سعر تأجير الرقم الواحد لليوم بالدولار'),
        ('rental_max_days', '30', 'أقصى مدة تأجير بالأيام'),
    ]
    
    for key, value, desc in default_settings:
        c.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, desc))

    # Remove settings left by the retired card and Asia Hawala payment methods.
    c.execute(
        "DELETE FROM settings WHERE key IN (?, ?)",
        ("payment_iqd_per_credit", "payment_asia_hawala_number"),
    )
    
    # إضافة الدول مع الأسعار الجديدة (سعر الشراء 1.5، سعر البيع 1.0)
    countries_data = [
        ("العراق 🇮🇶", "964", 1.5, 1.0),
        ("مصر 🇪🇬", "20", 1.5, 1.0),
        ("السعودية 🇸🇦", "966", 1.5, 1.0),
        ("الإمارات 🇦🇪", "971", 1.5, 1.0),
        ("الكويت 🇰🇼", "965", 1.5, 1.0),
        ("قطر 🇶🇦", "974", 1.5, 1.0),
        ("عُمان 🇴🇲", "968", 1.5, 1.0),
        ("لبنان 🇱🇧", "961", 1.5, 1.0),
        ("الأردن 🇯🇴", "962", 1.5, 1.0),
        ("الجزائر 🇩🇿", "213", 1.5, 1.0),
        ("المغرب 🇲🇦", "212", 1.5, 1.0),
        ("تونس 🇹🇳", "216", 1.5, 1.0),
        ("ليبيا 🇱🇾", "218", 1.5, 1.0),
        ("السودان 🇸🇩", "249", 1.5, 1.0),
        ("اليمن 🇾🇪", "967", 1.5, 1.0),
        ("سوريا 🇸🇾", "963", 1.5, 1.0),
        ("فلسطين 🇵🇸", "970", 1.5, 1.0),
        ("البحرين 🇧🇭", "973", 1.5, 1.0),
        ("موريتانيا 🇲🇷", "222", 1.5, 1.0),
        ("جيبوتي 🇩🇯", "253", 1.5, 1.0),
        ("تركيا 🇹🇷", "90", 1.5, 1.0),
        ("إيران 🇮🇷", "98", 1.5, 1.0),
        ("أفغانستان 🇦🇫", "93", 1.5, 1.0),
        ("ألبانيا 🇦🇱", "355", 1.5, 1.0),
        ("أندورا 🇦🇩", "376", 1.5, 1.0),
        ("أنغولا 🇦🇴", "244", 1.5, 1.0),
        ("أنتيغوا وباربودا 🇦🇬", "1268", 1.5, 1.0),
        ("الأرجنتين 🇦🇷", "54", 1.5, 1.0),
        ("أرمينيا 🇦🇲", "374", 1.5, 1.0),
        ("أستراليا 🇦🇺", "61", 1.5, 1.0),
        ("النمسا 🇦🇹", "43", 1.5, 1.0),
        ("أذربيجان 🇦🇿", "994", 1.5, 1.0),
        ("باهاماس 🇧🇸", "1242", 1.5, 1.0),
        ("بنغلاديش 🇧🇩", "880", 1.5, 1.0),
        ("باربادوس 🇧🇧", "1246", 1.5, 1.0),
        ("بيلاروسيا 🇧🇾", "375", 1.5, 1.0),
        ("بلجيكا 🇧🇪", "32", 1.5, 1.0),
        ("بليز 🇧🇿", "501", 1.5, 1.0),
        ("بنين 🇧🇯", "229", 1.5, 1.0),
        ("بوتان 🇧🇹", "975", 1.5, 1.0),
        ("بوليفيا 🇧🇴", "591", 1.5, 1.0),
        ("البوسنة والهرسك 🇧🇦", "387", 1.5, 1.0),
        ("بوتسوانا 🇧🇼", "267", 1.5, 1.0),
        ("البرازيل 🇧🇷", "55", 1.5, 1.0),
        ("بروناي 🇧🇳", "673", 1.5, 1.0),
        ("بلغاريا 🇧🇬", "359", 1.5, 1.0),
        ("بوركينا فاسو 🇧🇫", "226", 1.5, 1.0),
        ("بوروندي 🇧🇮", "257", 1.5, 1.0),
        ("الرأس الأخضر 🇨🇻", "238", 1.5, 1.0),
        ("كمبوديا 🇰🇭", "855", 1.5, 1.0),
        ("الكاميرون 🇨🇲", "237", 1.5, 1.0),
        ("كندا 🇨🇦", "1", 1.5, 1.0),
        ("جمهورية أفريقيا الوسطى 🇨🇫", "236", 1.5, 1.0),
        ("تشاد 🇹🇩", "235", 1.5, 1.0),
        ("تشيلي 🇨🇱", "56", 1.5, 1.0),
        ("الصين 🇨🇳", "86", 1.5, 1.0),
        ("كولومبيا 🇨🇴", "57", 1.5, 1.0),
        ("جزر القمر 🇰🇲", "269", 1.5, 1.0),
        ("الكونغو 🇨🇬", "242", 1.5, 1.0),
        ("كوستاريكا 🇨🇷", "506", 1.5, 1.0),
        ("كرواتيا 🇭🇷", "385", 1.5, 1.0),
        ("كوبا 🇨🇺", "53", 1.5, 1.0),
        ("قبرص 🇨🇾", "357", 1.5, 1.0),
        ("التشيك 🇨🇿", "420", 1.5, 1.0),
        ("الدنمارك 🇩🇰", "45", 1.5, 1.0),
        ("دومينيكا 🇩🇲", "1767", 1.5, 1.0),
        ("جمهورية الدومينيكان 🇩🇴", "1809", 1.5, 1.0),
        ("تيمور الشرقية 🇹🇱", "670", 1.5, 1.0),
        ("الإكوادور 🇪🇨", "593", 1.5, 1.0),
        ("السلفادور 🇸🇻", "503", 1.5, 1.0),
        ("غينيا الاستوائية 🇬🇶", "240", 1.5, 1.0),
        ("إريتريا 🇪🇷", "291", 1.5, 1.0),
        ("إستونيا 🇪🇪", "372", 1.5, 1.0),
        ("إسواتيني 🇸🇿", "268", 1.5, 1.0),
        ("إثيوبيا 🇪🇹", "251", 1.5, 1.0),
        ("فيجي 🇫🇯", "679", 1.5, 1.0),
        ("فنلندا 🇫🇮", "358", 1.5, 1.0),
        ("فرنسا 🇫🇷", "33", 1.5, 1.0),
        ("الغابون 🇬🇦", "241", 1.5, 1.0),
        ("غامبيا 🇬🇲", "220", 1.5, 1.0),
        ("جورجيا 🇬🇪", "995", 1.5, 1.0),
        ("ألمانيا 🇩🇪", "49", 1.5, 1.0),
        ("غانا 🇬🇭", "233", 1.5, 1.0),
        ("اليونان 🇬🇷", "30", 1.5, 1.0),
        ("غرينادا 🇬🇩", "1473", 1.5, 1.0),
        ("غواتيمالا 🇬🇹", "502", 1.5, 1.0),
        ("غينيا 🇬🇳", "224", 1.5, 1.0),
        ("غينيا بيساو 🇬🇼", "245", 1.5, 1.0),
        ("غيانا 🇬🇾", "592", 1.5, 1.0),
        ("هايتي 🇭🇹", "509", 1.5, 1.0),
        ("هندوراس 🇭🇳", "504", 1.5, 1.0),
        ("المجر 🇭🇺", "36", 1.5, 1.0),
        ("آيسلندا 🇮🇸", "354", 1.5, 1.0),
        ("الهند 🇮🇳", "91", 1.5, 1.0),
        ("إندونيسيا 🇮🇩", "62", 1.5, 1.0),
        ("جمهورية أيرلندا 🇮🇪", "353", 1.5, 1.0),
        ("إيطاليا 🇮🇹", "39", 1.5, 1.0),
        ("جامايكا 🇯🇲", "1876", 1.5, 1.0),
        ("اليابان 🇯🇵", "81", 1.5, 1.0),
        ("كازاخستان 🇰🇿", "7", 1.5, 1.0),
        ("كينيا 🇰🇪", "254", 1.5, 1.0),
        ("كيريباتي 🇰🇮", "686", 1.5, 1.0),
        ("كوسوفو 🇽🇰", "383", 1.5, 1.0),
        ("قيرغيزستان 🇰🇬", "996", 1.5, 1.0),
        ("لاوس 🇱🇦", "856", 1.5, 1.0),
        ("لاتفيا 🇱🇻", "371", 1.5, 1.0),
        ("ليسوتو 🇱🇸", "266", 1.5, 1.0),
        ("ليبيريا 🇱🇷", "231", 1.5, 1.0),
        ("ليختنشتاين 🇱🇮", "423", 1.5, 1.0),
        ("ليتوانيا 🇱🇹", "370", 1.5, 1.0),
        ("لوكسمبورغ 🇱🇺", "352", 1.5, 1.0),
        ("مدغشقر 🇲🇬", "261", 1.5, 1.0),
        ("ملاوي 🇲🇼", "265", 1.5, 1.0),
        ("ماليزيا 🇲🇾", "60", 1.5, 1.0),
        ("جزر المالديف 🇲🇻", "960", 1.5, 1.0),
        ("مالي 🇲🇱", "223", 1.5, 1.0),
        ("مالطا 🇲🇹", "356", 1.5, 1.0),
        ("جزر مارشال 🇲🇭", "692", 1.5, 1.0),
        ("موريشيوس 🇲🇺", "230", 1.5, 1.0),
        ("المكسيك 🇲🇽", "52", 1.5, 1.0),
        ("ميكرونيزيا 🇫🇲", "691", 1.5, 1.0),
        ("مولدوفا 🇲🇩", "373", 1.5, 1.0),
        ("موناكو 🇲🇨", "377", 1.5, 1.0),
        ("منغوليا 🇲🇳", "976", 1.5, 1.0),
        ("الجبل الأسود 🇲🇪", "382", 1.5, 1.0),
        ("موزمبيق 🇲🇿", "258", 1.5, 1.0),
        ("ميانمار 🇲🇲", "95", 1.5, 1.0),
        ("ناميبيا 🇳🇦", "264", 1.5, 1.0),
        ("ناورو 🇳🇷", "674", 1.5, 1.0),
        ("نيبال 🇳🇵", "977", 1.5, 1.0),
        ("هولندا 🇳🇱", "31", 1.5, 1.0),
        ("نيوزيلندا 🇳🇿", "64", 1.5, 1.0),
        ("نيكاراجوا 🇳🇮", "505", 1.5, 1.0),
        ("النيجر 🇳🇪", "227", 1.5, 1.0),
        ("نيجيريا 🇳🇬", "234", 1.5, 1.0),
        ("كوريا الشمالية 🇰🇵", "850", 1.5, 1.0),
        ("مقدونيا الشمالية 🇲🇰", "389", 1.5, 1.0),
        ("النرويج 🇳🇴", "47", 1.5, 1.0),
        ("باكستان 🇵🇰", "92", 1.5, 1.0),
        ("بالاو 🇵🇼", "680", 1.5, 1.0),
        ("بنما 🇵🇦", "507", 1.5, 1.0),
        ("بابوا غينيا الجديدة 🇵🇬", "675", 1.5, 1.0),
        ("باراغواي 🇵🇾", "595", 1.5, 1.0),
        ("بيرو 🇵🇪", "51", 1.5, 1.0),
        ("الفلبين 🇵🇭", "63", 1.5, 1.0),
        ("بولندا 🇵🇱", "48", 1.5, 1.0),
        ("البرتغال 🇵🇹", "351", 1.5, 1.0),
        ("رومانيا 🇷🇴", "40", 1.5, 1.0),
        ("روسيا 🇷🇺", "7", 1.5, 1.0),
        ("رواندا 🇷🇼", "250", 1.5, 1.0),
        ("سانت كيتس ونيفيس 🇰🇳", "1869", 1.5, 1.0),
        ("سانت لوسيا 🇱🇨", "1758", 1.5, 1.0),
        ("سانت فينسنت والغرينادين 🇻🇨", "1784", 1.5, 1.0),
        ("ساموا 🇼🇸", "685", 1.5, 1.0),
        ("سان مارينو 🇸🇲", "378", 1.5, 1.0),
        ("ساو تومي وبرينسيبي 🇸🇹", "239", 1.5, 1.0),
        ("السنغال 🇸🇳", "221", 1.5, 1.0)
    ]
    
    # السعر الموحد لكل الدول: شراء 0.50$ وبيع 0.30$.
    for name, code, _old_price, _old_sell_price in countries_data:
        c.execute("INSERT OR IGNORE INTO countries (name, calling_code, price, sell_price) VALUES (?, ?, ?, ?)", 
                 (name, code, 0.50, 0.30))

    # ترحيل لمرة واحدة يشمل الدول الموجودة مسبقاً. العلامة تمنع إعادة فرض
    # السعر بعد ذلك إذا غيّره المطور يدوياً من لوحة التحكم.
    price_migration = c.execute(
        "SELECT value FROM settings WHERE key=?",
        ("country_prices_050_030_applied",),
    ).fetchone()
    if not price_migration:
        c.execute("UPDATE countries SET price=0.50, sell_price=0.30")
        c.execute(
            """INSERT INTO settings (key, value, description)
               VALUES (?, ?, ?) ON CONFLICT (key) DO NOTHING""",
            (
                "country_prices_050_030_applied",
                "1",
                "تم توحيد سعر شراء الدول إلى 0.50 وسعر البيع إلى 0.30",
            ),
        )
    
    from feature_schema import migrate as migrate_features
    migrate_features(c)
    _migrate_auxiliary_schema(c)
    from account_branding import migrate as migrate_account_bio
    migrate_account_bio(c)
    from managed_twofa import migrate as migrate_managed_twofa
    migrate_managed_twofa(c)
    from promotion_schema import migrate as migrate_promotions
    migrate_promotions(c)
    # The collector has its own opt-in pools, jobs, and membership watches.
    # Keep this after the promotion schema because its jobs reference funding
    # campaigns created there.
    from funding_auto_collect import migrate as migrate_funding_auto_collect
    migrate_funding_auto_collect(c)
    from platform_schema import migrate as migrate_platform
    migrate_platform(c)
    conn.commit()
    conn.close()
