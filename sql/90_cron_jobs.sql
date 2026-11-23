-- 90_cron_jobs.sql  (run as superuser, LAST, connected to the project database)
-- Owner: M3 (Dustin Dowell)
--
-- pg_cron schedule from the proposal. Jobs run as background workers inside
-- the server (cron.use_background_workers = on in docker/postgres/postgresql.conf)
-- under the least-privileged role that can do the work:
--
--   job                        schedule        role               command
--   -------------------------  --------------  -----------------  -----------------------------------------
--   monitor_capture_snapshot   every minute    monitor_collector  monitor.capture_snapshot()  (+ alert rules)
--   monitor_purge_snapshots    03:17 daily     monitor_collector  monitor.purge_old_snapshots('7 days')
--   monitor_refresh_rollup     :02 every hour  monitor_collector  REFRESH monitor.mv_hourly_rollup, if it exists
--   cron_history_cleanup       03:29 daily     superuser          trim cron.job_run_details to 7 days
--
-- Safe everywhere: it does nothing (with a NOTICE) unless pg_cron is preloaded
-- AND this is the database named by cron.database_name. The Windows local
-- cluster has no pg_cron, so there scripts/collect.py --interval 60 is the
-- scheduler (proposal fallback). Re-running updates the jobs in place
-- (pg_cron keys jobs on name + user).
--
-- Inspect:   SELECT jobid, jobname, schedule, username, active FROM cron.job;
--            SELECT * FROM cron.job_run_details ORDER BY start_time DESC LIMIT 10;
-- Pause:     SELECT cron.alter_job(jobid, active := false) FROM cron.job
--             WHERE jobname = 'monitor_capture_snapshot';
DO $cron$
DECLARE
    v_cron_db text := current_setting('cron.database_name', true);
BEGIN
    IF current_setting('shared_preload_libraries') !~ '\mpg_cron\M' THEN
        RAISE NOTICE 'pg_cron is not preloaded on this server; no jobs scheduled '
                     '(run scripts/collect.py --interval 60 instead)';
        RETURN;
    END IF;

    IF v_cron_db IS DISTINCT FROM current_database() THEN
        RAISE NOTICE 'cron.database_name is %, not %; no jobs scheduled here',
                     v_cron_db, current_database();
        RETURN;
    END IF;

    CREATE EXTENSION IF NOT EXISTS pg_cron;

    PERFORM cron.schedule_in_database(
        'monitor_capture_snapshot', '* * * * *',
        'SELECT * FROM monitor.capture_snapshot()',
        current_database(), 'monitor_collector');

    PERFORM cron.schedule_in_database(
        'monitor_purge_snapshots', '17 3 * * *',
        $cmd$SELECT monitor.purge_old_snapshots(interval '7 days')$cmd$,
        current_database(), 'monitor_collector');

    -- mv_hourly_rollup belongs to the analysis layer (M5); refresh it only once
    -- it exists, so this job can be scheduled before that file is written
    PERFORM cron.schedule_in_database(
        'monitor_refresh_rollup', '2 * * * *',
        $cmd$DO $$ BEGIN
               IF to_regclass('monitor.mv_hourly_rollup') IS NOT NULL THEN
                   REFRESH MATERIALIZED VIEW monitor.mv_hourly_rollup;
               END IF;
             END $$ $cmd$,
        current_database(), 'monitor_collector');

    -- pg_cron's own run log grows by ~1,500 rows/day; keep 7 days like the snapshots
    PERFORM cron.schedule_in_database(
        'cron_history_cleanup', '29 3 * * *',
        $cmd$DELETE FROM cron.job_run_details WHERE end_time < now() - interval '7 days'$cmd$,
        current_database());

    RAISE NOTICE 'pg_cron jobs scheduled in database %', current_database();
END
$cron$;
