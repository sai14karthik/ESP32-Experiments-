-- CSI presence collect → PostgreSQL (Mac Mini)
-- Same DB as csi_pipeline_new (default: postgresql:///csi)
--
--   export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
--   psql postgresql:///csi -f schema.sql
--
-- Or collect_session auto-applies IF NOT EXISTS on connect.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Shared with csi_pipeline_new ingest (safe to re-run)
CREATE TABLE IF NOT EXISTS csi_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ,
    method        TEXT NOT NULL,
    label         TEXT,
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
    device_ts     BIGINT,
    host_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    sig_len       INTEGER,
    rx_format     INTEGER,
    len           INTEGER,
    first_word    INTEGER,
    iq            INTEGER[] NOT NULL,
    source_id     TEXT
);

CREATE INDEX IF NOT EXISTS csi_samples_session_host_ts_idx
    ON csi_samples (session_id, host_ts);
CREATE INDEX IF NOT EXISTS csi_samples_session_seq_idx
    ON csi_samples (session_id, seq);
CREATE INDEX IF NOT EXISTS csi_samples_session_source_idx
    ON csi_samples (session_id, source_id);

ALTER TABLE csi_samples ADD COLUMN IF NOT EXISTS source_id TEXT;

-- Labeled feature windows from csi_presence_c5 collect (empty=0 / occupied=1)
CREATE TABLE IF NOT EXISTS presence_windows (
    id            BIGSERIAL PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES csi_sessions (id) ON DELETE CASCADE,
    host_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    label         SMALLINT NOT NULL CHECK (label IN (0, 1)),
    label_src     TEXT NOT NULL DEFAULT 'cv',
    method        TEXT,
    n_csi         INTEGER,
    sources       TEXT[],
    macs          TEXT[],
    feat          DOUBLE PRECISION[] NOT NULL,
    session_tag   TEXT
);

CREATE INDEX IF NOT EXISTS presence_windows_session_ts_idx
    ON presence_windows (session_id, host_ts);
CREATE INDEX IF NOT EXISTS presence_windows_label_idx
    ON presence_windows (label);
CREATE INDEX IF NOT EXISTS presence_windows_tag_idx
    ON presence_windows (session_tag);
