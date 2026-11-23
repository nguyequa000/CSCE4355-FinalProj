-- 13_purge.sql  (run as superuser, after sql/12_capture_snapshot.sql)
-- Owner: M3 (Dustin Dowell)
--
-- Retention for the monitoring history itself (proposal: default 7 days of raw
-- snapshots). Deleting a monitor.snapshot row removes all of its detail rows
-- through ON DELETE CASCADE (session_sample, lock_wait, db_stat_sample,
-- statement_history, table_stats_history, index_stats_history).
--
-- Two kinds of old snapshot are KEPT:
--   * any snapshot referenced by monitor.alert_log (fired / last seen /
--     resolved) -- that is the supporting evidence of the alert, and the
--     foreign keys would refuse the delete anyway;
--   * the newest snapshot, which is the baseline for the next delta.
--
--   SELECT monitor.purge_old_snapshots();                    -- 7 days
--   SELECT monitor.purge_old_snapshots(interval '3 days');
--
-- Scheduled daily by sql/90_cron_jobs.sql. Space is reclaimed by autovacuum.
SET ROLE monitor_collector;

CREATE OR REPLACE FUNCTION monitor.purge_old_snapshots(
    p_retention interval DEFAULT interval '7 days')
RETURNS integer
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_cutoff  timestamptz;
    v_deleted integer;
BEGIN
    -- guard against an accidental "purge everything" (NULL, 0, negative, '7' seconds)
    IF p_retention IS NULL OR p_retention < interval '1 hour' THEN
        RAISE EXCEPTION 'retention must be at least 1 hour, got %', p_retention;
    END IF;

    v_cutoff := clock_timestamp() - p_retention;

    DELETE FROM monitor.snapshot s
     WHERE s.captured_at < v_cutoff
       AND s.snapshot_id < (SELECT max(s2.snapshot_id) FROM monitor.snapshot s2)
       AND NOT EXISTS (
             SELECT 1 FROM monitor.alert_log a
              WHERE s.snapshot_id IN (a.fired_snapshot_id,
                                      a.last_seen_snapshot_id,
                                      a.resolved_snapshot_id));
    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    RAISE NOTICE 'purge_old_snapshots: deleted % snapshot(s) captured before %',
                 v_deleted, v_cutoff;
    RETURN v_deleted;
END;
$fn$;

RESET ROLE;

REVOKE ALL ON FUNCTION monitor.purge_old_snapshots(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION monitor.purge_old_snapshots(interval) TO monitor_collector;
