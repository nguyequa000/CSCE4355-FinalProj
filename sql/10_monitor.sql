-- 10_monitor.sql  (run as superuser, connected to the project database)
-- Monitoring history schema. Owned by the collector role; the reader role sees
-- only the sanitized v_* reporting views; shop_app sees nothing here.
CREATE SCHEMA IF NOT EXISTS monitor AUTHORIZATION monitor_collector;
REVOKE ALL ON SCHEMA monitor FROM PUBLIC;

SET ROLE monitor_collector;

CREATE TABLE IF NOT EXISTS monitor.snapshot (
    snapshot_id   bigserial PRIMARY KEY,
    captured_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    collector_pid integer     NOT NULL DEFAULT pg_backend_pid()
);

-- one row per client backend per snapshot (credentials / full SQL text not stored;
-- only the first 60 chars of the statement are kept for context)
CREATE TABLE IF NOT EXISTS monitor.session_sample (
    snapshot_id      bigint  NOT NULL REFERENCES monitor.snapshot ON DELETE CASCADE,
    pid              integer NOT NULL,
    usename          text,
    application_name text,
    state            text,
    wait_event_type  text,
    wait_event       text,
    xact_start       timestamptz,
    query_start      timestamptz,
    state_change     timestamptz,
    blocked_by       integer[] NOT NULL DEFAULT '{}',
    query_excerpt    text,
    PRIMARY KEY (snapshot_id, pid)
);

-- one row per (waiter, blocker) edge: "who blocked whom" is preserved historically
CREATE TABLE IF NOT EXISTS monitor.lock_wait (
    snapshot_id      bigint  NOT NULL REFERENCES monitor.snapshot ON DELETE CASCADE,
    blocked_pid      integer NOT NULL,
    blocker_pid      integer NOT NULL,
    blocked_user     text,
    blocker_user     text,
    blocked_app      text,
    blocker_app      text,
    blocker_state    text,
    wait_started     timestamptz,   -- state_change of the waiting backend
    waited_seconds   numeric(10,1), -- captured_at - wait_started
    PRIMARY KEY (snapshot_id, blocked_pid, blocker_pid)
);

-- pg_stat_database counters; *_delta is NULL on the first sample or after a
-- stats reset (never negative)
CREATE TABLE IF NOT EXISTS monitor.db_stat_sample (
    snapshot_id     bigint NOT NULL REFERENCES monitor.snapshot ON DELETE CASCADE,
    datname         text   NOT NULL,
    numbackends     integer,
    xact_commit     bigint,
    xact_rollback   bigint,
    blks_read       bigint,
    blks_hit        bigint,
    deadlocks       bigint,
    stats_reset     timestamptz,
    xact_commit_delta   bigint,
    xact_rollback_delta bigint,
    blks_read_delta     bigint,
    blks_hit_delta      bigint,
    PRIMARY KEY (snapshot_id, datname)
);

CREATE TABLE IF NOT EXISTS monitor.alert_rule (
    rule_name         text PRIMARY KEY,
    threshold_seconds integer NOT NULL,
    severity          text    NOT NULL CHECK (severity IN ('info','warning','critical')),
    enabled           boolean NOT NULL DEFAULT true,
    description       text
);
INSERT INTO monitor.alert_rule VALUES
  ('blocked_session', 30, 'critical', true,
   'A backend has waited on a lock held by another backend for >= threshold_seconds at sample time')
ON CONFLICT (rule_name) DO NOTHING;

CREATE TABLE IF NOT EXISTS monitor.alert_log (
    alert_id            bigserial PRIMARY KEY,
    rule_name           text NOT NULL REFERENCES monitor.alert_rule,
    severity            text NOT NULL,
    blocked_pid         integer NOT NULL,
    blocker_pid         integer NOT NULL,
    first_seen_at       timestamptz NOT NULL,  -- earliest wait_started observed
    eligible_at         timestamptz NOT NULL,  -- first_seen_at + threshold
    fired_at            timestamptz NOT NULL,  -- snapshot time the alert was opened
    fired_snapshot_id   bigint NOT NULL REFERENCES monitor.snapshot,
    observed_seconds    numeric(10,1) NOT NULL, -- latest observed wait
    last_seen_snapshot_id bigint NOT NULL REFERENCES monitor.snapshot,
    resolved_at         timestamptz,
    resolved_snapshot_id bigint REFERENCES monitor.snapshot,
    notified_at         timestamptz,
    resolve_notified_at timestamptz
);
-- no duplicate open alert per rule + blocked backend
CREATE UNIQUE INDEX IF NOT EXISTS alert_log_one_open
    ON monitor.alert_log(rule_name, blocked_pid) WHERE resolved_at IS NULL;

-- ---- sanitized reporting views for monitor_reader ----
CREATE OR REPLACE VIEW monitor.v_snapshot AS
    SELECT snapshot_id, captured_at FROM monitor.snapshot;

CREATE OR REPLACE VIEW monitor.v_session_history AS
    SELECT s.snapshot_id, n.captured_at, s.pid, s.usename, s.application_name,
           s.state, s.wait_event_type, s.wait_event, s.blocked_by,
           s.xact_start, s.state_change
    FROM monitor.session_sample s JOIN monitor.snapshot n USING (snapshot_id);

CREATE OR REPLACE VIEW monitor.v_lock_history AS
    SELECT l.snapshot_id, n.captured_at, l.blocked_pid, l.blocker_pid,
           l.blocked_user, l.blocker_user, l.blocked_app, l.blocker_app,
           l.blocker_state, l.wait_started, l.waited_seconds
    FROM monitor.lock_wait l JOIN monitor.snapshot n USING (snapshot_id);

CREATE OR REPLACE VIEW monitor.v_current_blockers AS
    SELECT * FROM monitor.v_lock_history
    WHERE snapshot_id = (SELECT max(snapshot_id) FROM monitor.snapshot);

CREATE OR REPLACE VIEW monitor.v_db_stats AS
    SELECT d.snapshot_id, n.captured_at, d.datname, d.numbackends, d.deadlocks,
           d.xact_commit_delta, d.xact_rollback_delta, d.blks_read_delta, d.blks_hit_delta
    FROM monitor.db_stat_sample d JOIN monitor.snapshot n USING (snapshot_id);

CREATE OR REPLACE VIEW monitor.v_alerts AS
    SELECT alert_id, rule_name, severity, blocked_pid, blocker_pid, first_seen_at,
           eligible_at, fired_at, observed_seconds, resolved_at,
           CASE WHEN resolved_at IS NULL THEN 'open' ELSE 'resolved' END AS status,
           notified_at, resolve_notified_at
    FROM monitor.alert_log;

RESET ROLE;

GRANT USAGE ON SCHEMA monitor TO monitor_reader;
GRANT SELECT ON monitor.v_snapshot, monitor.v_session_history, monitor.v_lock_history,
               monitor.v_current_blockers, monitor.v_db_stats, monitor.v_alerts
      TO monitor_reader;
