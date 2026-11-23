-- 20_alerts.sql  (run as superuser, connected to the project database)
-- Blocked-session alert rule. Called by the collector once per snapshot, in the
-- same transaction that inserted the snapshot, after monitor.lock_wait is filled:
--
--     SELECT * FROM monitor.evaluate_alerts(<snapshot_id>);
--
-- The function is owned by monitor_collector (created under SET ROLE below) so
-- the collector can execute it and write alert_log. monitor_reader only sees
-- monitor.v_alerts; PUBLIC cannot execute it.
SET ROLE monitor_collector;

CREATE OR REPLACE FUNCTION monitor.evaluate_alerts(p_snapshot_id bigint)
RETURNS TABLE (opened integer, resolved integer)
LANGUAGE plpgsql
AS $fn$
-- Open one alert per distinct blocked backend whose waited_seconds at this
-- snapshot has reached the blocked_session threshold, keep the existing open
-- alert updated while the wait persists, and resolve open alerts for blocked
-- PIDs that no longer appear in monitor.lock_wait for this snapshot.
--
-- Idempotent per snapshot: re-running for the same (or an older) snapshot does
-- not create a second open alert, does not move last_seen_snapshot_id
-- backwards, and does not resolve alerts last observed in a newer snapshot.
--
-- PID-reuse guard (simple): if an open alert's blocked_pid reappears with a
-- wait_started later than the snapshot in which it was last observed, the old
-- alert is resolved at this snapshot's captured_at and a new alert is opened
-- for the new wait episode. Limitation: a PID can still be reused *within* one
-- sampling interval, or a new wait can start before the previous observation
-- time in a way this heuristic cannot distinguish from the same wait. The
-- stored first_seen_at/fired_at/observed_seconds are sample-bounded, never
-- exact lock-wait durations.
DECLARE
    v_captured_at   timestamptz;
    v_threshold     integer;
    v_severity      text;
    v_enabled       boolean;
    v_opened        integer := 0;
    v_resolved      integer := 0;
    v_rowcount      integer;
    v_alert_id      bigint;
    v_new_id        bigint;
    v_prev_captured timestamptz;
    v_extra_opened  integer := 0;
    v_extra_resolved integer := 0;
    r               RECORD;
BEGIN
    SELECT s.captured_at INTO v_captured_at
      FROM monitor.snapshot s
     WHERE s.snapshot_id = p_snapshot_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'monitor.snapshot % does not exist', p_snapshot_id;
    END IF;

    SELECT ar.threshold_seconds, ar.severity, ar.enabled
      INTO v_threshold, v_severity, v_enabled
      FROM monitor.alert_rule ar
     WHERE ar.rule_name = 'blocked_session';

    IF NOT FOUND OR NOT v_enabled THEN
        -- Disabled (or absent) blocked_session rule: leave its history
        -- untouched by making every candidate comparison NULL below. The extra
        -- rules (sql/21_extra_rules.sql) are still evaluated at the end.
        v_threshold := NULL;
    END IF;

    -- One candidate per distinct blocked PID. waited_seconds is computed by the
    -- collector from captured_at - wait_started; fall back to that derivation
    -- if wait_started is NULL. blocker_pid is the lowest blocker seen at or
    -- above the threshold; first_seen_at is the earliest qualifying wait.
    FOR r IN
        SELECT lw.blocked_pid,
               max(lw.waited_seconds) AS observed_seconds,
               min(COALESCE(lw.wait_started,
                            v_captured_at
                            - make_interval(secs => lw.waited_seconds::double precision)))
                   AS first_seen_at,
               min(lw.blocker_pid) AS blocker_pid
          FROM monitor.lock_wait lw
         WHERE lw.snapshot_id = p_snapshot_id
           AND lw.waited_seconds IS NOT NULL
           AND lw.waited_seconds >= v_threshold
         GROUP BY lw.blocked_pid
         ORDER BY lw.blocked_pid
    LOOP
        v_alert_id := NULL;
        v_prev_captured := NULL;

        SELECT a.alert_id, ps.captured_at
          INTO v_alert_id, v_prev_captured
          FROM monitor.alert_log a
          JOIN monitor.snapshot ps ON ps.snapshot_id = a.last_seen_snapshot_id
         WHERE a.rule_name = 'blocked_session'
           AND a.blocked_pid = r.blocked_pid
           AND a.resolved_at IS NULL
         FOR UPDATE;

        -- PID reuse / new wait episode: a wait that began after the previous
        -- sample cannot be the wait that alert described.
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
            VALUES ('blocked_session', v_severity, r.blocked_pid, r.blocker_pid,
                    r.first_seen_at,
                    r.first_seen_at + make_interval(secs => v_threshold),
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

    -- Recovery: an open alert whose blocked PID has no lock-wait row in this
    -- snapshot is resolved at this snapshot's captured_at. Rows observed in a
    -- newer snapshot are left alone (replay safety).
    UPDATE monitor.alert_log a
       SET resolved_at = v_captured_at,
           resolved_snapshot_id = p_snapshot_id
     WHERE a.rule_name = 'blocked_session'
       AND a.resolved_at IS NULL
       AND v_threshold IS NOT NULL
       AND a.last_seen_snapshot_id <= p_snapshot_id
       AND NOT EXISTS (
             SELECT 1
               FROM monitor.lock_wait lw
              WHERE lw.snapshot_id = p_snapshot_id
                AND lw.blocked_pid = a.blocked_pid);
    GET DIAGNOSTICS v_rowcount = ROW_COUNT;
    v_resolved := v_resolved + v_rowcount;

    -- Extra sample-based rules (idle_in_transaction, long_active_query) live in
    -- sql/21_extra_rules.sql. Guarded so a database built before that file was
    -- added still evaluates the blocked_session rule normally.
    IF to_regprocedure('monitor.evaluate_extra_rules(bigint)') IS NOT NULL THEN
        SELECT COALESCE(sum(x.opened), 0), COALESCE(sum(x.resolved), 0)
          INTO v_extra_opened, v_extra_resolved
          FROM monitor.evaluate_extra_rules(p_snapshot_id) x;
        v_opened := v_opened + v_extra_opened;
        v_resolved := v_resolved + v_extra_resolved;
    END IF;

    RETURN QUERY SELECT v_opened, v_resolved;
END;
$fn$;

RESET ROLE;

REVOKE ALL ON FUNCTION monitor.evaluate_alerts(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION monitor.evaluate_alerts(bigint) TO monitor_collector;
