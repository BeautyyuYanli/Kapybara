-- This protocol does not convert old sessions, receipts or channel semantics.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM sessions)
    OR EXISTS (SELECT 1 FROM requests)
    OR EXISTS (SELECT 1 FROM subscriptions)
    OR EXISTS (SELECT 1 FROM events) THEN
        RAISE EXCEPTION 'Old session state is unsupported; use a new schema';
    END IF;
END $$;

CREATE TABLE waiting_channels (
    id UUID PRIMARY KEY,
    request_id UUID UNIQUE,
    producer_session_id UUID,
    receiver_session_id UUID,
    active BOOLEAN NOT NULL DEFAULT false,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','ready','delivered')),
    output JSONB,
    mode TEXT NOT NULL DEFAULT 'steer' CHECK (mode IN ('steer','queue')),
    run_id UUID,
    outcome TEXT CHECK (outcome IN ('completed','failed','deleted')),
    completed_at TIMESTAMPTZ,
    cursor TEXT
);
CREATE INDEX waiting_channels_receiver ON waiting_channels(receiver_session_id) WHERE active;
ALTER TABLE inputs ADD COLUMN being_waited_id UUID;
ALTER TABLE inputs ADD CONSTRAINT inputs_one_channel_delivery UNIQUE(event_id);
DROP TABLE subscriptions;
DROP TABLE events;
