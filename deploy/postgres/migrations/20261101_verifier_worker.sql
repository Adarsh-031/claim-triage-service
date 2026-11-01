-- Apply once to existing PostgreSQL volumes before deploying verifier-worker (#28).
-- Fresh databases receive the same schema from deploy/postgres/init.sql.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS resume_count integer NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS drafts (
    run_id uuid NOT NULL,
    run_attempt integer NOT NULL,
    verify_attempt integer NOT NULL DEFAULT 0,
    state json NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'pending',
    claim_count integer NOT NULL DEFAULT 0,
    agent_host varchar(255),
    verifier_host varchar(255),
    heartbeat_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (run_id, run_attempt)
);
CREATE INDEX IF NOT EXISTS ix_drafts_status_updated ON drafts (status, updated_at);
