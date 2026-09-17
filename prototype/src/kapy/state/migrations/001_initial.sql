CREATE TABLE service_meta (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    epoch UUID NOT NULL
);
CREATE TABLE sessions (
    id UUID PRIMARY KEY,
    title TEXT NOT NULL,
    machine_ids JSONB NOT NULL,
    default_machine_id TEXT,
    config JSONB NOT NULL,
    initial_state JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('waiting','running','deleting')),
    latest_run_id UUID,
    next_seq BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE runs (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions ON DELETE CASCADE,
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('running','waiting','failed','interrupted')),
    checkpoint_no INTEGER NOT NULL DEFAULT 0,
    runner_state JSONB NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX one_active_run ON runs(session_id)
    WHERE status IN ('running','interrupted');
CREATE TABLE records (
    session_id UUID NOT NULL REFERENCES sessions ON DELETE CASCADE,
    seq BIGINT NOT NULL,
    run_id UUID,
    attempt INTEGER,
    message_id UUID,
    kind TEXT NOT NULL,
    data JSONB NOT NULL,
    text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    search_vector TSVECTOR NOT NULL,
    emission_id UUID,
    emission_fingerprint TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(session_id, seq),
    UNIQUE(session_id, emission_id)
);
CREATE INDEX records_kind ON records(session_id, kind, seq);
CREATE INDEX records_search ON records USING gin(search_vector)
    WHERE kind IN ('input','model_request','model_response','final','waiting','error');
CREATE TABLE inputs (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions ON DELETE CASCADE,
    event_id UUID,
    mode TEXT NOT NULL CHECK (mode IN ('steer','queue')),
    payload JSONB NOT NULL,
    seq BIGINT NOT NULL,
    run_id UUID,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','reserved','consumed')),
    UNIQUE(event_id, session_id)
);
CREATE INDEX inputs_pending ON inputs(session_id, state, mode, seq);
CREATE TABLE subscriptions (
    channel_id UUID NOT NULL,
    session_id UUID NOT NULL REFERENCES sessions ON DELETE CASCADE,
    PRIMARY KEY(channel_id, session_id)
);
CREATE INDEX subscriptions_session ON subscriptions(session_id);
CREATE TABLE events (
    id UUID PRIMARY KEY,
    ordinal BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    channel_id UUID NOT NULL,
    producer_session_id UUID,
    mode TEXT NOT NULL CHECK (mode IN ('steer','queue')),
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','delivered')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX events_pending ON events(channel_id, ordinal) WHERE state = 'pending';
CREATE TABLE requests (
    id UUID PRIMARY KEY,
    operation TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    receipt JSONB,
    target_session_id UUID,
    input_id UUID,
    waiting_id UUID,
    completed_run_id UUID,
    completion JSONB
);
CREATE INDEX requests_input ON requests(input_id);
CREATE TABLE checkpoints (
    run_id UUID NOT NULL REFERENCES runs ON DELETE CASCADE,
    number INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    cursor_seq BIGINT NOT NULL,
    PRIMARY KEY(run_id, number)
);
