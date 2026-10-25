"""Immediate membership rewards and durable departure monitoring."""


def migrate(c):
    for statement in [
        "ALTER TABLE funding_config ADD COLUMN IF NOT EXISTS reward_policy INTEGER NOT NULL DEFAULT 1",
        "UPDATE funding_config SET guarantee_seconds=0,reward_policy=2 WHERE reward_policy<2",
        "ALTER TABLE funding_wallets DROP CONSTRAINT IF EXISTS funding_wallets_points_check",
        "ALTER TABLE funding_tasks ADD COLUMN IF NOT EXISTS penalty_multiplier INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS funding_membership_watch(
            watch_id BIGSERIAL PRIMARY KEY,task_id TEXT NOT NULL UNIQUE,campaign_id BIGINT NOT NULL,
            target_id BIGINT NOT NULL,user_id BIGINT NOT NULL,title TEXT NOT NULL,reward BIGINT NOT NULL CHECK(reward>0),
            penalty_amount BIGINT NOT NULL CHECK(penalty_amount>0),status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','penalized')),
            granted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            next_check TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP+INTERVAL '5 minutes',
            lease_until TIMESTAMPTZ,last_checked TIMESTAMPTZ,last_result TEXT,penalized_at TIMESTAMPTZ,
            UNIQUE(target_id,user_id))""",
        "CREATE INDEX IF NOT EXISTS membership_watch_due_idx ON funding_membership_watch(next_check) WHERE status='active'",
        """CREATE TABLE IF NOT EXISTS task_cleanup_batches(batch_id TEXT PRIMARY KEY,actor_id BIGINT NOT NULL,
            target_user BIGINT,expires_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP+INTERVAL '10 minutes',
            status TEXT NOT NULL DEFAULT 'pending',result TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS task_cleanup_candidates(batch_id TEXT NOT NULL REFERENCES task_cleanup_batches(batch_id) ON DELETE CASCADE,
            kind TEXT NOT NULL,item_id TEXT NOT NULL,PRIMARY KEY(batch_id,kind,item_id))""",
    ]:
        c.execute(statement)
