-- 12_capture_snapshot.sql  (run as superuser, after sql/11_monitor_history.sql)
-- Owner: M3 (Dustin Dowell)
--
-- monitor.capture_snapshot(): the proposal's in-database collector. One call =
-- one snapshot, in the caller's transaction:
--
--   1. monitor.snapshot              captured_at = clock_timestamp()
--   2. monitor.session_sample        pg_stat_activity, client backends of this DB
--   3. monitor.lock_wait             one row per (waiter, blocker) edge
--   4. monitor.db_stat_sample        pg_stat_database counters + deltas
--   5. monitor.statement_history     pg_stat_statements (top 500 by total time)
--   6. monitor.table_stats_history   pg_stat_user_tables / pg_statio_user_tables
--   7. monitor.index_stats_history   pg_stat_user_indexes / pg_statio_user_indexes
--   8. monitor.evaluate_alerts()     when that function exists (sql/20_alerts.sql)
--
-- Steps 2-4 use the same SQL as the Python collector's original inline queries
-- (scripts/collect.py), so the alert rules and the dashboard read identical rows.
--
-- Callers:
--   * pg_cron, every minute, as monitor_collector (sql/90_cron_jobs.sql)
--   * scripts/collect.py on hosts without pg_cron (Windows local cluster)
--   * by hand:  SELECT * FROM monitor.capture_snapshot();
--
-- Delta rules (proposal risk table: "statistics counters are cumulative and can
-- be reset"): a *_delta is NULL -- a new baseline, never a negative number --
-- when there is no previous row for the same object, the database statistics
-- (pg_stat_database.stats_reset) or pg_stat_statements were reset, or any
-- tracked counter of that row decreased.
--
-- Lock safety: pg_total_relation_size()/pg_relation_size() take an
-- AccessShareLock. A relation with an AccessExclusiveLock held OR requested
-- (ALTER TABLE, VACUUM FULL, REINDEX, ...) gets a NULL size for that snapshot
-- instead of making the collector queue behind the very lock it should report.
-- lock_timeout is also capped at 5 s for the rest of the transaction.
SET ROLE monitor_collector;

CREATE OR REPLACE FUNCTION monitor.capture_snapshot()
RETURNS TABLE (snapshot_id     bigint,
               captured_at     timestamptz,
               sessions        integer,
               lock_edges      integer,
               statements      integer,
               tables          integer,
               indexes         integer,
               alerts_opened   integer,
               alerts_resolved integer)
LANGUAGE plpgsql
AS $fn$
#variable_conflict use_column
DECLARE
    v_sid         bigint;
    v_at          timestamptz;
    v_dboid       oid := (SELECT d.oid FROM pg_database d WHERE d.datname = current_database());
    v_sessions    integer := 0;
    v_edges       integer := 0;
    v_statements  integer := 0;
    v_tables      integer := 0;
    v_indexes     integer := 0;
    v_opened      integer;
    v_resolved    integer;
    v_step_opened integer;
    v_step_resolved integer;
    v_prev        bigint;
    v_db_reset    timestamptz;
    v_same_epoch  boolean;
    v_pss_reset   timestamptz;
    v_xlocked     oid[];
BEGIN
    PERFORM set_config('lock_timeout', '5s', true);

    -- 1. snapshot header ------------------------------------------------------
    INSERT INTO monitor.snapshot AS s (captured_at)
    VALUES (clock_timestamp())
    RETURNING s.snapshot_id, s.captured_at INTO v_sid, v_at;

    -- 2. sessions (query text clipped to 60 chars, never stored whole) --------
    INSERT INTO monitor.session_sample
        (snapshot_id, pid, usename, application_name, state, wait_event_type,
         wait_event, xact_start, query_start, state_change, blocked_by, query_excerpt)
    SELECT v_sid, a.pid, a.usename, a.application_name, a.state,
           a.wait_event_type, a.wait_event, a.xact_start, a.query_start,
           a.state_change, pg_blocking_pids(a.pid), left(a.query, 60)
    FROM pg_stat_activity a
    WHERE a.backend_type = 'client backend'
      AND a.datname = current_database()
      AND a.pid <> pg_backend_pid();
    GET DIAGNOSTICS v_sessions = ROW_COUNT;

    -- 3. lock-wait edges ("who blocked whom") ------------------------------------
    INSERT INTO monitor.lock_wait
        (snapshot_id, blocked_pid, blocker_pid, blocked_user, blocker_user,
         blocked_app, blocker_app, blocker_state, wait_started, waited_seconds)
    SELECT v_sid, w.pid, bp.pid,
           w.usename, b.usename, w.application_name, b.application_name, b.state,
           w.state_change,
           round(extract(epoch FROM (v_at - w.state_change))::numeric, 1)
    FROM pg_stat_activity w
    CROSS JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS bp(pid)
    LEFT JOIN pg_stat_activity b ON b.pid = bp.pid
    WHERE w.backend_type = 'client backend'
      AND w.datname = current_database()
      AND w.pid <> pg_backend_pid();
    GET DIAGNOSTICS v_edges = ROW_COUNT;

    -- 4. database counters ----------------------------------------------------
    SELECT d.stats_reset INTO v_db_reset
      FROM pg_stat_database d WHERE d.datname = current_database();

    SELECT max(p.snapshot_id) INTO v_prev
      FROM monitor.db_stat_sample p
     WHERE p.datname = current_database() AND p.snapshot_id < v_sid;

    INSERT INTO monitor.db_stat_sample
        (snapshot_id, datname, numbackends, xact_commit, xact_rollback, blks_read,
         blks_hit, deadlocks, stats_reset, xact_commit_delta, xact_rollback_delta,
         blks_read_delta, blks_hit_delta)
    SELECT v_sid, d.datname, d.numbackends, d.xact_commit, d.xact_rollback,
           d.blks_read, d.blks_hit, d.deadlocks, d.stats_reset,
           CASE WHEN f.ok THEN d.xact_commit   - p.xact_commit   END,
           CASE WHEN f.ok THEN d.xact_rollback - p.xact_rollback END,
           CASE WHEN f.ok THEN d.blks_read     - p.blks_read     END,
           CASE WHEN f.ok THEN d.blks_hit      - p.blks_hit      END
    FROM pg_stat_database d
    LEFT JOIN monitor.db_stat_sample p
           ON p.snapshot_id = v_prev AND p.datname = d.datname
    CROSS JOIN LATERAL (
        SELECT p.snapshot_id IS NOT NULL
           AND p.stats_reset IS NOT DISTINCT FROM d.stats_reset
           AND d.xact_commit   >= p.xact_commit
           AND d.xact_rollback >= p.xact_rollback
           AND d.blks_read     >= p.blks_read
           AND d.blks_hit      >= p.blks_hit AS ok
    ) f
    WHERE d.datname = current_database();

    -- 5. statement statistics ---------------------------------------------------
    -- Skipped (statements = 0) when pg_stat_statements is not installed in this
    -- database or not preloaded; selecting the view would otherwise raise.
    IF to_regclass('public.pg_stat_statements') IS NOT NULL
       AND current_setting('shared_preload_libraries') ~ '\mpg_stat_statements\M' THEN

        IF to_regclass('public.pg_stat_statements_info') IS NOT NULL THEN
            SELECT i.stats_reset INTO v_pss_reset FROM public.pg_stat_statements_info i;
        END IF;

        SELECT max(h.snapshot_id) INTO v_prev
          FROM monitor.statement_history h WHERE h.snapshot_id < v_sid;

        INSERT INTO monitor.statement_history
            (snapshot_id, queryid, userid, toplevel, rolname, query_text,
             calls, total_exec_time, mean_exec_time, rows,
             shared_blks_hit, shared_blks_read, temp_blks_written, stats_reset,
             calls_delta, total_exec_time_delta, rows_delta,
             shared_blks_hit_delta, shared_blks_read_delta)
        SELECT v_sid, s.queryid, s.userid, s.toplevel, r.rolname, left(s.query, 500),
               s.calls, s.total_exec_time, s.mean_exec_time, s.rows,
               s.shared_blks_hit, s.shared_blks_read, s.temp_blks_written, v_pss_reset,
               CASE WHEN f.ok THEN s.calls            - p.calls            END,
               CASE WHEN f.ok THEN greatest(s.total_exec_time - p.total_exec_time, 0) END,
               CASE WHEN f.ok THEN s.rows             - p.rows             END,
               CASE WHEN f.ok THEN s.shared_blks_hit  - p.shared_blks_hit  END,
               CASE WHEN f.ok THEN s.shared_blks_read - p.shared_blks_read END
        FROM (
            SELECT pss.*
              FROM public.pg_stat_statements pss
             WHERE pss.dbid = v_dboid
               AND pss.queryid IS NOT NULL
               -- utility statements are not normalized: never keep anything
               -- that might carry a password literal
               AND pss.query !~* 'password'
             ORDER BY pss.total_exec_time DESC
             LIMIT 500
        ) s
        LEFT JOIN pg_roles r ON r.oid = s.userid
        LEFT JOIN monitor.statement_history p
               ON p.snapshot_id = v_prev
              AND p.queryid = s.queryid AND p.userid = s.userid AND p.toplevel = s.toplevel
        CROSS JOIN LATERAL (
            SELECT p.snapshot_id IS NOT NULL
               AND p.stats_reset IS NOT DISTINCT FROM v_pss_reset
               AND s.calls            >= p.calls
               AND s.rows             >= p.rows
               AND s.shared_blks_hit  >= p.shared_blks_hit
               AND s.shared_blks_read >= p.shared_blks_read AS ok
        ) f;
        GET DIAGNOSTICS v_statements = ROW_COUNT;
    END IF;

    -- relations with an AccessExclusiveLock held or queued (and, for an index,
    -- its table) -- their size is not read this snapshot
    v_xlocked := ARRAY(
        SELECT l.relation
          FROM pg_locks l
         WHERE l.locktype = 'relation' AND l.database = v_dboid
           AND l.mode = 'AccessExclusiveLock'
        UNION
        SELECT x.indrelid
          FROM pg_locks l JOIN pg_index x ON x.indexrelid = l.relation
         WHERE l.locktype = 'relation' AND l.database = v_dboid
           AND l.mode = 'AccessExclusiveLock'
        UNION
        SELECT x.indexrelid
          FROM pg_locks l JOIN pg_index x ON x.indrelid = l.relation
         WHERE l.locktype = 'relation' AND l.database = v_dboid
           AND l.mode = 'AccessExclusiveLock');

    -- 6. table statistics ---------------------------------------------------------
    SELECT max(h.snapshot_id) INTO v_prev
      FROM monitor.table_stats_history h WHERE h.snapshot_id < v_sid;
    -- same statistics epoch as the previous table snapshot? (pg_stat_reset)
    v_same_epoch := COALESCE(
        (SELECT p.stats_reset IS NOT DISTINCT FROM v_db_reset
           FROM monitor.db_stat_sample p
          WHERE p.snapshot_id = v_prev AND p.datname = current_database()),
        false);

    INSERT INTO monitor.table_stats_history
        (snapshot_id, relid, schemaname, relname, seq_scan, seq_tup_read, idx_scan,
         idx_tup_fetch, n_tup_ins, n_tup_upd, n_tup_del, n_tup_hot_upd, n_live_tup,
         n_dead_tup, n_mod_since_analyze, heap_blks_read, heap_blks_hit,
         last_vacuum, last_autovacuum, last_analyze, last_autoanalyze,
         vacuum_count, autovacuum_count, total_bytes,
         seq_scan_delta, seq_tup_read_delta, idx_scan_delta, n_tup_ins_delta,
         n_tup_upd_delta, n_tup_del_delta, heap_blks_read_delta, heap_blks_hit_delta)
    SELECT v_sid, t.relid, t.schemaname, t.relname, t.seq_scan, t.seq_tup_read, t.idx_scan,
           t.idx_tup_fetch, t.n_tup_ins, t.n_tup_upd, t.n_tup_del, t.n_tup_hot_upd, t.n_live_tup,
           t.n_dead_tup, t.n_mod_since_analyze, io.heap_blks_read, io.heap_blks_hit,
           t.last_vacuum, t.last_autovacuum, t.last_analyze, t.last_autoanalyze,
           t.vacuum_count, t.autovacuum_count,
           CASE WHEN t.relid <> ALL (v_xlocked) THEN pg_total_relation_size(t.relid) END,
           CASE WHEN f.ok THEN t.seq_scan     - p.seq_scan     END,
           CASE WHEN f.ok THEN t.seq_tup_read - p.seq_tup_read END,
           CASE WHEN f.ok THEN t.idx_scan     - p.idx_scan     END,
           CASE WHEN f.ok THEN t.n_tup_ins    - p.n_tup_ins    END,
           CASE WHEN f.ok THEN t.n_tup_upd    - p.n_tup_upd    END,
           CASE WHEN f.ok THEN t.n_tup_del    - p.n_tup_del    END,
           CASE WHEN f.ok THEN io.heap_blks_read - p.heap_blks_read END,
           CASE WHEN f.ok THEN io.heap_blks_hit  - p.heap_blks_hit  END
    FROM pg_stat_user_tables t
    JOIN pg_statio_user_tables io ON io.relid = t.relid
    LEFT JOIN monitor.table_stats_history p
           ON p.snapshot_id = v_prev AND p.relid = t.relid
    CROSS JOIN LATERAL (
        SELECT p.snapshot_id IS NOT NULL
           AND v_same_epoch
           AND t.seq_scan     >= p.seq_scan
           AND t.seq_tup_read >= p.seq_tup_read
           AND COALESCE(t.idx_scan, 0) >= COALESCE(p.idx_scan, 0)
           AND t.n_tup_ins    >= p.n_tup_ins
           AND t.n_tup_upd    >= p.n_tup_upd
           AND t.n_tup_del    >= p.n_tup_del
           AND COALESCE(io.heap_blks_read, 0) >= COALESCE(p.heap_blks_read, 0)
           AND COALESCE(io.heap_blks_hit, 0)  >= COALESCE(p.heap_blks_hit, 0) AS ok
    ) f;
    GET DIAGNOSTICS v_tables = ROW_COUNT;

    -- 7. index statistics -----------------------------------------------------------
    SELECT max(h.snapshot_id) INTO v_prev
      FROM monitor.index_stats_history h WHERE h.snapshot_id < v_sid;
    v_same_epoch := COALESCE(
        (SELECT p.stats_reset IS NOT DISTINCT FROM v_db_reset
           FROM monitor.db_stat_sample p
          WHERE p.snapshot_id = v_prev AND p.datname = current_database()),
        false);

    INSERT INTO monitor.index_stats_history
        (snapshot_id, indexrelid, relid, schemaname, relname, indexrelname,
         is_unique, is_primary, idx_scan, idx_tup_read, idx_tup_fetch,
         idx_blks_read, idx_blks_hit, index_bytes, idx_scan_delta, idx_tup_read_delta)
    SELECT v_sid, i.indexrelid, i.relid, i.schemaname, i.relname, i.indexrelname,
           x.indisunique, x.indisprimary, i.idx_scan, i.idx_tup_read, i.idx_tup_fetch,
           io.idx_blks_read, io.idx_blks_hit,
           CASE WHEN i.indexrelid <> ALL (v_xlocked) THEN pg_relation_size(i.indexrelid) END,
           CASE WHEN f.ok THEN i.idx_scan     - p.idx_scan     END,
           CASE WHEN f.ok THEN i.idx_tup_read - p.idx_tup_read END
    FROM pg_stat_user_indexes i
    JOIN pg_statio_user_indexes io ON io.indexrelid = i.indexrelid
    JOIN pg_index x ON x.indexrelid = i.indexrelid
    LEFT JOIN monitor.index_stats_history p
           ON p.snapshot_id = v_prev AND p.indexrelid = i.indexrelid
    CROSS JOIN LATERAL (
        SELECT p.snapshot_id IS NOT NULL
           AND v_same_epoch
           AND i.idx_scan     >= p.idx_scan
           AND i.idx_tup_read >= p.idx_tup_read AS ok
    ) f;
    GET DIAGNOSTICS v_indexes = ROW_COUNT;

    -- 7b. optional history panels (sql/12_history.sql).  collect_extra() fills
    --     monitor.table_stat_sample and monitor.statement_sample behind
    --     v_table_stats / v_statement_stats; it is idempotent per snapshot, so
    --     calling it here and from scripts/collect.py never duplicates rows.
    IF to_regprocedure('monitor.collect_extra(bigint)') IS NOT NULL THEN
        PERFORM monitor.collect_extra(v_sid);
    END IF;

    -- 8. alert rules, same transaction as the rows they read ------------------
    v_opened := 0;
    v_resolved := 0;
    IF to_regprocedure('monitor.evaluate_alerts(bigint)') IS NOT NULL THEN
        SELECT e.opened, e.resolved INTO v_opened, v_resolved
          FROM monitor.evaluate_alerts(v_sid) e;
        v_opened := COALESCE(v_opened, 0);
        v_resolved := COALESCE(v_resolved, 0);
    END IF;

    -- Extra rule sets live in sql/21_extra_rules.sql and sql/22_more_rules.sql.
    -- Guarded so a database built from only the original files still works.
    IF to_regprocedure('monitor.evaluate_extra_rules(bigint)') IS NOT NULL THEN
        SELECT e.opened, e.resolved INTO v_step_opened, v_step_resolved
          FROM monitor.evaluate_extra_rules(v_sid) e;
        v_opened := v_opened + COALESCE(v_step_opened, 0);
        v_resolved := v_resolved + COALESCE(v_step_resolved, 0);
    END IF;

    IF to_regprocedure('monitor.evaluate_more_rules(bigint)') IS NOT NULL THEN
        SELECT e.opened, e.resolved INTO v_step_opened, v_step_resolved
          FROM monitor.evaluate_more_rules(v_sid) e;
        v_opened := v_opened + COALESCE(v_step_opened, 0);
        v_resolved := v_resolved + COALESCE(v_step_resolved, 0);
    END IF;

    RETURN QUERY SELECT v_sid, v_at, v_sessions, v_edges, v_statements,
                        v_tables, v_indexes, v_opened, v_resolved;
END;
$fn$;

RESET ROLE;

REVOKE ALL ON FUNCTION monitor.capture_snapshot() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION monitor.capture_snapshot() TO monitor_collector;
