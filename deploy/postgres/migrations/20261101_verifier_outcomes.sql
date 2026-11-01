-- Apply once to existing PostgreSQL volumes before deploying the grounding verifier (#27).
-- Fresh databases receive the same table from deploy/postgres/init.sql.
CREATE TABLE IF NOT EXISTS verifier_outcomes (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    run_attempt integer NOT NULL,
    verifier_attempt integer NOT NULL,
    verified boolean NOT NULL,
    failed_checks json NOT NULL,
    passed_checks json NOT NULL,
    detail json NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_verifier_outcomes_run_id ON verifier_outcomes (run_id);
