-- 22_more_rules.sql  (run as superuser, after sql/20_alerts.sql)
--
-- Three more sample-based alert rules, all severity warning:
--
--   connection_saturation  backends connected to the monitored database
--                          reached threshold_value x max_connections:
--                              sum(db_stat_sample.numbackends) at this snapshot
--                              / current_setting('max_connections')
--                              >= threshold_value          (default 0.80)
--                          This is per monitored database: pg_stat_database
--                          .numbackends counts only backends connected to that
--                          database, and the collector samples one row for
--                          current_database().  blocked_pid = blocker_pid = 0.
--
--   cache_hit_ratio        buffer cache hit ratio over the five minutes ending
--                          at this snapshot:
--                              sum(blks_hit_delta) /
--                              nullif(sum(blks_hit_delta + blks_read_delta), 0)
--                              < threshold_value           (default 0.90)
--                          Requires >= 100 blocks in the window; with fewer
--                          blocks the ratio is unknown, so no alert is opened
--                          *and no open alert is resolved* on that snapshot.
--                          Rows with a NULL delta (first sample / stats reset)
--                          are excluded.  blocked_pid = blocker_pid = 0.
--
--   dead_tuple_risk        per table at this snapshot:
--                              n_dead_tup >= 1000
--                              AND n_dead_tup / nullif(n_live_tup + n_dead_tup, 0)
--                                  >= threshold_value  (default 0.20)
--                          blocked_pid = relid::int (the table's OID; OIDs are
--                          small enough for int4 in practice),
--                          blocker_pid = 0.
--
-- Each rule opens at most one alert per key (the partial unique index
-- alert_log_one_open on (rule_name, blocked_pid) WHERE resolved_at IS NULL).
-- On open, first_seen_at = eligible_at = fired_at = captured_at: these are
-- instantaneous threshold checks, not sustained-duration rules, so the alert is
-- eligible the moment the snapshot that violates the threshold is captured.
-- While the condition stays true, observed_seconds grows from first_seen_at and
-- last_seen_snapshot_id advances (never backwards on replay).  The alert is
-- resolved on the first snapshot where the condition is false.
--
-- Schema note (the only alert_rule change): these rules compare ratios, so
-- alert_rule gains one nullable column, threshold_value numeric.  The existing
-- threshold_seconds column is NOT NULL and is not meaningful for these rules;
-- their rows store 0 there and the real threshold in threshold_value.
--
-- monitor.table_stat_sample is normally created by sql/12_history.sql.  The
-- CREATE TABLE IF NOT EXISTS below repeats the same contract so this file also
-- works on a database built without sql/12_history.sql.
SET ROLE monitor_collector;

CREATE TABLE IF NOT EXISTS monitor.table_stat_sample (
    snapshot_id      bigint NOT NULL REFERENCES monitor.snapshot ON DELETE CASCADE,
    relid            oid    NOT NULL,
    schemaname       text,
    relname          text,
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

ALTER TABLE monitor.alert_rule ADD COLUMN IF NOT EXISTS threshold_value numeric;

INSERT INTO monitor.alert_rule
       (rule_name, threshold_seconds, severity, enabled, description, threshold_value)
VALUES
  ('connection_saturation', 0, 'warning', true,
   'Backends connected to the monitored database reached >= threshold_value x max_connections at sample time',
   0.80),
  ('cache_hit_ratio', 0, 'warning', true,
   'Buffer cache hit ratio over the last 5 minutes fell below threshold_value (>= 100 blocks in the window required)',
   0.90),
  ('dead_tuple_risk', 0, 'warning', true,
   'A table has >= 1000 dead tuples and dead/(live+dead) >= threshold_value at sample time',
   0.20)
ON CONFLICT (rule_name) DO NOTHING;

CREATE OR REPLACE FUNCTION monitor.evaluate_more_rules(p_snapshot_id bigint)
RETURNS TABLE (opened integer, resolved integer)
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_captured_at      timestamptz;
    v_opened           integer := 0;
    v_resolved         integer := 0;
    v_rowcount         integer;
    v_alert_id         bigint;
    v_new_id           bigint;
    -- connection_saturation
    v_conn_ratio       numeric;
    v_conn_enabled     boolean;
    v_conn_threshold   numeric;
    v_conn_configured  boolean;
    -- cache_hit_ratio
    v_cache_hit        bigint;
    v_cache_blocks     bigint;
    v_cache_ratio      numeric;
    v_cache_enabled    boolean;
    v_cache_threshold  numeric;
    v_cache_configured boolean;
    -- dead_tuple_risk
    v_dead_enabled     boolean;
    v_dead_threshold   numeric;
    r                  RECORD;
BEGIN
    SELECT s.captured_at INTO v_captured_at
      FROM monitor.snapshot s
     WHERE s.snapshot_id = p_snapshot_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'monitor.snapshot % does not exist', p_snapshot_id;
    END IF;

    -- ---- connection_saturation: current snapshot only ----
    SELECT sum(d.numbackends)::numeric
           / nullif(current_setting('max_connections')::integer, 0)
      INTO v_conn_ratio
      FROM monitor.db_stat_sample d
     WHERE d.snapshot_id = p_snapshot_id;

    SELECT ar.enabled, ar.threshold_value
      INTO v_conn_enabled, v_conn_threshold
      FROM monitor.alert_rule ar
     WHERE ar.rule_name = 'connection_saturation';
    v_conn_configured := COALESCE(v_conn_enabled, false)
                         AND v_conn_threshold IS NOT NULL;

    -- ---- cache_hit_ratio: five-minute window ending at this snapshot ----
    -- snapshot_id <= p keeps a replay of an older snapshot from seeing newer
    -- samples; NULL deltas and rows missing either delta are excluded.
    SELECT sum(d.blks_hit_delta), sum(d.blks_hit_delta + d.blks_read_delta)
      INTO v_cache_hit, v_cache_blocks
      FROM monitor.db_stat_sample d
      JOIN monitor.snapshot s ON s.snapshot_id = d.snapshot_id
     WHERE d.snapshot_id <= p_snapshot_id
       AND s.captured_at <= v_captured_at
       AND s.captured_at > v_captured_at - interval '5 minutes'
       AND d.blks_hit_delta IS NOT NULL
       AND d.blks_read_delta IS NOT NULL;
    v_cache_ratio := v_cache_hit::numeric / nullif(v_cache_blocks, 0);

    SELECT ar.enabled, ar.threshold_value
      INTO v_cache_enabled, v_cache_threshold
      FROM monitor.alert_rule ar
     WHERE ar.rule_name = 'cache_hit_ratio';
    v_cache_configured := COALESCE(v_cache_enabled, false)
                          AND v_cache_threshold IS NOT NULL;

    SELECT ar.enabled, ar.threshold_value
      INTO v_dead_enabled, v_dead_threshold
      FROM monitor.alert_rule ar
     WHERE ar.rule_name = 'dead_tuple_risk';

    -- One candidate row per (rule, key) that violates its threshold at this
    -- snapshot.  A disabled rule, a NULL threshold, or an unknown value
    -- (connection count unavailable, fewer than 100 blocks in the cache
    -- window) contributes no candidate.
    FOR r IN
        WITH cand AS (
            SELECT 'connection_saturation'::text AS rule_name,
                   0::integer AS pid,
                   ar.severity
              FROM monitor.alert_rule ar
             WHERE ar.rule_name = 'connection_saturation'
               AND ar.enabled
               AND ar.threshold_value IS NOT NULL
               AND v_conn_ratio >= ar.threshold_value
            UNION ALL
            SELECT 'cache_hit_ratio'::text, 0, ar.severity
              FROM monitor.alert_rule ar
             WHERE ar.rule_name = 'cache_hit_ratio'
               AND ar.enabled
               AND ar.threshold_value IS NOT NULL
               AND v_cache_blocks >= 100
               AND v_cache_ratio < ar.threshold_value
            UNION ALL
            SELECT 'dead_tuple_risk'::text, t.relid::integer, ar.severity
              FROM monitor.table_stat_sample t
              JOIN monitor.alert_rule ar
                ON ar.rule_name = 'dead_tuple_risk'
               AND ar.enabled
               AND ar.threshold_value IS NOT NULL
             WHERE t.snapshot_id = p_snapshot_id
               AND t.n_dead_tup >= 1000
               AND t.n_dead_tup::numeric
                   / nullif(t.n_live_tup + t.n_dead_tup, 0) >= ar.threshold_value
        )
        SELECT c.rule_name, c.pid, c.severity
          FROM cand c
         ORDER BY c.rule_name, c.pid
    LOOP
        v_alert_id := NULL;

        SELECT a.alert_id INTO v_alert_id
          FROM monitor.alert_log a
         WHERE a.rule_name = r.rule_name
           AND a.blocked_pid = r.pid
           AND a.resolved_at IS NULL
         FOR UPDATE;

        IF v_alert_id IS NOT NULL THEN
            UPDATE monitor.alert_log a
               SET observed_seconds =
                       round(extract(epoch FROM (v_captured_at - a.first_seen_at))
                             ::numeric, 1),
                   last_seen_snapshot_id = p_snapshot_id
             WHERE a.alert_id = v_alert_id
               AND a.last_seen_snapshot_id <= p_snapshot_id;
        ELSE
            INSERT INTO monitor.alert_log
                   (rule_name, severity, blocked_pid, blocker_pid,
                    first_seen_at, eligible_at, fired_at, fired_snapshot_id,
                    observed_seconds, last_seen_snapshot_id)
            VALUES (r.rule_name, r.severity, r.pid, 0,
                    v_captured_at, v_captured_at, v_captured_at, p_snapshot_id,
                    0, p_snapshot_id)
            ON CONFLICT (rule_name, blocked_pid) WHERE resolved_at IS NULL
            DO NOTHING
            RETURNING alert_id INTO v_new_id;

            IF v_new_id IS NOT NULL THEN
                v_opened := v_opened + 1;
            END IF;
        END IF;
    END LOOP;

    -- ---- recovery: first snapshot where each condition is false ----
    -- Unknown values never resolve: a replay or a collection gap must not
    -- close an alert that the current sample cannot judge.
    IF v_conn_configured AND v_conn_ratio IS NOT NULL
       AND v_conn_ratio < v_conn_threshold THEN
        UPDATE monitor.alert_log a
           SET resolved_at = v_captured_at,
               resolved_snapshot_id = p_snapshot_id
         WHERE a.rule_name = 'connection_saturation'
           AND a.resolved_at IS NULL
           AND a.last_seen_snapshot_id <= p_snapshot_id;
        GET DIAGNOSTICS v_rowcount = ROW_COUNT;
        v_resolved := v_resolved + v_rowcount;
    END IF;

    IF v_cache_configured AND v_cache_blocks >= 100
       AND v_cache_ratio >= v_cache_threshold THEN
        UPDATE monitor.alert_log a
           SET resolved_at = v_captured_at,
               resolved_snapshot_id = p_snapshot_id
         WHERE a.rule_name = 'cache_hit_ratio'
           AND a.resolved_at IS NULL
           AND a.last_seen_snapshot_id <= p_snapshot_id;
        GET DIAGNOSTICS v_rowcount = ROW_COUNT;
        v_resolved := v_resolved + v_rowcount;
    END IF;

    -- An empty table_stat_sample for this snapshot is unknown (no table
    -- collection), not "every table is clean"; it never resolves open alerts.
    IF COALESCE(v_dead_enabled, false) AND v_dead_threshold IS NOT NULL
       AND EXISTS (SELECT 1 FROM monitor.table_stat_sample
                    WHERE snapshot_id = p_snapshot_id) THEN
        UPDATE monitor.alert_log a
           SET resolved_at = v_captured_at,
               resolved_snapshot_id = p_snapshot_id
         WHERE a.rule_name = 'dead_tuple_risk'
           AND a.resolved_at IS NULL
           AND a.last_seen_snapshot_id <= p_snapshot_id
           AND NOT EXISTS (
                 SELECT 1
                   FROM monitor.table_stat_sample t
                    JOIN monitor.alert_rule ar
                      ON ar.rule_name = 'dead_tuple_risk'
                     AND ar.enabled
                     AND ar.threshold_value IS NOT NULL
                  WHERE t.snapshot_id = p_snapshot_id
                    AND t.relid::integer = a.blocked_pid
                    AND t.n_dead_tup >= 1000
                    AND t.n_dead_tup::numeric
                        / nullif(t.n_live_tup + t.n_dead_tup, 0)
                        >= ar.threshold_value);
        GET DIAGNOSTICS v_rowcount = ROW_COUNT;
        v_resolved := v_resolved + v_rowcount;
    END IF;

    RETURN QUERY SELECT v_opened, v_resolved;
END;
$fn$;

RESET ROLE;

REVOKE ALL ON FUNCTION monitor.evaluate_more_rules(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION monitor.evaluate_more_rules(bigint) TO monitor_collector;
