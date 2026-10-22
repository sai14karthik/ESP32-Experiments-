-- RoomSensor four-board collect. Run on the Mac Mini Postgres (database csi).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS room_presence_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ,
    tag           TEXT,
    master_url    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS room_presence_samples (
    id            BIGSERIAL PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES room_presence_sessions (id) ON DELETE CASCADE,
    host_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    room          SMALLINT,
    moving        SMALLINT,
    calibrating   SMALLINT,
    wander_th     REAL,
    jitter_th     REAL,
    l1_active     SMALLINT,
    l1_room       SMALLINT,
    l1_move       SMALLINT,
    l1_wander     REAL,
    l1_jitter     REAL,
    l2_active     SMALLINT,
    l2_room       SMALLINT,
    l2_move       SMALLINT,
    l2_wander     REAL,
    l2_jitter     REAL,
    l3_active     SMALLINT,
    l3_room       SMALLINT,
    l3_move       SMALLINT,
    l3_wander     REAL,
    l3_jitter     REAL
);

CREATE INDEX IF NOT EXISTS room_presence_samples_session_ts_idx
    ON room_presence_samples (session_id, host_ts);
