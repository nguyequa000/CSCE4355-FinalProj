-- 14_history.sql  (run as superuser, connected to the project database, after
-- sql/10_monitor.sql; the project setup script applies it before sql/20_alerts.sql)
--
-- Enriched monitoring history on top of the T2 snapshot tables:
--
--   monitor.table_stat_sample    per-table counters, index usage and tuple churn
--                                from pg_stat_user_tables (seq_scan/idx_scan)
--   monitor.statement_sample     pg_stat_statements counters, NO query text
--   monitor.v_table_stats        reader view: table growth / dead tuples / scans
--   monitor.v_statement_stats    reader view: per-statement deltas
--   monitor.v_hourly_rollup      reader view: one row per hour of history
--   monitor.collect_extra()      fills the two history tables for one snapshot
--   monitor.purge_history()      retention; never deletes alert-referenced snapshots
--
-- Ownership follows sql/10_monitor.sql: the tables, the function and the views
-- belong to monitor_collector; monitor_reader gets SELECT on the v_* views only.
--
-- Privacy: statement history stores identifiers and counters only
-- (queryid/userid/db time), never query text — statements can contain literals
-- and personal data.  sql/01_shop.sql installs pg_stat_statements; the
-- statement part of collect_extra() is guarded so a database without the
-- extension still collects table history.

SET ROLE monitor_collector;

-- ---------------------------------------------------------------------------
-- Table statistics history: one row per user table per snapshot.
-- ---------------------------------------------------------------------------
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

-- history lookups are "this table over time" as often as "this snapshot"
CREATE INDEX IF NOT EXISTS table_stat_sample_relname_idx
    ON monitor.table_stat_sample (schemaname, relname, snapshot_id DESC);

-- ---------------------------------------------------------------------------
-- Statement statistics history: one row per (queryid, userid) per snapshot.
-- *_delta is NULL when there is no comparable previous sample of the same
-- (queryid, userid) or when the counter went backwards (pg_stat_statements
-- reset); a delta is never negative.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitor.statement_sample (
    snapshot_id         bigint NOT NULL REFERENCES monitor.snapshot ON DELETE CASCADE,
    queryid             bigint NOT NULL,
    userid              oid    NOT NULL,
    calls               bigint,
    total_exec_time     double precision,
    mean_exec_time      double precision,
    rows                bigint,
    shared_blks_hit     bigint,
    shared_blks_read    bigint,
    calls_delta         bigint,
    total_exec_time_delta double precision,
    PRIMARY KEY (snapshot_id, queryid, userid)
);

-- supports the "previous sample of this statement" lookup in collect_extra()
CREATE INDEX IF NOT EXISTS statement_sample_query_prev_idx
    ON monitor.statement_sample (queryid, userid, snapshot_id DESC);

-- ---------------------------------------------------------------------------
-- monitor.collect_extra(snapshot_id)
-- Called by scripts/collect.py inside the snapshot transaction, after
-- db_stat_sample and before the alert rules.  Idempotent per snapshot
-- (ON CONFLICT DO NOTHING), so replaying a snapshot never duplicates rows.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION monitor.collect_extra(p_snapshot_id bigint)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_pgss_schema text;
    v_dbid        oid;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM monitor.snapshot s
                    WHERE s.snapshot_id = p_snapshot_id) THEN
        RAISE EXCEPTION 'monitor.snapshot % does not exist', p_snapshot_id;
    END IF;

    -- ---- per-table counters.  pg_stat_user_tables already excludes system
    -- catalogs; the monitor schema's own tables are excluded too, so the
    -- history never measures its own bookkeeping.
    INSERT INTO monitor.table_stat_sample
        (snapshot_id, relid, schemaname, relname, n_live_tup, n_dead_tup,
         seq_scan, idx_scan, n_tup_ins, n_tup_upd, n_tup_del,
         last_autovacuum, last_autoanalyze)
    SELECT p_snapshot_id, t.relid, t.schemaname, t.relname,
           t.n_live_tup, t.n_dead_tup, t.seq_scan, t.idx_scan,
           t.n_tup_ins, t.n_tup_upd, t.n_tup_del,
           t.last_autovacuum, t.last_autoanalyze
      FROM pg_stat_user_tables t
     WHERE t.schemaname <> 'monitor'
    ON CONFLICT (snapshot_id, relid) DO NOTHING;

    -- ---- statement counters, only for the database being monitored and only
    -- top-level statements (nested rows would collide on queryid/userid).
    -- Dynamic SQL because the extension may be absent (or in another schema)
    -- and a missing pg_stat_statements must not fail the snapshot.
    SELECT n.nspname
      INTO v_pgss_schema
      FROM pg_extension e
      JOIN pg_namespace n ON n.oid = e.extnamespace
     WHERE e.extname = 'pg_stat_statements';

    IF v_pgss_schema IS NOT NULL
       AND to_regclass(format('%I.pg_stat_statements', v_pgss_schema)) IS NOT NULL
    THEN
        SELECT d.oid INTO v_dbid
          FROM pg_database d
         WHERE d.datname = current_database();

        EXECUTE format($sql$
            WITH cur AS (
                SELECT s.queryid, s.userid, s.calls, s.total_exec_time,
                       s.mean_exec_time, s.rows,
                       s.shared_blks_hit, s.shared_blks_read
                  FROM %I.pg_stat_statements s
                 WHERE s.dbid = $1
                   AND s.toplevel
                   AND s.queryid IS NOT NULL
            ), prev AS (
                SELECT DISTINCT ON (p.queryid, p.userid)
                       p.queryid, p.userid, p.calls, p.total_exec_time
                  FROM monitor.statement_sample p
                 WHERE p.snapshot_id < $2
                 ORDER BY p.queryid, p.userid, p.snapshot_id DESC
            )
            INSERT INTO monitor.statement_sample
                (snapshot_id, queryid, userid, calls, total_exec_time,
                 mean_exec_time, rows, shared_blks_hit, shared_blks_read,
                 calls_delta, total_exec_time_delta)
            SELECT $2, c.queryid, c.userid, c.calls, c.total_exec_time,
                   c.mean_exec_time, c.rows, c.shared_blks_hit, c.shared_blks_read,
                   CASE WHEN p.calls IS NULL OR c.calls < p.calls
                        THEN NULL ELSE c.calls - p.calls END,
                   CASE WHEN p.total_exec_time IS NULL
                         OR c.total_exec_time < p.total_exec_time
                        THEN NULL ELSE c.total_exec_time - p.total_exec_time END
              FROM cur c
              LEFT JOIN prev p
                     ON p.queryid = c.queryid AND p.userid = c.userid
            ON CONFLICT (snapshot_id, queryid, userid) DO NOTHING
        $sql$, v_pgss_schema) USING v_dbid, p_snapshot_id;
    ELSE
        RAISE NOTICE 'monitor.collect_extra: pg_stat_statements missing in %; '
                     'statement history skipped', current_database();
    END IF;
END;
$fn$;

-- ---------------------------------------------------------------------------
-- Reader views
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW monitor.v_table_stats AS
    SELECT t.snapshot_id,
           n.captured_at,
           t.schemaname,
           t.relname,
           t.n_live_tup,
           t.n_dead_tup,
           round(t.n_dead_tup::numeric
                 / nullif(t.n_live_tup + t.n_dead_tup, 0), 4) AS dead_ratio,
           t.seq_scan,
           t.idx_scan
      FROM monitor.table_stat_sample t
      JOIN monitor.snapshot n USING (snapshot_id);

CREATE OR REPLACE VIEW monitor.v_statement_stats AS
    SELECT t.snapshot_id,
           n.captured_at,
           t.queryid,
           t.calls_delta,
           t.total_exec_time_delta,
           t.mean_exec_time
      FROM monitor.statement_sample t
      JOIN monitor.snapshot n USING (snapshot_id);

-- alert_log lives in sql/20_alerts.sql, which the setup script applies AFTER
-- this file.  A view cannot reference a relation that does not exist yet, so
-- the rollup reads alert_log through this narrow SECURITY DEFINER helper: it
-- reports 0 until the table exists and real counts afterwards.  That keeps one
-- view definition (and one reader grant) valid in any installation order.
CREATE OR REPLACE FUNCTION monitor.alerts_fired_in_hour(p_hour timestamptz)
RETURNS integer
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
    v_count integer := 0;
BEGIN
    IF to_regclass('monitor.alert_log') IS NULL THEN
        RETURN 0;
    END IF;
    EXECUTE
        'SELECT count(*)::integer
           FROM monitor.alert_log a
          WHERE a.fired_at >= $1
            AND a.fired_at < $1 + interval ''1 hour'''
      INTO v_count
      USING p_hour;
    RETURN v_count;
END;
$fn$;

-- One row per clock hour of history.  A snapshot with no known db deltas
-- (first sample, or a stats reset) contributes NULL, which SUM ignores; the
-- hit ratio is sum(hit) / nullif(sum(hit + read), 0) over the same rows.
CREATE OR REPLACE VIEW monitor.v_hourly_rollup AS
    WITH per_snapshot AS (
        SELECT n.snapshot_id,
               date_trunc('hour', n.captured_at) AS hour,
               (SELECT count(*)::integer FROM monitor.lock_wait w
                 WHERE w.snapshot_id = n.snapshot_id) AS lock_edges,
               (SELECT count(*)::integer FROM monitor.session_sample x
                 WHERE x.snapshot_id = n.snapshot_id) AS sessions,
               (SELECT sum(d.xact_commit_delta) FROM monitor.db_stat_sample d
                 WHERE d.snapshot_id = n.snapshot_id
                   AND d.datname = current_database()) AS xact_commit_delta_sum,
               (SELECT sum(d.blks_hit_delta) FROM monitor.db_stat_sample d
                 WHERE d.snapshot_id = n.snapshot_id
                   AND d.datname = current_database()) AS blks_hit_delta_sum,
               (SELECT sum(d.blks_hit_delta + d.blks_read_delta)
                  FROM monitor.db_stat_sample d
                 WHERE d.snapshot_id = n.snapshot_id
                   AND d.datname = current_database()) AS blks_total_delta_sum
          FROM monitor.snapshot n
    )
    SELECT hour,
           count(*)::integer AS snapshots,
           coalesce(max(lock_edges), 0)::integer AS max_lock_edges,
           coalesce(max(sessions), 0)::integer AS max_sessions,
           sum(xact_commit_delta_sum)::bigint AS xact_commit_delta_sum,
           round(sum(blks_hit_delta_sum)::numeric
                 / nullif(sum(blks_total_delta_sum), 0), 4) AS blks_hit_ratio,
           monitor.alerts_fired_in_hour(hour) AS alerts_fired
      FROM per_snapshot
     GROUP BY hour
     ORDER BY hour;

-- ---------------------------------------------------------------------------
-- Retention: drop snapshots older than p_keep, CASCADE to their samples, but
-- keep every snapshot an alert points at (fired / last seen / resolved).  The
-- alert_log FKs have no ON DELETE clause, so a blind delete would error; here
-- such snapshots are silently skipped and the row counts as not deleted.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION monitor.purge_history(p_keep interval DEFAULT '7 days')
RETURNS integer
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_cutoff  timestamptz;
    v_deleted integer;
BEGIN
    IF p_keep IS NULL OR p_keep < interval '0' THEN
        RAISE EXCEPTION 'keep interval must be >= 0, got %', p_keep;
    END IF;

    v_cutoff := now() - p_keep;

    WITH doomed AS (
        DELETE FROM monitor.snapshot s
         WHERE s.captured_at < v_cutoff
           AND NOT EXISTS (
                 SELECT 1
                   FROM monitor.alert_log a
                  WHERE a.fired_snapshot_id    = s.snapshot_id
                     OR a.last_seen_snapshot_id = s.snapshot_id
                     OR a.resolved_snapshot_id  = s.snapshot_id)
        RETURNING 1
    )
    SELECT count(*)::integer INTO v_deleted FROM doomed;

    RETURN v_deleted;
END;
$fn$;

RESET ROLE;

-- ---------------------------------------------------------------------------
-- Privileges: the reader sees the reporting views only; nothing here is
-- callable by PUBLIC.
-- ---------------------------------------------------------------------------
REVOKE ALL ON monitor.table_stat_sample, monitor.statement_sample FROM PUBLIC;
REVOKE ALL ON FUNCTION monitor.collect_extra(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION monitor.purge_history(interval) FROM PUBLIC;
REVOKE ALL ON FUNCTION monitor.alerts_fired_in_hour(timestamptz) FROM PUBLIC;

GRANT SELECT ON monitor.v_table_stats, monitor.v_statement_stats,
               monitor.v_hourly_rollup
      TO monitor_reader;
GRANT EXECUTE ON FUNCTION monitor.collect_extra(bigint) TO monitor_collector;
GRANT EXECUTE ON FUNCTION monitor.purge_history(interval) TO monitor_collector;
GRANT EXECUTE ON FUNCTION monitor.alerts_fired_in_hour(timestamptz)
      TO monitor_collector, monitor_reader;
