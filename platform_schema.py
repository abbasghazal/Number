"""Additive Next Generation migrations; existing wallets and rewards are preserved."""


def migrate(c):
    for statement in [
        "ALTER TABLE operation_notices ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE funding_config ADD COLUMN IF NOT EXISTS minimum INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE funding_config ADD COLUMN IF NOT EXISTS maximum INTEGER NOT NULL DEFAULT 100000",
        "ALTER TABLE funding_config ADD COLUMN IF NOT EXISTS guarantee_seconds INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE funding_campaigns ADD COLUMN IF NOT EXISTS guarantee_seconds INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE funding_campaigns ADD COLUMN IF NOT EXISTS refunded BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE funding_campaigns ADD COLUMN IF NOT EXISTS request_key TEXT",
        "ALTER TABLE funding_campaigns ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE funding_campaigns DROP CONSTRAINT IF EXISTS funding_campaigns_status_check",
        "ALTER TABLE funding_campaigns ADD CONSTRAINT funding_campaigns_status_check CHECK(status IN ('active','pausing','completed','cancelled'))",
        "CREATE UNIQUE INDEX IF NOT EXISTS funding_campaign_request_idx ON funding_campaigns(user_id,request_key) WHERE request_key IS NOT NULL",
        "ALTER TABLE funding_tasks DROP CONSTRAINT IF EXISTS funding_tasks_status_check",
        "ALTER TABLE funding_tasks ADD CONSTRAINT funding_tasks_status_check CHECK(status IN ('reserved','pending','completed','expired','failed'))",
        "ALTER TABLE funding_tasks ADD COLUMN IF NOT EXISTS check_due TIMESTAMPTZ",
        "ALTER TABLE funding_tasks ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ",
        "ALTER TABLE funding_tasks ADD COLUMN IF NOT EXISTS last_result TEXT",
        "ALTER TABLE funding_tasks ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS funding_task_due_idx ON funding_tasks(check_due) WHERE status='pending'",
        """CREATE TABLE IF NOT EXISTS funding_verifications(id BIGSERIAL PRIMARY KEY,task_id TEXT NOT NULL,
            user_id BIGINT NOT NULL,phase TEXT NOT NULL,result TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS funding_margin(task_id TEXT PRIMARY KEY,campaign_id BIGINT NOT NULL,
            amount BIGINT NOT NULL CHECK(amount>=0),created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)""",
        """INSERT INTO funding_margin(task_id,campaign_id,amount) SELECT t.task_id,c.campaign_id,c.cost-c.reward
            FROM funding_tasks t JOIN funding_campaigns c ON c.campaign_id=t.campaign_id WHERE t.status='completed'
            ON CONFLICT DO NOTHING""",
        """CREATE TABLE IF NOT EXISTS funding_adjustments(request_key TEXT PRIMARY KEY,campaign_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,quantity INTEGER NOT NULL,cost BIGINT NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)""",
        "ALTER TABLE promotion_prices DROP CONSTRAINT IF EXISTS promotion_prices_kind_check",
        "ALTER TABLE promotion_prices ADD CONSTRAINT promotion_prices_kind_check CHECK(kind IN ('members','reactions','search'))",
        "INSERT INTO promotion_prices(kind,unit_price) VALUES('search',0.01) ON CONFLICT DO NOTHING",
        "ALTER TABLE promotion_orders DROP CONSTRAINT IF EXISTS promotion_orders_kind_check",
        "ALTER TABLE promotion_orders ADD CONSTRAINT promotion_orders_kind_check CHECK(kind IN ('members','reactions','search'))",
        "ALTER TABLE promotion_orders ADD COLUMN IF NOT EXISTS search_phrase TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE promotion_orders ADD COLUMN IF NOT EXISTS next_attempt TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE promotion_orders ADD COLUMN IF NOT EXISTS consecutive_errors INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS promotion_execution_config(id INTEGER PRIMARY KEY CHECK(id=1),enabled BOOLEAN NOT NULL DEFAULT TRUE,
            max_orders INTEGER NOT NULL DEFAULT 3,members_limit INTEGER NOT NULL DEFAULT 1,reactions_limit INTEGER NOT NULL DEFAULT 2,
            search_limit INTEGER NOT NULL DEFAULT 1,delay_seconds INTEGER NOT NULL DEFAULT 2,error_limit INTEGER NOT NULL DEFAULT 5,
            cooldown_seconds INTEGER NOT NULL DEFAULT 300,attempt_multiplier INTEGER NOT NULL DEFAULT 3)""",
        "INSERT INTO promotion_execution_config(id) VALUES(1) ON CONFLICT DO NOTHING",
        """CREATE TABLE IF NOT EXISTS platform_sessions(token_hash TEXT PRIMARY KEY,user_id BIGINT NOT NULL,csrf TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,last_seen TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS platform_sessions_expiry_idx ON platform_sessions(expires_at)",
        """CREATE TABLE IF NOT EXISTS platform_oauth(state_hash TEXT PRIMARY KEY,verifier TEXT NOT NULL,nonce TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,consumed BOOLEAN NOT NULL DEFAULT FALSE)""",
        """CREATE TABLE IF NOT EXISTS platform_requests(user_id BIGINT NOT NULL,request_key TEXT NOT NULL,route TEXT NOT NULL,
            body_hash TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'running',result TEXT,http_status INTEGER,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,request_key))""",
        """CREATE TABLE IF NOT EXISTS platform_handoffs(token_hash TEXT PRIMARY KEY,user_id BIGINT NOT NULL,payload TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,consumed BOOLEAN NOT NULL DEFAULT FALSE)""",
        """CREATE TABLE IF NOT EXISTS platform_preferences(user_id BIGINT PRIMARY KEY,theme TEXT NOT NULL DEFAULT 'dark',
            language TEXT NOT NULL DEFAULT 'ar',notifications BOOLEAN NOT NULL DEFAULT TRUE,stealth BOOLEAN NOT NULL DEFAULT FALSE)""",
        """CREATE TABLE IF NOT EXISTS platform_drafts(draft_id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,kind TEXT NOT NULL,
            payload TEXT NOT NULL,expires_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP+INTERVAL '10 minutes')""",
    ]:
        c.execute(statement)
    c.execute("ALTER TABLE platform_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP")
    c.execute("CREATE INDEX IF NOT EXISTS platform_requests_running_idx ON platform_requests(updated_at) WHERE status='running'")
    from web_auth import migrate as migrate_web_auth
    migrate_web_auth(c)
    from membership_schema import migrate as migrate_memberships
    migrate_memberships(c)
    from funding_auto_collect import migrate as migrate_funding_auto
    migrate_funding_auto(c)
    from session_lifecycle import migrate as migrate_session_lifecycle
    migrate_session_lifecycle(c)
    from api_service import migrate as migrate_api_service
    migrate_api_service(c)
