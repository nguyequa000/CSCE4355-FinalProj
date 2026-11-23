-- dashboard/dev_fixtures.sql  —  DEV / SYNTHETIC FIXTURES ONLY, NOT EVIDENCE.
-- Loads six fake snapshots, two lock incidents (one resolved, one open) and
-- two alerts into monitor.* so the dashboard can be exercised before the live
-- demo produces real data. Run as the cluster superuser against a throwaway db:
--
--   set -a; . ./.env; set +a
--   PGPASSWORD="$PG_SUPERUSER_PASSWORD" psql -h 127.0.0.1 -p 55432 -U postgres \
--     -d shopdb_t4 -v ON_ERROR_STOP=1 -f dashboard/dev_fixtures.sql
--
-- Never run this against the demo database you intend to show.

TRUNCATE monitor.alert_log, monitor.db_stat_sample, monitor.lock_wait,
         monitor.session_sample, monitor.snapshot RESTART IDENTITY CASCADE;

-- six snapshots, 45 s apart, latest 15 s old
INSERT INTO monitor.snapshot (snapshot_id, captured_at, collector_pid)
SELECT k,
       now() - interval '15 seconds' - make_interval(secs => (6 - k) * 45),
       4300
FROM generate_series(1, 6) AS k;

-- session samples -----------------------------------------------------------
-- steady background sessions on every snapshot
INSERT INTO monitor.session_sample
  (snapshot_id, pid, usename, application_name, state, wait_event_type, wait_event,
   xact_start, query_start, state_change, blocked_by, query_excerpt)
SELECT k, p.pid, 'shop_app', p.app, p.state, p.wet, p.we,
       NULL, s.captured_at - interval '3 seconds', s.captured_at - interval '3 seconds',
       '{}', p.q
FROM monitor.snapshot s
JOIN generate_series(1, 6) AS k ON k = s.snapshot_id
CROSS JOIN (VALUES
  (4400, 'psql',              'active', NULL,   NULL,            'select * from shop.orders limit 50'),
  (4401, 'psql',              'idle',   NULL,   NULL,            NULL),
  (4300, 'monitor_collector', 'idle',   NULL,   NULL,            NULL)
) AS p(pid, app, state, wet, we, q);

-- incident 1 (resolved): 4051 waits on 4050, snapshots 1-3
INSERT INTO monitor.session_sample
  (snapshot_id, pid, usename, application_name, state, wait_event_type, wait_event,
   xact_start, query_start, state_change, blocked_by, query_excerpt)
SELECT k, 4050, 'shop_app', 'psql', 'idle in transaction', NULL, NULL,
       s.captured_at - interval '2 minutes', NULL,
       s.captured_at - interval '90 seconds', '{}', 'update shop.orders set status=$1'
FROM monitor.snapshot s JOIN generate_series(1, 3) AS k ON k = s.snapshot_id;

INSERT INTO monitor.session_sample
  (snapshot_id, pid, usename, application_name, state, wait_event_type, wait_event,
   xact_start, query_start, state_change, blocked_by, query_excerpt)
SELECT k, 4051, 'shop_app', 'psql', 'active', 'Lock', 'transactionid',
       s.captured_at - interval '40 seconds', s.captured_at - interval '40 seconds',
       s.captured_at - make_interval(secs => 12 + (k - 1) * 45), ARRAY[4050],
       'update shop.orders set status=$1 where order_id=$2'
FROM monitor.snapshot s JOIN generate_series(1, 3) AS k ON k = s.snapshot_id;

-- incident 2 (still open): 4101 waits on 4100, snapshots 4-6
INSERT INTO monitor.session_sample
  (snapshot_id, pid, usename, application_name, state, wait_event_type, wait_event,
   xact_start, query_start, state_change, blocked_by, query_excerpt)
SELECT k, 4100, 'shop_app', 'psql', 'idle in transaction', NULL, NULL,
       s.captured_at - interval '2 minutes', NULL,
       s.captured_at - interval '100 seconds', '{}', 'delete from shop.order_items where order_id=$1'
FROM monitor.snapshot s JOIN generate_series(4, 6) AS k ON k = s.snapshot_id;

INSERT INTO monitor.session_sample
  (snapshot_id, pid, usename, application_name, state, wait_event_type, wait_event,
   xact_start, query_start, state_change, blocked_by, query_excerpt)
SELECT k, 4101, 'shop_app', 'psql', 'active', 'Lock', 'transactionid',
       s.captured_at - interval '55 seconds', s.captured_at - interval '55 seconds',
       s.captured_at - make_interval(secs => 8.4 + (k - 4) * 45), ARRAY[4100],
       'delete from shop.order_items where order_id=$1'
FROM monitor.snapshot s JOIN generate_series(4, 6) AS k ON k = s.snapshot_id;

-- recent second waiter at the latest snapshot only, below the 30 s threshold
INSERT INTO monitor.session_sample
  (snapshot_id, pid, usename, application_name, state, wait_event_type, wait_event,
   xact_start, query_start, state_change, blocked_by, query_excerpt)
SELECT 6, 4151, 'shop_app', 'psql', 'active', 'Lock', 'tuple',
       s.captured_at - interval '21 seconds', s.captured_at - interval '21 seconds',
       s.captured_at - interval '21 seconds', ARRAY[4100],
       'select * from shop.customers where customer_id=$1 for update'
FROM monitor.snapshot s WHERE s.snapshot_id = 6;

INSERT INTO monitor.session_sample
  (snapshot_id, pid, usename, application_name, state, wait_event_type, wait_event,
   xact_start, query_start, state_change, blocked_by, query_excerpt)
SELECT 6, 4150, 'shop_app', 'psql', 'idle in transaction', NULL, NULL,
       s.captured_at - interval '30 seconds', NULL, s.captured_at - interval '26 seconds',
       '{}', 'update shop.customers set loyalty_points=$1'
FROM monitor.snapshot s WHERE s.snapshot_id = 6;

-- lock history --------------------------------------------------------------
INSERT INTO monitor.lock_wait
  (snapshot_id, blocked_pid, blocker_pid, blocked_user, blocker_user,
   blocked_app, blocker_app, blocker_state, wait_started, waited_seconds)
SELECT k, 4051, 4050, 'shop_app', 'shop_app', 'psql', 'psql',
       'idle in transaction',
       s.captured_at - make_interval(secs => 12 + (k - 1) * 45),
       12 + (k - 1) * 45
FROM monitor.snapshot s JOIN generate_series(1, 3) AS k ON k = s.snapshot_id;

INSERT INTO monitor.lock_wait
  (snapshot_id, blocked_pid, blocker_pid, blocked_user, blocker_user,
   blocked_app, blocker_app, blocker_state, wait_started, waited_seconds)
SELECT k, 4101, 4100, 'shop_app', 'shop_app', 'psql', 'psql',
       'idle in transaction',
       s.captured_at - make_interval(secs => 8.4 + (k - 4) * 45),
       8.4 + (k - 4) * 45
FROM monitor.snapshot s JOIN generate_series(4, 6) AS k ON k = s.snapshot_id;

INSERT INTO monitor.lock_wait
  (snapshot_id, blocked_pid, blocker_pid, blocked_user, blocker_user,
   blocked_app, blocker_app, blocker_state, wait_started, waited_seconds)
SELECT 6, 4151, 4100, 'shop_app', 'shop_app', 'psql', 'psql',
       'idle in transaction', s.captured_at - interval '21 seconds', 21.0
FROM monitor.snapshot s WHERE s.snapshot_id = 6;

-- database counters: first sample has NULL deltas (unknown, not zero)
INSERT INTO monitor.db_stat_sample
  (snapshot_id, datname, numbackends, xact_commit, xact_rollback, blks_read, blks_hit,
   deadlocks, stats_reset, xact_commit_delta, xact_rollback_delta, blks_read_delta, blks_hit_delta)
SELECT k, d.datname, 6 + k,
       100000 + k * 410, 900 + k * 3, 5000 + k * 120, 900000 + k * 55000,
       NULL, now() - interval '1 day',
       CASE WHEN k = 1 THEN NULL ELSE 410 END,
       CASE WHEN k = 1 THEN NULL ELSE 3 END,
       CASE WHEN k = 1 THEN NULL ELSE 120 END,
       CASE WHEN k = 1 THEN NULL ELSE 55000 END
FROM monitor.snapshot s
JOIN generate_series(1, 6) AS k ON k = s.snapshot_id
CROSS JOIN (SELECT current_database() AS datname) AS d;

-- alerts --------------------------------------------------------------------
INSERT INTO monitor.alert_log
  (alert_id, rule_name, severity, blocked_pid, blocker_pid, first_seen_at, eligible_at,
   fired_at, fired_snapshot_id, observed_seconds, last_seen_snapshot_id, resolved_at,
   resolved_snapshot_id, notified_at, resolve_notified_at)
SELECT 1, 'blocked_session', 'critical', 4051, 4050,
       (SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = 1) - interval '12 seconds',
       (SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = 1) + interval '18 seconds',
       (SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = 2),
       2, 102.0, 3,
       (SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = 4),
       4,
       (SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = 2) + interval '2 seconds',
       (SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = 4) + interval '2 seconds';

INSERT INTO monitor.alert_log
  (alert_id, rule_name, severity, blocked_pid, blocker_pid, first_seen_at, eligible_at,
   fired_at, fired_snapshot_id, observed_seconds, last_seen_snapshot_id, resolved_at,
   resolved_snapshot_id, notified_at, resolve_notified_at)
SELECT 2, 'blocked_session', 'critical', 4101, 4100,
       (SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = 4) - interval '8.4 seconds',
       (SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = 4) + interval '21.6 seconds',
       (SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = 5),
       5, 98.4, 6, NULL, NULL,
       (SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = 5) + interval '2 seconds',
       NULL;

SELECT setval('monitor.snapshot_snapshot_id_seq',
              (SELECT max(snapshot_id) FROM monitor.snapshot));
SELECT setval('monitor.alert_log_alert_id_seq',
              (SELECT max(alert_id) FROM monitor.alert_log));
SELECT 'fixtures: snapshots=' || (SELECT count(*) FROM monitor.snapshot)
    || ' sessions='  || (SELECT count(*) FROM monitor.session_sample)
    || ' lock_edges=' || (SELECT count(*) FROM monitor.lock_wait)
    || ' alerts='    || (SELECT count(*) FROM monitor.alert_log) AS loaded;
