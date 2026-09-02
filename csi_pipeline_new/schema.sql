-- CSI capture pipeline (Mac Mini → PostgreSQL)
-- Apply: psql "$DATABASE_URL" -f schema.sql

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS csi_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ,
    method        TEXT NOT NULL,          -- e.g. '4.1' | '4.2' | '4.3'
    label         TEXT,                  -- human notes: subject, distance, activity
    recv_port     TEXT,
    baud          INTEGER,
    channel       INTEGER,
    host          TEXT,
    git_commit    TEXT
);

CREATE TABLE IF NOT EXISTS csi_samples (
    id            BIGSERIAL PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES csi_sessions (id) ON DELETE CASCADE,
    seq           INTEGER,
    mac           TEXT,
    rssi          INTEGER,
    rate          INTEGER,
    noise_floor   INTEGER,
    fft_gain      INTEGER,
    agc_gain      INTEGER,
    channel       INTEGER,
    device_ts     BIGINT,                -- firmware local_timestamp
    host_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    sig_len       INTEGER,
    rx_format     INTEGER,
    len           INTEGER,
    first_word    INTEGER,
    iq            INTEGER[] NOT NULL     -- imag,real interleaved; length = len
);

CREATE INDEX IF NOT EXISTS csi_samples_session_host_ts_idx
    ON csi_samples (session_id, host_ts);

CREATE INDEX IF NOT EXISTS csi_samples_session_seq_idx
    ON csi_samples (session_id, seq);
