"""Persistent orders, point funding and developer code monitoring."""


def migrate(c):
    for statement in [
        """CREATE TABLE IF NOT EXISTS promotion_prices(kind TEXT PRIMARY KEY CHECK(kind IN ('members','reactions')),
            unit_price NUMERIC(18,6) NOT NULL CHECK(unit_price>0),enabled BOOLEAN NOT NULL DEFAULT FALSE,
            version INTEGER NOT NULL DEFAULT 1)""",
        "INSERT INTO promotion_prices(kind,unit_price) VALUES('members',0.01),('reactions',0.005) ON CONFLICT DO NOTHING",
        """CREATE TABLE IF NOT EXISTS promotion_pool(phone TEXT PRIMARY KEY,owner_id BIGINT NOT NULL,
            session_key TEXT NOT NULL,enabled BOOLEAN NOT NULL DEFAULT TRUE,updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS promotion_orders(order_id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('members','reactions')),target_id BIGINT NOT NULL,target TEXT NOT NULL,
            title TEXT NOT NULL,message_id BIGINT NOT NULL DEFAULT 0,emoji TEXT NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 1000),unit_price NUMERIC(18,6) NOT NULL,
            price_version INTEGER NOT NULL,charge NUMERIC(18,2) NOT NULL DEFAULT 0,refunded NUMERIC(18,2) NOT NULL DEFAULT 0,
            referral_charge NUMERIC(18,2) NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'quote' CHECK(status IN ('quote','running','completed','partial','review','cancelled','expired')),
            cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP+INTERVAL '10 minutes',updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS promotion_orders_owner_idx ON promotion_orders(user_id,created_at DESC)",
        """CREATE TABLE IF NOT EXISTS promotion_items(order_id TEXT NOT NULL REFERENCES promotion_orders(order_id),
            phone TEXT NOT NULL,owner_id BIGINT NOT NULL,session_key TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','checking','executing','success','failed','unknown')),result TEXT,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(order_id,phone))""",
        """CREATE TABLE IF NOT EXISTS promotion_slots(kind TEXT NOT NULL,target_id BIGINT NOT NULL,message_id BIGINT NOT NULL,
            phone TEXT NOT NULL,order_id TEXT NOT NULL,PRIMARY KEY(kind,target_id,message_id,phone))""",
        """CREATE TABLE IF NOT EXISTS funding_config(id INTEGER PRIMARY KEY CHECK(id=1),reward BIGINT NOT NULL CHECK(reward>0),
            cost BIGINT NOT NULL CHECK(cost>=reward),enabled BOOLEAN NOT NULL DEFAULT TRUE)""",
        "INSERT INTO funding_config(id,reward,cost) VALUES(1,10,15) ON CONFLICT DO NOTHING",
        "CREATE TABLE IF NOT EXISTS funding_wallets(user_id BIGINT PRIMARY KEY,points BIGINT NOT NULL DEFAULT 0 CHECK(points>=0))",
        """CREATE TABLE IF NOT EXISTS funding_ledger(entry_id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,
            amount BIGINT NOT NULL,balance_after BIGINT NOT NULL,operation TEXT NOT NULL,reference TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,UNIQUE(user_id,operation,reference))""",
        """CREATE TABLE IF NOT EXISTS funding_campaigns(campaign_id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,
            target_id BIGINT NOT NULL,target TEXT NOT NULL,title TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 100000),
            delivered INTEGER NOT NULL DEFAULT 0,reward BIGINT NOT NULL,cost BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','cancelled')),
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS funding_target_live_idx ON funding_campaigns(target_id) WHERE status='active'",
        """CREATE TABLE IF NOT EXISTS funding_tasks(task_id TEXT PRIMARY KEY,campaign_id BIGINT NOT NULL REFERENCES funding_campaigns(campaign_id),
            user_id BIGINT NOT NULL,status TEXT NOT NULL DEFAULT 'reserved' CHECK(status IN ('reserved','completed','expired')),
            expires_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP+INTERVAL '10 minutes',UNIQUE(campaign_id,user_id))""",
        "CREATE TABLE IF NOT EXISTS funding_memberships(target_id BIGINT NOT NULL,user_id BIGINT NOT NULL,PRIMARY KEY(target_id,user_id))",
        """CREATE TABLE IF NOT EXISTS code_monitor_config(id INTEGER PRIMARY KEY CHECK(id=1),enabled BOOLEAN NOT NULL DEFAULT FALSE,
            source TEXT NOT NULL DEFAULT '@taIabati',source_id BIGINT,monitor_phone TEXT,last_message BIGINT NOT NULL DEFAULT 0)""",
        "INSERT INTO code_monitor_config(id) VALUES(1) ON CONFLICT DO NOTHING",
        """CREATE TABLE IF NOT EXISTS code_monitor_bots(username TEXT PRIMARY KEY,button_text TEXT NOT NULL DEFAULT 'استخدام كود',
            enabled BOOLEAN NOT NULL DEFAULT TRUE)""",
        "INSERT INTO code_monitor_bots(username) VALUES('@faabot') ON CONFLICT DO NOTHING",
        """CREATE TABLE IF NOT EXISTS monitored_codes(code_id BIGSERIAL PRIMARY KEY,source_id BIGINT NOT NULL,message_id BIGINT NOT NULL,
            bot TEXT NOT NULL,code TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,UNIQUE(bot,code))""",
        """CREATE TABLE IF NOT EXISTS monitored_code_accounts(code_id BIGINT NOT NULL REFERENCES monitored_codes(code_id),
            phone TEXT NOT NULL,owner_id BIGINT NOT NULL,session_key TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','preparing','sending','confirmed','failed','unknown')),
            result TEXT,next_attempt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(code_id,phone))""",
        "CREATE UNIQUE INDEX IF NOT EXISTS monitored_codes_normalized_idx ON monitored_codes(bot,lower(code))",
        """CREATE TABLE IF NOT EXISTS user_code_monitor_config(
            owner_id BIGINT PRIMARY KEY,enabled BOOLEAN NOT NULL DEFAULT FALSE,
            source TEXT NOT NULL DEFAULT '@taIabati',source_id BIGINT,monitor_phone TEXT,
            last_message BIGINT NOT NULL DEFAULT 0,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS user_code_monitor_bots(
            owner_id BIGINT NOT NULL,username TEXT NOT NULL,button_text TEXT NOT NULL DEFAULT 'استخدام كود',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,PRIMARY KEY(owner_id,username))""",
        """CREATE TABLE IF NOT EXISTS user_monitored_codes(
            code_id BIGSERIAL PRIMARY KEY,owner_id BIGINT NOT NULL,source_id BIGINT NOT NULL,message_id BIGINT NOT NULL,
            bot TEXT NOT NULL,code TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_id,bot,code))""",
        """CREATE TABLE IF NOT EXISTS user_monitored_code_accounts(
            code_id BIGINT NOT NULL REFERENCES user_monitored_codes(code_id),phone TEXT NOT NULL,
            session_key TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','preparing','sending','confirmed','failed','unknown')),
            result TEXT,next_attempt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(code_id,phone))""",
        "CREATE INDEX IF NOT EXISTS user_code_monitor_pending_idx ON user_monitored_code_accounts(status,next_attempt)",
        """CREATE TABLE IF NOT EXISTS user_code_monitor_sources(
            owner_id BIGINT NOT NULL,source_id BIGINT NOT NULL,access_hash BIGINT,source TEXT NOT NULL,monitor_phone TEXT NOT NULL,
            last_message BIGINT NOT NULL DEFAULT 0,enabled BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(owner_id,source_id))""",
        "CREATE INDEX IF NOT EXISTS user_code_monitor_sources_enabled_idx ON user_code_monitor_sources(owner_id,enabled)",
    ]:
        c.execute(statement)
    c.execute(
        """INSERT INTO user_code_monitor_sources(owner_id,source_id,source,monitor_phone,last_message,enabled)
           SELECT owner_id,source_id,source,monitor_phone,last_message,enabled FROM user_code_monitor_config
           WHERE source_id IS NOT NULL AND monitor_phone IS NOT NULL
           ON CONFLICT(owner_id,source_id) DO NOTHING"""
    )
    # Preserve the previous developer-only monitor as that developer's first
    # personal configuration; new users receive isolated rows on first use.
    from config import DEVELOPER_ID
    if DEVELOPER_ID:
        c.execute(
            """INSERT INTO user_code_monitor_config(owner_id,enabled,source,source_id,monitor_phone,last_message)
               SELECT ?,enabled,source,source_id,monitor_phone,last_message FROM code_monitor_config WHERE id=1
               ON CONFLICT(owner_id) DO NOTHING""",
            (DEVELOPER_ID,),
        )
        c.execute(
            """INSERT INTO user_code_monitor_bots(owner_id,username,button_text,enabled)
               SELECT ?,username,button_text,enabled FROM code_monitor_bots
               ON CONFLICT(owner_id,username) DO NOTHING""",
            (DEVELOPER_ID,),
        )
        c.execute(
            """INSERT INTO user_code_monitor_sources(owner_id,source_id,source,monitor_phone,last_message,enabled)
               SELECT owner_id,source_id,source,monitor_phone,last_message,enabled FROM user_code_monitor_config
               WHERE owner_id=? AND source_id IS NOT NULL AND monitor_phone IS NOT NULL
               ON CONFLICT(owner_id,source_id) DO NOTHING""",
            (DEVELOPER_ID,),
        )
    c.execute("ALTER TABLE user_code_monitor_sources ADD COLUMN IF NOT EXISTS access_hash BIGINT")
    c.execute("ALTER TABLE promotion_orders ADD COLUMN IF NOT EXISTS referral_charge NUMERIC(18,2) NOT NULL DEFAULT 0")
