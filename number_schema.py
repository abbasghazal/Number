"""Idempotent schema and pricing migrations; accounts still require classification."""


def migrate(c):
    for statement in [
        "ALTER TABLE deferred_purchases ADD COLUMN IF NOT EXISTS funds_reserved BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE deferred_purchases ADD COLUMN IF NOT EXISTS referral_amount NUMERIC(18,2) NOT NULL DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS number_type_prices (
            country_id BIGINT NOT NULL REFERENCES countries(country_id) ON DELETE CASCADE,
            number_type TEXT NOT NULL CHECK(number_type IN ('spam','special','clean','guaranteed')),
            bot_sell_price NUMERIC(18,2) CHECK(bot_sell_price>0),bot_buy_price NUMERIC(18,2) CHECK(bot_buy_price>0),
            sell_enabled BOOLEAN NOT NULL DEFAULT FALSE,buy_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            warranty TEXT NOT NULL DEFAULT '{}',version INTEGER NOT NULL DEFAULT 1,
            updated_by BIGINT,updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(country_id,number_type))""",
        "ALTER TABLE number_type_prices ADD COLUMN IF NOT EXISTS bot_sell_stars BIGINT CHECK(bot_sell_stars BETWEEN 1 AND 10000000)",
        """CREATE TABLE IF NOT EXISTS number_metadata (
            phone TEXT PRIMARY KEY,country_id BIGINT REFERENCES countries(country_id),
            number_type TEXT CHECK(number_type IN ('spam','special','clean','guaranteed')),
            features TEXT NOT NULL DEFAULT '',warranty TEXT NOT NULL DEFAULT '{}',checked_at TIMESTAMPTZ,
            check_scope TEXT NOT NULL DEFAULT '',classified_by BIGINT,classified_at TIMESTAMP,
            revision INTEGER NOT NULL DEFAULT 1,reviewed BOOLEAN NOT NULL DEFAULT FALSE)""",
        """CREATE TABLE IF NOT EXISTS number_type_history (
            id BIGSERIAL PRIMARY KEY,phone TEXT NOT NULL,actor_id BIGINT,old_type TEXT,new_type TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        "ALTER TABLE commerce_quotes ADD COLUMN IF NOT EXISTS number_snapshot TEXT",
        "ALTER TABLE commerce_sales ADD COLUMN IF NOT EXISTS number_snapshot TEXT",
        "ALTER TABLE balance_ledger ADD COLUMN IF NOT EXISTS number_snapshot TEXT",
        "CREATE INDEX IF NOT EXISTS number_metadata_type_country_idx ON number_metadata(number_type,country_id)",
        "ALTER TABLE star_purchase_orders ADD COLUMN IF NOT EXISTS number_snapshot TEXT",
        "ALTER TABLE pending_sales ADD COLUMN IF NOT EXISTS number_type TEXT",
        "ALTER TABLE pending_sales ADD COLUMN IF NOT EXISTS country_id BIGINT",
        "ALTER TABLE pending_sales ADD COLUMN IF NOT EXISTS review_version INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE pending_sales ADD COLUMN IF NOT EXISTS approved_version INTEGER",
        "ALTER TABLE pending_sales ADD COLUMN IF NOT EXISTS pricing_version INTEGER",
        "ALTER TABLE pending_sales ADD COLUMN IF NOT EXISTS number_snapshot TEXT",
        "ALTER TABLE pending_sales ADD COLUMN IF NOT EXISTS offer_expires_at TIMESTAMPTZ",
        """CREATE TABLE IF NOT EXISTS number_sale_receipts (
            id BIGSERIAL PRIMARY KEY,phone TEXT NOT NULL,user_id BIGINT NOT NULL,amount NUMERIC(18,2) NOT NULL,
            number_snapshot TEXT NOT NULL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        "ALTER TABLE auctions ADD COLUMN IF NOT EXISTS number_snapshot TEXT",
        """CREATE TABLE IF NOT EXISTS account_management_grants (
            phone TEXT PRIMARY KEY,owner_id BIGINT NOT NULL,granted_by BIGINT NOT NULL,
            sensitive BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS account_operation_audit (
            id BIGSERIAL PRIMARY KEY,actor_id BIGINT NOT NULL,phone TEXT NOT NULL,owner_id BIGINT,
            action TEXT NOT NULL,result TEXT NOT NULL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS search_join_results (
            job_id TEXT NOT NULL,phone TEXT NOT NULL,result TEXT NOT NULL,wait_seconds INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(job_id,phone))""",
        """CREATE TABLE IF NOT EXISTS automation_rules (
            rule_id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,phone TEXT NOT NULL,kind TEXT NOT NULL,
            payload TEXT NOT NULL,enabled BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS automation_jobs (
            job_id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,
            due_at TIMESTAMPTZ NOT NULL,ends_at TIMESTAMPTZ,timezone TEXT NOT NULL,
            interval_minutes INTEGER CHECK(interval_minutes>=5),status TEXT NOT NULL DEFAULT 'paused',
            cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,run_count INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS automation_job_accounts (
            job_id BIGINT REFERENCES automation_jobs(job_id) ON DELETE CASCADE,phone TEXT NOT NULL,
            run_count INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',result TEXT,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(job_id,phone))""",
        """CREATE TABLE IF NOT EXISTS automation_events (
            phone TEXT NOT NULL,chat_id BIGINT NOT NULL,message_id BIGINT NOT NULL,
            status TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(phone,chat_id,message_id))""",
        """CREATE TABLE IF NOT EXISTS automation_cooldowns (
            rule_id BIGINT REFERENCES automation_rules(rule_id) ON DELETE CASCADE,sender_id BIGINT NOT NULL,
            last_reply TIMESTAMPTZ NOT NULL,PRIMARY KEY(rule_id,sender_id))""",
        """CREATE TABLE IF NOT EXISTS automation_manual (
            user_id BIGINT NOT NULL,phone TEXT NOT NULL,chat_id BIGINT NOT NULL,until_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(user_id,phone,chat_id))""",
        """CREATE TABLE IF NOT EXISTS automation_log (
            id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,phone TEXT,action TEXT NOT NULL,
            result TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS automation_templates (
            template_id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,text TEXT NOT NULL DEFAULT '',
            media BYTEA,media_name TEXT)""",
        """CREATE TABLE IF NOT EXISTS automation_waits(phone TEXT PRIMARY KEY,until_at TIMESTAMPTZ NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS automation_jobs_due_idx ON automation_jobs(due_at) WHERE status='pending'",
    ]:
        c.execute(statement)
    for table in ("accounts", "stored_accounts", "purchased_accounts", "sold_accounts", "pending_sales", "auctions"):
        c.execute(f"INSERT INTO number_metadata(phone) SELECT phone FROM {table} ON CONFLICT DO NOTHING")
    from inventory_pricing import apply_default_prices
    apply_default_prices(c)
    if not c.execute("SELECT 1 FROM settings WHERE key='number_types_v1'").fetchone():
        # No legacy quote has the required immutable identity snapshot. No funds
        # were debited for ordinary pending/deferred purchases.
        c.execute(
            "UPDATE commerce_quotes SET status='cancelled' WHERE service='purchase' AND status='pending' AND number_snapshot IS NULL"
        )
        c.execute(
            "UPDATE deferred_purchases SET status='cancelled',last_error='مراجعة تصنيف الرقم مطلوبة' WHERE status='pending'"
        )
        c.execute(
            "DELETE FROM pending_purchases WHERE quote_id IS NULL OR quote_id IN (SELECT quote_id FROM commerce_quotes WHERE status='cancelled')"
        )
        c.execute(
            "UPDATE star_purchase_orders SET status=CASE WHEN status='paid' THEN 'refund_pending' ELSE 'cancelled' END WHERE status IN ('pending','checkout','paid') AND number_snapshot IS NULL"
        )
        c.execute(
            "DELETE FROM account_reservations WHERE star_order_id IS NULL OR star_order_id IN (SELECT order_id FROM star_purchase_orders WHERE status IN ('cancelled','refund_pending'))"
        )
        c.execute("INSERT INTO settings(key,value) VALUES('number_types_v1','1')")
        c.execute(
            "UPDATE auctions SET status='cancelled' WHERE status IN ('active','processing') AND number_snapshot IS NULL"
        )
