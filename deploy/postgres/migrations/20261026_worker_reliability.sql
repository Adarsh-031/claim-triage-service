-- Apply once to existing PostgreSQL volumes before deploying the reliable worker.
-- Fresh databases receive the same columns from deploy/postgres/init.sql.
ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS requeue_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS started_at timestamptz,
    ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz,
    ADD COLUMN IF NOT EXISTS finished_at timestamptz;
