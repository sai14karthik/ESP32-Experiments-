-- ============================================================================
-- CSI pipeline — useful queries
--
-- This is a snippet library, not a script. Open psql and paste the block you
-- want:
--   psql postgresql:///csi
--
-- Do NOT run it with  psql -f queries.sql  — section 6 would drop your data.
--
-- Many blocks need a session id. Set it once per psql session:
--   \set sid '0b4db575-bc04-4279-b4af-07ed9fbe5124'
-- then use  :'sid'  wherever it appears.
--
-- Long iq arrays flood the screen. Turn the pager off first:
--   \pset pager off
-- ============================================================================


-- ############################################################################
-- 1. INSPECT — what is in the database
-- ############################################################################

-- All capture runs, newest first.
SELECT id, method, label, recv_port, channel, baud,
       started_at, ended_at,
       ended_at - started_at AS duration
FROM csi_sessions
ORDER BY started_at DESC;

-- Packet count and time span per run.
SELECT s.label,
       s.method,
       count(*)                                              AS packets,
       min(c.host_ts)                                        AS first_packet,
       max(c.host_ts)                                        AS last_packet,
       round(EXTRACT(EPOCH FROM max(c.host_ts) - min(c.host_ts))::numeric, 2) AS span_s
FROM csi_samples c
JOIN csi_sessions s ON s.id = c.session_id
GROUP BY s.id, s.label, s.method
ORDER BY max(c.host_ts) DESC;

-- Packets per session, including sessions that recorded nothing.
-- Note: this count is normally a few rows higher than the last
-- "inserted total=N" line printed during capture, because the final flush on
-- Ctrl+C is reported by the "stopped ... rows=N" line instead.
SELECT s.label,
       s.started_at,
       count(c.id) AS packets
FROM csi_sessions s
LEFT JOIN csi_samples c ON c.session_id = s.id
GROUP BY s.id, s.label, s.started_at
ORDER BY s.started_at;

-- Sessions that were never closed (process killed instead of Ctrl+C).
SELECT id, label, started_at
FROM csi_sessions
WHERE ended_at IS NULL
ORDER BY started_at DESC;

-- Recent packets, readable. NEVER "SELECT *" here: iq is 234 ints wide.
SELECT seq, mac, rssi, channel, len, host_ts, iq[1:6] AS iq_head
FROM csi_samples
ORDER BY host_ts DESC
LIMIT 20;

-- Full header of a single packet, plus array endpoints.
SELECT seq, mac, rssi, rate, noise_floor, fft_gain, agc_gain, channel,
       device_ts, host_ts, sig_len, rx_format, len, first_word,
       array_length(iq, 1)     AS iq_n,
       array_length(iq, 1) / 2 AS iq_pairs,
       iq[1:4]                 AS first_two_pairs,
       iq[231:234]             AS last_two_pairs
FROM csi_samples
ORDER BY host_ts DESC
LIMIT 1;


-- ############################################################################
-- 2. HEALTH — is the capture trustworthy
-- ############################################################################

-- Quick whole-database sanity check. Expect iq_ok = t, len = 234, and a
-- single mac / channel for a §4.3 setup.
SELECT count(*)                                AS packets,
       bool_and(array_length(iq, 1) = len)     AS iq_ok,
       min(len)                                AS len,
       count(DISTINCT mac)                     AS macs,
       count(DISTINCT channel)                 AS channels
FROM csi_samples;

-- Integrity summary per run. Every boolean should be true, bad_iq should be 0.
SELECT s.label,
       count(*)                                       AS packets,
       bool_and(array_length(c.iq, 1) = c.len)        AS iq_length_matches_len,
       bool_and(array_length(c.iq, 1) % 2 = 0)        AS iq_pairs_complete,
       count(DISTINCT c.len)                          AS distinct_len_values,
       count(DISTINCT c.mac)                          AS distinct_macs,
       count(DISTINCT c.channel)                      AS distinct_channels,
       min(c.rssi)                                    AS min_rssi,
       max(c.rssi)                                    AS max_rssi,
       count(*) FILTER (WHERE c.iq IS NULL
                           OR array_length(c.iq, 1) = 0) AS bad_iq
FROM csi_samples c
JOIN csi_sessions s ON s.id = c.session_id
GROUP BY s.id, s.label
ORDER BY s.label;

-- Effective sample rate per run (rows per second actually stored).
SELECT s.label,
       count(*) AS packets,
       round(EXTRACT(EPOCH FROM max(c.host_ts) - min(c.host_ts))::numeric, 2) AS span_s,
       round((count(*) / NULLIF(EXTRACT(EPOCH FROM max(c.host_ts) - min(c.host_ts)), 0))::numeric, 1)
         AS packets_per_sec
FROM csi_samples c
JOIN csi_sessions s ON s.id = c.session_id
GROUP BY s.id, s.label
ORDER BY s.label;

-- Sampling jitter for one run. Wide min/max means non-uniform sampling,
-- which matters for any FFT-based analysis.
WITH gaps AS (
    SELECT EXTRACT(EPOCH FROM host_ts - lag(host_ts) OVER (ORDER BY host_ts)) AS dt
    FROM csi_samples
    WHERE session_id = :'sid'
)
SELECT round(min(dt)::numeric, 4)                                      AS min_gap_s,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY dt)::numeric, 4) AS median_gap_s,
       round(avg(dt)::numeric, 4)                                      AS mean_gap_s,
       round(max(dt)::numeric, 4)                                      AS max_gap_s
FROM gaps
WHERE dt IS NOT NULL;

-- Packet loss: the sender increments seq every packet, so gaps are drops.
WITH d AS (
    SELECT seq, seq - lag(seq) OVER (ORDER BY seq) AS step
    FROM csi_samples
    WHERE session_id = :'sid'
)
SELECT count(*) FILTER (WHERE step > 1) AS gap_events,
       coalesce(sum(step - 1) FILTER (WHERE step > 1), 0) AS packets_missed,
       max(step) - 1 AS largest_gap
FROM d;

-- RSSI drift over a run, bucketed per second.
SELECT date_trunc('second', host_ts) AS t,
       count(*)                      AS packets,
       round(avg(rssi)::numeric, 1)  AS avg_rssi
FROM csi_samples
WHERE session_id = :'sid'
GROUP BY 1
ORDER BY 1;


-- ############################################################################
-- 3. SIGNAL — amplitude and phase from the raw iq array
--
-- iq is imag,real interleaved. For subcarrier k (1-based):
--   imag = iq[2k-1],  real = iq[2k]
--   amplitude = sqrt(real^2 + imag^2),  phase = atan2(imag, real)
-- ############################################################################

-- Per-subcarrier amplitude and phase for one packet.
SELECT k                                                        AS subcarrier,
       c.iq[2 * k]                                              AS re,
       c.iq[2 * k - 1]                                          AS im,
       round(sqrt((c.iq[2 * k]::double precision) ^ 2
                + (c.iq[2 * k - 1]::double precision) ^ 2)::numeric, 4) AS amplitude,
       round(atan2(c.iq[2 * k - 1]::double precision,
                   c.iq[2 * k]::double precision)::numeric, 4)  AS phase_rad
FROM csi_samples c,
     LATERAL generate_series(1, array_length(c.iq, 1) / 2) AS k
WHERE c.id = (SELECT max(id) FROM csi_samples WHERE session_id = :'sid')
ORDER BY k;

-- Mean amplitude per subcarrier across a whole run — the channel profile.
SELECT k AS subcarrier,
       count(*) AS packets,
       round(avg(sqrt((c.iq[2 * k]::double precision) ^ 2
                    + (c.iq[2 * k - 1]::double precision) ^ 2))::numeric, 3) AS mean_amp,
       round(stddev_samp(sqrt((c.iq[2 * k]::double precision) ^ 2
                            + (c.iq[2 * k - 1]::double precision) ^ 2))::numeric, 3) AS std_amp
FROM csi_samples c,
     LATERAL generate_series(1, array_length(c.iq, 1) / 2) AS k
WHERE c.session_id = :'sid'
GROUP BY k
ORDER BY k;

-- Dead bins: subcarriers that are zero in every packet (nulls / DC / guard).
-- Drop these before feeding features to a model; they carry no variance.
SELECT k AS subcarrier
FROM csi_samples c,
     LATERAL generate_series(1, array_length(c.iq, 1) / 2) AS k
WHERE c.session_id = :'sid'
GROUP BY k
HAVING bool_and(c.iq[2 * k] = 0 AND c.iq[2 * k - 1] = 0)
ORDER BY k;

-- Time series of one subcarrier's amplitude (change the 30 to pick a bin).
-- This is the shape you would feed to a breathing / motion detector.
SELECT host_ts,
       round(sqrt((iq[2 * 30]::double precision) ^ 2
                + (iq[2 * 30 - 1]::double precision) ^ 2)::numeric, 3) AS amp_sc30
FROM csi_samples
WHERE session_id = :'sid'
ORDER BY host_ts;

-- Compare two runs by their mean amplitude profile (e.g. empty vs occupied).
WITH prof AS (
    SELECT s.label,
           k,
           avg(sqrt((c.iq[2 * k]::double precision) ^ 2
                  + (c.iq[2 * k - 1]::double precision) ^ 2)) AS mean_amp
    FROM csi_samples c
    JOIN csi_sessions s ON s.id = c.session_id,
         LATERAL generate_series(1, array_length(c.iq, 1) / 2) AS k
    WHERE s.label IN ('baseline_empty', 'occupied')   -- <<< edit labels
    GROUP BY s.label, k
)
SELECT k AS subcarrier,
       round(max(mean_amp) FILTER (WHERE label = 'baseline_empty')::numeric, 3) AS empty_amp,
       round(max(mean_amp) FILTER (WHERE label = 'occupied')::numeric, 3)       AS occupied_amp,
       round((max(mean_amp) FILTER (WHERE label = 'occupied')
            - max(mean_amp) FILTER (WHERE label = 'baseline_empty'))::numeric, 3) AS delta
FROM prof
GROUP BY k
ORDER BY abs(coalesce(max(mean_amp) FILTER (WHERE label = 'occupied')
                    - max(mean_amp) FILTER (WHERE label = 'baseline_empty'), 0)) DESC;


-- ############################################################################
-- 4. EXPORT — get data out for Python / MATLAB
-- ############################################################################

-- One run to CSV, iq as a bracketed array string. Run from psql (\copy is
-- client side, so the path is on your machine, not the server).
--   \copy (SELECT seq, host_ts, rssi, len, iq FROM csi_samples WHERE session_id = :'sid' ORDER BY host_ts) TO 'session.csv' WITH CSV HEADER

-- Long format: one row per (packet, subcarrier). Large — filter first.
--   \copy (SELECT c.seq, c.host_ts, k AS subcarrier, c.iq[2*k] AS re, c.iq[2*k-1] AS im FROM csi_samples c, LATERAL generate_series(1, array_length(c.iq,1)/2) k WHERE c.session_id = :'sid' ORDER BY c.host_ts, k) TO 'session_long.csv' WITH CSV HEADER

-- Session metadata for all runs.
--   \copy (SELECT * FROM csi_sessions ORDER BY started_at) TO 'sessions.csv' WITH CSV HEADER


-- ############################################################################
-- 5. MAINTENANCE
-- ############################################################################

-- Disk used by each table, including indexes.
SELECT relname AS table_name,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
       pg_size_pretty(pg_relation_size(relid))       AS table_size,
       n_live_tup                                    AS approx_rows
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC;

-- Reclaim space and refresh planner stats after large deletes.
VACUUM ANALYZE csi_samples;
VACUUM ANALYZE csi_sessions;


-- ############################################################################
-- 6. DELETE — destructive. Read the SELECT above each one first.
--
-- csi_samples has ON DELETE CASCADE, so deleting a session also deletes all
-- of its packets. You never need to delete from csi_samples by hand.
-- ############################################################################

-- Preview, then delete, one specific run.
SELECT id, label, started_at,
       (SELECT count(*) FROM csi_samples WHERE session_id = csi_sessions.id) AS packets
FROM csi_sessions
WHERE id = :'sid';

DELETE FROM csi_sessions WHERE id = :'sid';

-- Delete every run carrying a given label (repeated test runs).
SELECT label, count(*) AS sessions FROM csi_sessions
WHERE label = 'dryrun' GROUP BY label;

DELETE FROM csi_sessions WHERE label = 'dryrun';

-- Delete junk runs: fewer than 10 packets (usually aborted starts).
SELECT s.id, s.label, s.started_at, count(c.id) AS packets
FROM csi_sessions s
LEFT JOIN csi_samples c ON c.session_id = s.id
GROUP BY s.id
HAVING count(c.id) < 10;

DELETE FROM csi_sessions s
WHERE (SELECT count(*) FROM csi_samples c WHERE c.session_id = s.id) < 10;

-- Delete runs older than 30 days.
SELECT id, label, started_at FROM csi_sessions
WHERE started_at < now() - INTERVAL '30 days';

DELETE FROM csi_sessions
WHERE started_at < now() - INTERVAL '30 days';

-- Close sessions left open by a killed process (keeps their data).
UPDATE csi_sessions
SET ended_at = now()
WHERE ended_at IS NULL;

-- Wipe everything but keep the tables. Cannot be undone.
TRUNCATE csi_samples, csi_sessions RESTART IDENTITY CASCADE;

-- Drop the schema entirely. Re-create with:  psql postgresql:///csi -f schema.sql
-- DROP TABLE IF EXISTS csi_samples, csi_sessions;
