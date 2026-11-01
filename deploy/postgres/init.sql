-- Fresh volumes only. Existing volumes: apply deploy/postgres/migrations/*.sql (see docs/runbooks.md).
-- Python supplies UUID and timestamp defaults for normal application writes.
CREATE TABLE IF NOT EXISTS runs (
    id uuid PRIMARY KEY,
    status varchar(20) NOT NULL DEFAULT 'queued',
    claim json NOT NULL,
    result json,
    trace json,
    error text,
    requeue_count integer NOT NULL DEFAULT 0,
    started_at timestamptz,
    heartbeat_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
    id uuid PRIMARY KEY,
    raw_body text NOT NULL,
    errors json NOT NULL,
    client_id varchar(255) NOT NULL DEFAULT 'unknown',
    received_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    agree boolean NOT NULL,
    note text,
    created_at timestamptz NOT NULL
);

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
