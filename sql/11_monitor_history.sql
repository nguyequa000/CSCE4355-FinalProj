-- 11_monitor_history.sql  (run as superuser, after sql/10_monitor.sql)
-- Owner: M2 (Dustin Dowell)
--
-- Completes the proposal's history tables. sql/10_monitor.sql already provides
-- the session, lock and database history under these names:
--
--   proposal name            table in this build
--   -----------------------  ---------------------------------
--   snapshot                 monitor.snapshot
--   session_history          monitor.session_sample
--   lock_history             monitor.lock_wait
--   database_stats_history   monitor.db_stat_sample
--   statement_history        monitor.statement_history      (this file)
--   table_stats_history      monitor.table_stats_history    (this file)
--   index_stats_history      monitor.index_stats_history    (this file)
--
-- Conventions (same as 10_monitor.sql):
--   * every history row belongs to one snapshot (ON DELETE CASCADE), so the
--     retention job deletes a snapshot and all of its detail rows as one unit;
--   * cumulative counters are stored as read, plus *_delta columns against the
--     previous snapshot. A delta is NULL when there is no previous row, the
--     statistics were reset, or a counter went down -- never negative.
-- Filled by monitor.capture_snapshot() (sql/12_capture_snapshot.sql).
SET ROLE monitor_collector;

-- time-range lookups ("last 15 minutes") from the dashboard and analysis views
CREATE INDEX IF NOT EXISTS snapshot_captured_at_idx ON monitor.snapshot (captured_at);

-- ---------------------------------------------------------------------------
-- pg_stat_statements, current database, top 500 statements by total time
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitor.statement_history (
    snapshot_id            bigint  NOT NULL REFERENCES monitor.snapshot ON DELETE CASCADE,
    queryid                bigint  NOT NULL,
    userid                 oid     NOT NULL,
    toplevel               boolean NOT NULL,
    rolname                text,
    query_text             text,             -- normalized text ($1 placeholders), first 500 chars
    calls                  bigint  NOT NULL,
    total_exec_time        double precision NOT NULL,   -- ms
    mean_exec_time         double precision NOT NULL,   -- ms
    rows                   bigint  NOT NULL,
    shared_blks_hit        bigint  NOT NULL,
    shared_blks_read       bigint  NOT NULL,
    temp_blks_written      bigint  NOT NULL,
    stats_reset            timestamptz,      -- pg_stat_statements_info.stats_reset at capture
    calls_delta            bigint,
    total_exec_time_delta  double precision,
    rows_delta             bigint,
    shared_blks_hit_delta  bigint,
    shared_blks_read_delta bigint,
    PRIMARY KEY (snapshot_id, queryid, userid, toplevel)
);
CREATE INDEX IF NOT EXISTS statement_history_queryid_idx
    ON monitor.statement_history (queryid, snapshot_id);

-- ---------------------------------------------------------------------------
-- pg_stat_user_tables + pg_statio_user_tables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitor.table_stats_history (
    snapshot_id          bigint NOT NULL REFERENCES monitor.snapshot ON DELETE CASCADE,
    relid                oid    NOT NULL,
    schemaname           text   NOT NULL,
    relname              text   NOT NULL,
    seq_scan             bigint,
    seq_tup_read         bigint,
    idx_scan             bigint,          -- NULL when the table has no index
    idx_tup_fetch        bigint,
    n_tup_ins            bigint,
    n_tup_upd            bigint,
    n_tup_del            bigint,
    n_tup_hot_upd        bigint,
    n_live_tup           bigint,
    n_dead_tup           bigint,
    n_mod_since_analyze  bigint,
    heap_blks_read       bigint,
    heap_blks_hit        bigint,
    last_vacuum          timestamptz,
    last_autovacuum      timestamptz,
    last_analyze         timestamptz,
    last_autoanalyze     timestamptz,
    vacuum_count         bigint,
    autovacuum_count     bigint,
    total_bytes          bigint,          -- pg_total_relation_size; NULL if skipped (exclusive lock pending)
    seq_scan_delta       bigint,
    seq_tup_read_delta   bigint,
    idx_scan_delta       bigint,
    n_tup_ins_delta      bigint,
    n_tup_upd_delta      bigint,
    n_tup_del_delta      bigint,
    heap_blks_read_delta bigint,
    heap_blks_hit_delta  bigint,
    PRIMARY KEY (snapshot_id, relid)
);
CREATE INDEX IF NOT EXISTS table_stats_history_relid_idx
    ON monitor.table_stats_history (relid, snapshot_id);

-- ---------------------------------------------------------------------------
-- pg_stat_user_indexes + pg_statio_user_indexes + pg_index flags
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitor.index_stats_history (
    snapshot_id         bigint NOT NULL REFERENCES monitor.snapshot ON DELETE CASCADE,
    indexrelid          oid    NOT NULL,
    relid               oid    NOT NULL,
    schemaname          text   NOT NULL,
    relname             text   NOT NULL,
    indexrelname        text   NOT NULL,
    is_unique           boolean NOT NULL,
    is_primary          boolean NOT NULL,
    idx_scan            bigint,
    idx_tup_read        bigint,
    idx_tup_fetch       bigint,
    idx_blks_read       bigint,
    idx_blks_hit        bigint,
    index_bytes         bigint,           -- pg_relation_size; NULL if skipped (exclusive lock pending)
    idx_scan_delta      bigint,
    idx_tup_read_delta  bigint,
    PRIMARY KEY (snapshot_id, indexrelid)
);
CREATE INDEX IF NOT EXISTS index_stats_history_indexrelid_idx
    ON monitor.index_stats_history (indexrelid, snapshot_id);

-- ---------------------------------------------------------------------------
-- reader-facing views (base tables are never granted to monitor_reader)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW monitor.v_statement_history AS
    SELECT h.snapshot_id, n.captured_at, h.queryid, h.rolname, h.toplevel, h.query_text,
           h.calls, h.total_exec_time, h.mean_exec_time, h.rows,
           h.shared_blks_hit, h.shared_blks_read, h.temp_blks_written,
           h.calls_delta, h.total_exec_time_delta, h.rows_delta,
           h.shared_blks_hit_delta, h.shared_blks_read_delta
    FROM monitor.statement_history h JOIN monitor.snapshot n USING (snapshot_id);

CREATE OR REPLACE VIEW monitor.v_table_stats_history AS
    SELECT h.snapshot_id, n.captured_at, h.relid, h.schemaname, h.relname,
           h.seq_scan, h.seq_tup_read, h.idx_scan, h.idx_tup_fetch,
           h.n_tup_ins, h.n_tup_upd, h.n_tup_del, h.n_tup_hot_upd,
           h.n_live_tup, h.n_dead_tup, h.n_mod_since_analyze,
           h.heap_blks_read, h.heap_blks_hit,
           h.last_vacuum, h.last_autovacuum, h.last_analyze, h.last_autoanalyze,
           h.vacuum_count, h.autovacuum_count, h.total_bytes,
           h.seq_scan_delta, h.seq_tup_read_delta, h.idx_scan_delta,
           h.n_tup_ins_delta, h.n_tup_upd_delta, h.n_tup_del_delta,
           h.heap_blks_read_delta, h.heap_blks_hit_delta
    FROM monitor.table_stats_history h JOIN monitor.snapshot n USING (snapshot_id);

CREATE OR REPLACE VIEW monitor.v_index_stats_history AS
    SELECT h.snapshot_id, n.captured_at, h.indexrelid, h.relid, h.schemaname, h.relname,
           h.indexrelname, h.is_unique, h.is_primary,
           h.idx_scan, h.idx_tup_read, h.idx_tup_fetch, h.idx_blks_read, h.idx_blks_hit,
           h.index_bytes, h.idx_scan_delta, h.idx_tup_read_delta
    FROM monitor.index_stats_history h JOIN monitor.snapshot n USING (snapshot_id);

RESET ROLE;

GRANT SELECT ON monitor.v_statement_history, monitor.v_table_stats_history,
                monitor.v_index_stats_history
      TO monitor_reader;
