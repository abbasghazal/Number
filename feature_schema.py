"""Additive migrations for the storefront and operational features."""


def migrate(c):
    statements = [
        """CREATE TABLE IF NOT EXISTS deferred_purchases (
            quote_id TEXT PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(user_id),phone TEXT NOT NULL,
            amount NUMERIC(18,2) NOT NULL CHECK(amount>0),status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','completed','cancelled')),
            ready_at TIMESTAMP NOT NULL,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,last_error TEXT)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS deferred_purchase_phone_idx ON deferred_purchases(phone) WHERE status='pending'",
        "ALTER TABLE countries ADD COLUMN IF NOT EXISTS stars_price BIGINT CHECK(stars_price BETWEEN 1 AND 10000000)",
        "ALTER TABLE account_reservations ADD COLUMN IF NOT EXISTS star_order_id TEXT",
        """CREATE TABLE IF NOT EXISTS star_purchase_orders (
            order_id TEXT PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(user_id),
            phone TEXT NOT NULL,country_id BIGINT,calling_code TEXT NOT NULL,name TEXT NOT NULL,
            inventory_session TEXT NOT NULL,amount BIGINT NOT NULL CHECK(amount BETWEEN 1 AND 10000000),
            credit_value NUMERIC(18,2) NOT NULL CHECK(credit_value>0),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN
                ('pending','checkout','paid','completed','refund_pending','refunded','expired','cancelled')),
            charge_id TEXT UNIQUE,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP+INTERVAL '5 minutes',
            paid_at TIMESTAMP,completed_at TIMESTAMP,refunded_at TIMESTAMP,
            next_attempt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,last_error TEXT,
            refund_lease_until TIMESTAMP)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS star_purchase_live_phone_idx ON star_purchase_orders(phone)
            WHERE status IN ('pending','checkout','paid')""",
        "CREATE INDEX IF NOT EXISTS star_purchase_owner_idx ON star_purchase_orders(user_id,created_at DESC)",
        """CREATE TABLE IF NOT EXISTS staff_roles (
            user_id BIGINT PRIMARY KEY,role TEXT NOT NULL CHECK(role IN
            ('administrator','support','inventory','finance','operations')),
            updated_by BIGINT,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """INSERT INTO staff_roles(user_id,role) SELECT user_id,'administrator' FROM admins
            ON CONFLICT(user_id) DO NOTHING""",
        """CREATE TABLE IF NOT EXISTS commerce_coupons (
            code TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('percent','fixed')),
            value NUMERIC(18,2) NOT NULL CHECK(value>0),service TEXT NOT NULL
            CHECK(service IN ('all','purchase','rental','renewal')),
            expires_at TIMESTAMP NOT NULL,max_uses INTEGER NOT NULL CHECK(max_uses>0),
            per_user INTEGER NOT NULL DEFAULT 1 CHECK(per_user>0),
            used INTEGER NOT NULL DEFAULT 0,enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_by BIGINT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS commerce_preferences (
            user_id BIGINT PRIMARY KEY,coupon TEXT)""",
        """CREATE TABLE IF NOT EXISTS loyalty_members (
            user_id BIGINT PRIMARY KEY,spent NUMERIC(18,2) NOT NULL DEFAULT 0,
            purchases INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS loyalty_tiers (
            name TEXT PRIMARY KEY,threshold NUMERIC(18,2) NOT NULL UNIQUE CHECK(threshold>=0),
            percent NUMERIC(5,2) NOT NULL CHECK(percent>=0 AND percent<=50))""",
        """INSERT INTO loyalty_tiers(name,threshold,percent) VALUES
            ('أساسي',0,0),('فضي',100,2),('ذهبي',500,5),('بلاتيني',1000,7)
            ON CONFLICT DO NOTHING""",
        """CREATE TABLE IF NOT EXISTS commerce_quotes (
            quote_id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,service TEXT NOT NULL,
            reference TEXT NOT NULL,base NUMERIC(18,2) NOT NULL,amount NUMERIC(18,2) NOT NULL,
            discount NUMERIC(18,2) NOT NULL,coupon TEXT,
            status TEXT NOT NULL DEFAULT 'pending',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP+INTERVAL '5 minutes',
            completed_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS commerce_quote_owner_idx ON commerce_quotes(user_id,reference,created_at DESC)",
        """CREATE TABLE IF NOT EXISTS commerce_redemptions (
            quote_id TEXT PRIMARY KEY REFERENCES commerce_quotes(quote_id),
            code TEXT NOT NULL REFERENCES commerce_coupons(code),user_id BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS commerce_redemption_user_idx ON commerce_redemptions(code,user_id)",
        "ALTER TABLE pending_purchases ADD COLUMN IF NOT EXISTS quote_id TEXT",
        "ALTER TABLE rental_renewal_quotes ADD COLUMN IF NOT EXISTS promotion_quote TEXT",
        # Lease history survives inventory transfer/removal; allocation still checks inventory transactionally.
        "ALTER TABLE rental_leases DROP CONSTRAINT IF EXISTS rental_leases_phone_fkey",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS acquisition_cost NUMERIC(18,2) CHECK(acquisition_cost>=0)",
        """CREATE TABLE IF NOT EXISTS commerce_sales (
            sale_id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,phone TEXT NOT NULL,
            revenue NUMERIC(18,2) NOT NULL,cost NUMERIC(18,2),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS business_expenses (
            expense_id BIGSERIAL PRIMARY KEY,amount NUMERIC(18,2) NOT NULL CHECK(amount>0),
            description TEXT NOT NULL,created_by BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS country_watches (
            user_id BIGINT NOT NULL,country_id BIGINT NOT NULL REFERENCES countries(country_id) ON DELETE CASCADE,
            service TEXT NOT NULL CHECK(service IN ('purchase','rental')),
            favorite BOOLEAN NOT NULL DEFAULT FALSE,stock_alert BOOLEAN NOT NULL DEFAULT FALSE,
            price_alert BOOLEAN NOT NULL DEFAULT FALSE,available BOOLEAN NOT NULL DEFAULT FALSE,
            last_price NUMERIC(18,2),last_count INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id,country_id,service))""",
        "CREATE INDEX IF NOT EXISTS country_watch_alert_idx ON country_watches(country_id,service)",
        "CREATE INDEX IF NOT EXISTS ticket_messages_ticket_idx ON ticket_messages(ticket_id,message_id)",
        """INSERT INTO ticket_messages(ticket_id,user_id,message)
            SELECT t.ticket_id,t.user_id,t.message FROM support_tickets t WHERE
            NOT EXISTS(SELECT 1 FROM ticket_messages m WHERE m.ticket_id=t.ticket_id)""",
        "ALTER TABLE scheduled_operations ADD COLUMN IF NOT EXISTS interval_minutes INTEGER CHECK(interval_minutes BETWEEN 5 AND 10080)",
        "ALTER TABLE scheduled_operations ADD COLUMN IF NOT EXISTS run_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE scheduled_operations ADD COLUMN IF NOT EXISTS started_at TIMESTAMP",
        "ALTER TABLE scheduled_operations ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
        "CREATE INDEX IF NOT EXISTS scheduled_operations_due_idx ON scheduled_operations(due_at) WHERE status='pending'",
    ]
    for statement in statements:
        c.execute(statement)
    if not c.execute("SELECT 1 FROM settings WHERE key='shared_rental_inventory_v1'").fetchone():
        c.execute("""UPDATE accounts a SET is_active=TRUE WHERE NOT is_sold AND
            EXISTS(SELECT 1 FROM rental_leases l WHERE l.phone=a.phone AND l.status='active')""")
        c.execute("INSERT INTO settings(key,value) VALUES('shared_rental_inventory_v1','1')")

    from number_schema import migrate as migrate_number_types
    migrate_number_types(c)
