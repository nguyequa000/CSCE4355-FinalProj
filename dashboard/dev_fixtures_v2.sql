-- dashboard/dev_fixtures_v2.sql  —  DEV / SYNTHETIC FIXTURES ONLY, NOT EVIDENCE.
--
-- Companion to dashboard/dev_fixtures.sql for the optional history views used
-- by the table-health, statement and hourly-rollup panels. It:
--   1. creates the contract base tables (monitor.table_stat_sample,
--      monitor.statement_sample) IF NOT EXISTS,
--   2. creates simple stand-in versions of monitor.v_table_stats,
--      monitor.v_statement_stats and monitor.v_hourly_rollup ONLY when a view
--      with that name is missing (the production lane owns the real views),
--   3. grants SELECT on those three views to monitor_reader,
--   4. loads synthetic rows for a throwaway development database.
--
-- Run AFTER dashboard/dev_fixtures.sql, as the cluster superuser:
--
--   bash scripts/setup_db.sh shopdb_d2
--   set -a; . ./.env; set +a
--   PGPASSWORD="$PG_SUPERUSER_PASSWORD" psql -h 127.0.0.1 -p 55432 -U postgres \
--     -d shopdb_d2 -v ON_ERROR_STOP=1 \
--     -f dashboard/dev_fixtures.sql -f dashboard/dev_fixtures_v2.sql
--
-- Never run this against the demo database you intend to show.

-- ---- contract base tables ---------------------------------------------------
CREATE TABLE IF NOT EXISTS monitor.table_stat_sample (
    snapshot_id      bigint NOT NULL REFERENCES monitor.snapshot ON DELETE CASCADE,
    relid            oid    NOT NULL,
    schemaname       text   NOT NULL,
    relname          text   NOT NULL,
    n_live_tup       bigint,
    n_dead_tup       bigint,
    seq_scan         bigint,
    idx_scan         bigint,
    n_tup_ins        bigint,
    n_tup_upd        bigint,
    n_tup_del        bigint,
    last_autovacuum  timestamptz,
    last_autoanalyze timestamptz,
    PRIMARY KEY (snapshot_id, relid)
);

CREATE TABLE IF NOT EXISTS monitor.statement_sample (
    snapshot_id          bigint NOT NULL REFERENCES monitor.snapshot ON DELETE CASCADE,
    queryid               bigint NOT NULL,
    userid                oid,
    calls                 bigint,
    total_exec_time       double precision,
    mean_exec_time        double precision,
    rows                  bigint,
    shared_blks_hit       bigint,
    shared_blks_read      bigint,
    calls_delta           bigint,
    total_exec_time_delta double precision,
    PRIMARY KEY (snapshot_id, queryid, userid)
);

-- ---- simple stand-in views, only when missing --------------------------------
DO $do$
BEGIN
    IF to_regclass('monitor.v_table_stats') IS NULL THEN
        EXECUTE $view$
            CREATE VIEW monitor.v_table_stats AS
            SELECT t.snapshot_id,
                   s.captured_at,
                   t.schemaname,
                   t.relname,
                   t.n_live_tup,
                   t.n_dead_tup,
                   CASE
                       WHEN COALESCE(t.n_live_tup, 0) + COALESCE(t.n_dead_tup, 0) > 0
                       THEN round(
                           COALESCE(t.n_dead_tup, 0)::numeric
                           / (COALESCE(t.n_live_tup, 0) + COALESCE(t.n_dead_tup, 0)),
                           4)
                       ELSE 0::numeric
                   END AS dead_ratio,
                   t.seq_scan,
                   t.idx_scan
              FROM monitor.table_stat_sample t
              JOIN monitor.snapshot s USING (snapshot_id)
        $view$;
    END IF;

    IF to_regclass('monitor.v_statement_stats') IS NULL THEN
        EXECUTE $view$
            CREATE VIEW monitor.v_statement_stats AS
            SELECT st.snapshot_id,
                   s.captured_at,
                   st.queryid,
                   st.calls_delta,
                   st.total_exec_time_delta,
                   st.mean_exec_time
              FROM monitor.statement_sample st
              JOIN monitor.snapshot s USING (snapshot_id)
        $view$;
    END IF;

    IF to_regclass('monitor.v_hourly_rollup') IS NULL THEN
        EXECUTE $view$
            CREATE VIEW monitor.v_hourly_rollup AS
            WITH per_snapshot AS (
                SELECT s.snapshot_id,
                       s.captured_at,
                       (SELECT count(*) FROM monitor.lock_wait lw
                         WHERE lw.snapshot_id = s.snapshot_id) AS lock_edges,
                       (SELECT count(*) FROM monitor.session_sample ss
                         WHERE ss.snapshot_id = s.snapshot_id) AS sessions
                  FROM monitor.snapshot s
            ),
            hourly AS (
                SELECT date_trunc('hour', captured_at) AS hour,
                       count(*)::bigint AS snapshots,
                       max(lock_edges)::bigint AS max_lock_edges,
                       max(sessions)::bigint AS max_sessions
                  FROM per_snapshot
                 GROUP BY 1
            ),
            db AS (
                SELECT date_trunc('hour', s.captured_at) AS hour,
                       sum(d.xact_commit_delta)::bigint AS xact_commit_delta_sum,
                       CASE
                           WHEN COALESCE(sum(d.blks_hit_delta), 0)
                                + COALESCE(sum(d.blks_read_delta), 0) > 0
                           THEN round(
                               COALESCE(sum(d.blks_hit_delta), 0)::numeric
                               / (COALESCE(sum(d.blks_hit_delta), 0)
                                  + COALESCE(sum(d.blks_read_delta), 0)),
                               4)
                           ELSE NULL
                       END AS blks_hit_ratio
                  FROM monitor.db_stat_sample d
                  JOIN monitor.snapshot s USING (snapshot_id)
                 GROUP BY 1
            ),
            al AS (
                SELECT date_trunc('hour', fired_at) AS hour,
                       count(*)::bigint AS alerts_fired
                  FROM monitor.alert_log
                 GROUP BY 1
            )
            SELECT h.hour,
                   h.snapshots,
                   h.max_lock_edges,
                   h.max_sessions,
                   COALESCE(db.xact_commit_delta_sum, 0) AS xact_commit_delta_sum,
                   db.blks_hit_ratio,
                   COALESCE(al.alerts_fired, 0) AS alerts_fired
              FROM hourly h
              LEFT JOIN db USING (hour)
              LEFT JOIN al USING (hour)
        $view$;
    END IF;
END
$do$;

GRANT SELECT ON monitor.v_table_stats, monitor.v_statement_stats, monitor.v_hourly_rollup
      TO monitor_reader;

-- dev_fixtures.sql must have created snapshots 1..6 first
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM monitor.snapshot WHERE snapshot_id = 6) THEN
        RAISE EXCEPTION 'dev_fixtures_v2.sql needs snapshots from dashboard/dev_fixtures.sql; run that file first';
    END IF;
END
$do$;

-- ---- synthetic rows ---------------------------------------------------------
DELETE FROM monitor.table_stat_sample;

-- two shop tables, six snapshots: dead tuples grow, scans shift to indexes
INSERT INTO monitor.table_stat_sample
  (snapshot_id, relid, schemaname, relname, n_live_tup, n_dead_tup, seq_scan, idx_scan,
   n_tup_ins, n_tup_upd, n_tup_del, last_autovacuum, last_autoanalyze)
SELECT s.snapshot_id,
       t.relid,
       t.schemaname,
       t.relname,
       t.live0 + s.snapshot_id * 25,
       t.dead0 + s.snapshot_id * t.dead_step,
       t.seq0 - s.snapshot_id * t.seq_step,
       t.idx0 + s.snapshot_id * t.idx_step,
       s.snapshot_id * 40,
       s.snapshot_id * 15,
       s.snapshot_id * t.dead_step,
       CASE WHEN t.relname = 'orders' THEN s.captured_at - interval '2 hours' END,
       CASE WHEN t.relname = 'orders' THEN s.captured_at - interval '90 minutes' END
FROM monitor.snapshot s
CROSS JOIN (VALUES
    ('shop.orders'::regclass,    'shop', 'orders',    10000, 120, 30, 1200, 60, 8000, 400),
    ('shop.customers'::regclass, 'shop', 'customers',  1000,  10,  5,  300, 10, 2500, 120)
) AS t(relid, schemaname, relname, live0, dead0, dead_step, seq0, seq_step, idx0, idx_step)
WHERE s.snapshot_id BETWEEN 1 AND 6;

DELETE FROM monitor.statement_sample;

-- four queryids per snapshot; no SQL text exists in this contract
INSERT INTO monitor.statement_sample
  (snapshot_id, queryid, userid, calls, total_exec_time, mean_exec_time, rows,
   shared_blks_hit, shared_blks_read, calls_delta, total_exec_time_delta)
SELECT s.snapshot_id,
       q.queryid,
       10::oid AS userid,
       q.calls0 + s.snapshot_id,
       round((q.total0 * s.snapshot_id)::numeric, 2)::double precision,
       round((q.total0 / q.calls0)::numeric, 3)::double precision,
       q.rows0 + s.snapshot_id * 10,
       q.hits0,
       q.reads0,
       q.calls0,
       round((q.total0 * (1 + (s.snapshot_id - 1) * 0.10))::numeric, 2)::double precision
FROM monitor.snapshot s
CROSS JOIN (VALUES
    (8812345678901234567::bigint, 420, 18320.5, 4200, 180000, 1200),
    (8823456789012345678::bigint, 180,  9410.0, 1800,  95000,  700),
    (8834567890123456789::bigint,  90,  5220.0,  900,  42000,  310),
    (8845678901234567890::bigint,  40,  1080.0,  400,   9000,  120)
) AS q(queryid, calls0, total0, rows0, hits0, reads0)
WHERE s.snapshot_id BETWEEN 1 AND 6;

-- the extra rule names may not exist in a database built before sql/22_more_rules.sql
INSERT INTO monitor.alert_rule (rule_name, threshold_seconds, severity, enabled, description)
VALUES
  ('connection_saturation', 0, 'warning', true,
   'DEV/SYNTHETIC fixture rule: connections approaching the configured limit'),
  ('cache_hit_ratio', 0, 'warning', true,
   'DEV/SYNTHETIC fixture rule: database block cache hit ratio below target'),
  ('dead_tuple_risk', 0, 'critical', true,
   'DEV/SYNTHETIC fixture rule: a table has a high dead-tuple ratio')
ON CONFLICT (rule_name) DO NOTHING;

DELETE FROM monitor.alert_log
 WHERE rule_name IN ('connection_saturation', 'cache_hit_ratio', 'dead_tuple_risk',
                     'idle_in_transaction', 'long_active_query');

-- dead_tuple_risk: blocked_pid is the table relid (orders open, customers open)
INSERT INTO monitor.alert_log
  (rule_name, severity, blocked_pid, blocker_pid, first_seen_at, eligible_at,
   fired_at, fired_snapshot_id, observed_seconds, last_seen_snapshot_id,
   resolved_at, resolved_snapshot_id, notified_at, resolve_notified_at)
SELECT 'dead_tuple_risk', 'critical', c.oid::int, 0,
       s.captured_at - interval '45 seconds',
       s.captured_at - interval '15 seconds',
       s.captured_at, s.snapshot_id, 45.0, s.snapshot_id,
       NULL, NULL, s.captured_at + interval '2 seconds', NULL
FROM monitor.snapshot s
JOIN pg_class c ON c.relname = 'orders' AND c.relkind = 'r'
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'shop'
WHERE s.snapshot_id = 5;

INSERT INTO monitor.alert_log
  (rule_name, severity, blocked_pid, blocker_pid, first_seen_at, eligible_at,
   fired_at, fired_snapshot_id, observed_seconds, last_seen_snapshot_id,
   resolved_at, resolved_snapshot_id, notified_at, resolve_notified_at)
SELECT 'dead_tuple_risk', 'critical', c.oid::int, 0,
       s.captured_at - interval '20 seconds',
       s.captured_at - interval '10 seconds',
       s.captured_at, s.snapshot_id, 20.0, s.snapshot_id,
       NULL, NULL, s.captured_at + interval '2 seconds', NULL
FROM monitor.snapshot s
JOIN pg_class c ON c.relname = 'customers' AND c.relkind = 'r'
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'shop'
WHERE s.snapshot_id = 6;

-- database-level rules: blocked_pid = blocker_pid = 0
INSERT INTO monitor.alert_log
  (rule_name, severity, blocked_pid, blocker_pid, first_seen_at, eligible_at,
   fired_at, fired_snapshot_id, observed_seconds, last_seen_snapshot_id,
   resolved_at, resolved_snapshot_id, notified_at, resolve_notified_at)
SELECT 'connection_saturation', 'warning', 0, 0,
       s.captured_at - interval '30 seconds', s.captured_at,
       s.captured_at, s.snapshot_id, 30.0, s.snapshot_id,
       NULL, NULL, s.captured_at + interval '2 seconds', NULL
FROM monitor.snapshot s
WHERE s.snapshot_id = 6;

INSERT INTO monitor.alert_log
  (rule_name, severity, blocked_pid, blocker_pid, first_seen_at, eligible_at,
   fired_at, fired_snapshot_id, observed_seconds, last_seen_snapshot_id,
   resolved_at, resolved_snapshot_id, notified_at, resolve_notified_at)
SELECT 'cache_hit_ratio', 'warning', 0, 0,
       s3.captured_at - interval '20 seconds', s3.captured_at,
       s3.captured_at, 3, 20.0, 3,
       s4.captured_at, 4,
       s3.captured_at + interval '2 seconds', s4.captured_at + interval '2 seconds'
FROM monitor.snapshot s3, monitor.snapshot s4
WHERE s3.snapshot_id = 3 AND s4.snapshot_id = 4;

-- sample-based rules: blocked_pid = blocker_pid = offending pid
INSERT INTO monitor.alert_log
  (rule_name, severity, blocked_pid, blocker_pid, first_seen_at, eligible_at,
   fired_at, fired_snapshot_id, observed_seconds, last_seen_snapshot_id,
   resolved_at, resolved_snapshot_id, notified_at, resolve_notified_at)
SELECT 'idle_in_transaction', 'warning', 4100, 4100,
       s.captured_at - interval '2 minutes', s.captured_at - interval '1 minute',
       s.captured_at, 5, 120.0, 6,
       NULL, NULL, s.captured_at + interval '2 seconds', NULL
FROM monitor.snapshot s
WHERE s.snapshot_id = 5;

INSERT INTO monitor.alert_log
  (rule_name, severity, blocked_pid, blocker_pid, first_seen_at, eligible_at,
   fired_at, fired_snapshot_id, observed_seconds, last_seen_snapshot_id,
   resolved_at, resolved_snapshot_id, notified_at, resolve_notified_at)
SELECT 'long_active_query', 'warning', 4400, 4400,
       s2.captured_at - interval '90 seconds', s2.captured_at - interval '30 seconds',
       s2.captured_at, 2, 90.0, 2,
       s3.captured_at, 3,
       s2.captured_at + interval '2 seconds', s3.captured_at + interval '2 seconds'
FROM monitor.snapshot s2, monitor.snapshot s3
WHERE s2.snapshot_id = 2 AND s3.snapshot_id = 3;

SELECT setval('monitor.alert_log_alert_id_seq',
              (SELECT max(alert_id) FROM monitor.alert_log));

SELECT 'fixtures_v2: table_stats=' || (SELECT count(*) FROM monitor.table_stat_sample)
    || ' statements='  || (SELECT count(*) FROM monitor.statement_sample)
    || ' new_alerts='  || (SELECT count(*) FROM monitor.alert_log
                            WHERE rule_name IN ('connection_saturation', 'cache_hit_ratio',
                                                'dead_tuple_risk', 'idle_in_transaction',
                                                'long_active_query')) AS loaded;
