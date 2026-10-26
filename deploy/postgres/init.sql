-- Keep this schema aligned with libs/common/triage_common/store.py.
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
