-- 21_extra_rules.sql  (run as superuser, after sql/20_alerts.sql)
--
-- Two additional sample-based alert rules:
--
--   idle_in_transaction  state = 'idle in transaction' and
--                        captured_at - state_change >= threshold (default 60 s)
--   long_active_query    state = 'active', wait_event_type IS DISTINCT FROM 'Lock'
--                        (so a lock-blocked session is not counted twice), and
--                        captured_at - query_start >= threshold (default 60 s).
--                        The collector's own backend is excluded.
--
-- Both are evaluated from monitor.session_sample rows of the snapshot being
-- evaluated, by monitor.evaluate_extra_rules(snapshot_id). sql/20_alerts.sql's
-- monitor.evaluate_alerts() calls it when the function exists (guarded with
-- to_regprocedure), so the collector keeps its single call site.
--
-- alert_log column mapping (these rules have no waiter/blocker pair):
--   blocked_pid       the offending session PID
--   blocker_pid       the same PID repeated (column is NOT NULL; no blocker
--                     exists for "this session has been idle in a transaction"
--                     or "this session has run one statement too long")
--   first_seen_at     state_change (idle rule) / query_start (long-query rule)
--   eligible_at       first_seen_at + threshold
--   fired_at          snapshot captured_at
--   observed_seconds  captured_at - first_seen_at, rounded to 0.1 s
-- The partial unique index alert_log_one_open(rule_name, blocked_pid) therefore
-- means: at most one open alert per rule per session.
--
-- Idempotent per snapshot in the same way as the blocked_session rule: replaying
-- a snapshot does not duplicate an open alert and never moves
-- last_seen_snapshot_id backwards. A session whose first_seen_at is later than
-- the snapshot in which its open alert was last observed is treated as a new
-- episode (new transaction / new statement), so the old alert is resolved and a
-- new one may open. Resolution happens on the first snapshot where the rule's
-- condition (including its threshold) no longer holds for that PID.
SET ROLE monitor_collector;

INSERT INTO monitor.alert_rule (rule_name, threshold_seconds, severity, enabled, description)
VALUES
  ('idle_in_transaction', 60, 'warning', true,
   'A session has held an open transaction while idle for >= threshold_seconds at sample time'),
  ('long_active_query', 60, 'warning', true,
   'A session has been executing the same statement for >= threshold_seconds at sample time (lock waits and the collector excluded)')
ON CONFLICT (rule_name) DO NOTHING;

CREATE OR REPLACE FUNCTION monitor.evaluate_extra_rules(p_snapshot_id bigint)
RETURNS TABLE (opened integer, resolved integer)
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_captured_at   timestamptz;
    v_opened        integer := 0;
    v_resolved      integer := 0;
    v_rowcount      integer;
    v_alert_id      bigint;
    v_new_id        bigint;
    v_prev_captured timestamptz;
    r               RECORD;
BEGIN
    SELECT s.captured_at INTO v_captured_at
      FROM monitor.snapshot s
     WHERE s.snapshot_id = p_snapshot_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'monitor.snapshot % does not exist', p_snapshot_id;
    END IF;

    -- One candidate row per (rule, pid) that has reached its threshold at this
    -- snapshot. session_sample has one row per pid, so no grouping is needed.
    -- A disabled rule contributes no candidates.
    FOR r IN
        WITH cand AS (
            SELECT 'idle_in_transaction'::text AS rule_name,
                   s.pid,
                   s.state_change AS first_seen_at,
                   extract(epoch FROM (n.captured_at - s.state_change))
                       ::double precision AS observed_seconds
              FROM monitor.session_sample s
              JOIN monitor.snapshot n USING (snapshot_id)
             WHERE s.snapshot_id = p_snapshot_id
               AND s.state = 'idle in transaction'
               AND s.state_change IS NOT NULL
               AND s.pid <> n.collector_pid
            UNION ALL
            SELECT 'long_active_query'::text,
                   s.pid,
                   s.query_start,
                   extract(epoch FROM (n.captured_at - s.query_start))
                       ::double precision
              FROM monitor.session_sample s
              JOIN monitor.snapshot n USING (snapshot_id)
             WHERE s.snapshot_id = p_snapshot_id
               AND s.state = 'active'
               AND s.wait_event_type IS DISTINCT FROM 'Lock'
               AND s.query_start IS NOT NULL
               AND s.pid <> n.collector_pid
        )
        SELECT c.rule_name, c.pid, c.first_seen_at,
               round(c.observed_seconds::numeric, 1) AS observed_seconds,
               ar.threshold_seconds, ar.severity
          FROM cand c
          JOIN monitor.alert_rule ar USING (rule_name)
         WHERE ar.enabled
           AND c.observed_seconds >= ar.threshold_seconds
         ORDER BY c.rule_name, c.pid
    LOOP
        v_alert_id := NULL;
        v_prev_captured := NULL;

        SELECT a.alert_id, ps.captured_at
          INTO v_alert_id, v_prev_captured
          FROM monitor.alert_log a
          JOIN monitor.snapshot ps ON ps.snapshot_id = a.last_seen_snapshot_id
         WHERE a.rule_name = r.rule_name
           AND a.blocked_pid = r.pid
           AND a.resolved_at IS NULL
         FOR UPDATE;

        -- New transaction / new statement: the previous episode is over even
        -- though the PID is the same.
        IF v_alert_id IS NOT NULL AND r.first_seen_at > v_prev_captured THEN
            UPDATE monitor.alert_log
               SET resolved_at = v_captured_at,
                   resolved_snapshot_id = p_snapshot_id
             WHERE alert_id = v_alert_id;
            v_resolved := v_resolved + 1;
            v_alert_id := NULL;
        END IF;

        IF v_alert_id IS NOT NULL THEN
            UPDATE monitor.alert_log
               SET observed_seconds = r.observed_seconds,
                   last_seen_snapshot_id = p_snapshot_id
             WHERE alert_id = v_alert_id
               AND last_seen_snapshot_id <= p_snapshot_id;
        ELSE
            INSERT INTO monitor.alert_log
                   (rule_name, severity, blocked_pid, blocker_pid,
                    first_seen_at, eligible_at, fired_at, fired_snapshot_id,
                    observed_seconds, last_seen_snapshot_id)
            VALUES (r.rule_name, r.severity, r.pid, r.pid,
                    r.first_seen_at,
                    r.first_seen_at + make_interval(secs => r.threshold_seconds),
                    v_captured_at, p_snapshot_id,
                    r.observed_seconds, p_snapshot_id)
            ON CONFLICT (rule_name, blocked_pid) WHERE resolved_at IS NULL
            DO NOTHING
            RETURNING alert_id INTO v_new_id;

            IF v_new_id IS NOT NULL THEN
                v_opened := v_opened + 1;
            END IF;
        END IF;
    END LOOP;

    -- Recovery: resolve an open extra-rule alert when this snapshot no longer
    -- satisfies that rule's condition for the PID (released transaction,
    -- statement finished, or rule disabled). Rows last observed in a newer
    -- snapshot are left alone (replay safety).
    UPDATE monitor.alert_log a
       SET resolved_at = v_captured_at,
           resolved_snapshot_id = p_snapshot_id
     WHERE a.rule_name IN ('idle_in_transaction', 'long_active_query')
       AND a.resolved_at IS NULL
       AND a.last_seen_snapshot_id <= p_snapshot_id
       AND EXISTS (
             SELECT 1 FROM monitor.alert_rule ar
              WHERE ar.rule_name = a.rule_name AND ar.enabled)
       AND NOT EXISTS (
             SELECT 1
               FROM monitor.session_sample s
               JOIN monitor.snapshot n USING (snapshot_id)
               JOIN monitor.alert_rule ar
                 ON ar.rule_name = a.rule_name AND ar.enabled
              WHERE s.snapshot_id = p_snapshot_id
                AND s.pid = a.blocked_pid
                AND s.pid <> n.collector_pid
                AND (
                      (a.rule_name = 'idle_in_transaction'
                       AND s.state = 'idle in transaction'
                       AND s.state_change IS NOT NULL
                       AND n.captured_at - s.state_change
                           >= make_interval(secs => ar.threshold_seconds))
                   OR (a.rule_name = 'long_active_query'
                       AND s.state = 'active'
                       AND s.wait_event_type IS DISTINCT FROM 'Lock'
                       AND s.query_start IS NOT NULL
                       AND n.captured_at - s.query_start
                           >= make_interval(secs => ar.threshold_seconds))
                ));
    GET DIAGNOSTICS v_rowcount = ROW_COUNT;
    v_resolved := v_resolved + v_rowcount;

    RETURN QUERY SELECT v_opened, v_resolved;
END;
$fn$;

RESET ROLE;

REVOKE ALL ON FUNCTION monitor.evaluate_extra_rules(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION monitor.evaluate_extra_rules(bigint) TO monitor_collector;
