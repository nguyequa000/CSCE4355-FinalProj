#!/usr/bin/env python3
"""Scheduled monitoring collector for the shop database.

Modes
-----
    python scripts/collect.py --once [--db NAME]
    python scripts/collect.py --interval 60 [--count N] [--db NAME]

This is the proposal's Python scheduler fallback, for servers without pg_cron
(the Windows local cluster).  In the Docker environment pg_cron calls
monitor.capture_snapshot() every minute instead -- do not run both.  The
loop is aligned to a fixed cadence: tick N happens at start + N*interval, so
collection runtime does not accumulate into drift.

Every snapshot is ONE transaction.  When monitor.capture_snapshot() exists
(sql/12_capture_snapshot.sql, M3) the collector simply calls it, so pg_cron and
this script produce identical history:
  1. monitor.snapshot             (captured_at = clock_timestamp())
  2. monitor.session_sample       pg_stat_activity, client backends of this DB
  3. monitor.lock_wait            one row per (waiter, blocker) edge
  4. monitor.db_stat_sample       pg_stat_database counters + deltas
  5. monitor.statement_history    pg_stat_statements         (function only)
  6. monitor.table_stats_history  pg_stat_user_tables        (function only)
  7. monitor.index_stats_history  pg_stat_user_indexes       (function only)
  8. monitor.evaluate_alerts()    only if that function exists
On a database built before that function existed, steps 1-4 and 8 run from the
inline SQL below.

No credentials are stored.  Query text is truncated to 60 characters by the
schema contract; the collector never samples literal values itself.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python scripts/collect.py` from anywhere
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect, load_env  # noqa: E402

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# One row per client backend in THIS database, excluding the collector itself.
# blocked_by uses pg_blocking_pids(); query text is clipped, never stored whole.
INSERT_SESSIONS = """
INSERT INTO monitor.session_sample
    (snapshot_id, pid, usename, application_name, state, wait_event_type,
     wait_event, xact_start, query_start, state_change, blocked_by,
     query_excerpt)
SELECT %(sid)s, a.pid, a.usename, a.application_name, a.state,
       a.wait_event_type, a.wait_event, a.xact_start, a.query_start,
       a.state_change, pg_blocking_pids(a.pid), left(a.query, 60)
FROM pg_stat_activity a
WHERE a.backend_type = 'client backend'
  AND a.datname = current_database()
  AND a.pid <> pg_backend_pid()
"""

# One row per (waiter, blocker) edge, rebuilt from live pg_stat_activity in the
# same snapshot so the blocker's user/app/state are captured even when the
# blocker is not itself a sampled client backend of this database.
INSERT_LOCK_WAITS = """
WITH snap AS (
    SELECT captured_at FROM monitor.snapshot WHERE snapshot_id = %(sid)s
)
INSERT INTO monitor.lock_wait
    (snapshot_id, blocked_pid, blocker_pid, blocked_user, blocker_user,
     blocked_app, blocker_app, blocker_state, wait_started, waited_seconds)
SELECT %(sid)s, w.pid, bp.pid,
       w.usename, b.usename, w.application_name, b.application_name, b.state,
       w.state_change,
       round(extract(epoch FROM (snap.captured_at - w.state_change))::numeric, 1)
FROM pg_stat_activity w
CROSS JOIN snap
CROSS JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS bp(pid)
LEFT JOIN pg_stat_activity b ON b.pid = bp.pid
WHERE w.backend_type = 'client backend'
  AND w.datname = current_database()
  AND w.pid <> pg_backend_pid()
"""

# Counters for the current database plus deltas against the previous sample of
# the same datname.  A delta is NULL when: there is no previous sample, the
# stats were reset, or ANY counter decreased (so an interval delta is never
# negative and never silently mixes two stats epochs).
INSERT_DB_STAT = """
WITH cur AS (
    SELECT d.datname, d.numbackends, d.xact_commit, d.xact_rollback,
           d.blks_read, d.blks_hit, d.deadlocks, d.stats_reset
    FROM pg_stat_database d
    WHERE d.datname = current_database()
), prev AS (
    SELECT p.*
    FROM monitor.db_stat_sample p
    WHERE p.datname = (SELECT datname FROM cur)
      AND p.snapshot_id < %(sid)s
    ORDER BY p.snapshot_id DESC
    LIMIT 1
), flagged AS (
    SELECT c.*, p.snapshot_id AS prev_snapshot_id,
           (p.snapshot_id IS NOT NULL
            AND p.stats_reset IS DISTINCT FROM c.stats_reset) AS reset_seen,
           (p.snapshot_id IS NOT NULL
            AND (c.xact_commit   < p.xact_commit
              OR c.xact_rollback < p.xact_rollback
              OR c.blks_read     < p.blks_read
              OR c.blks_hit      < p.blks_hit)) AS decreased
    FROM cur c
    LEFT JOIN prev p ON true
)
INSERT INTO monitor.db_stat_sample
    (snapshot_id, datname, numbackends, xact_commit, xact_rollback, blks_read,
     blks_hit, deadlocks, stats_reset, xact_commit_delta, xact_rollback_delta,
     blks_read_delta, blks_hit_delta)
SELECT %(sid)s, f.datname, f.numbackends, f.xact_commit, f.xact_rollback,
       f.blks_read, f.blks_hit, f.deadlocks, f.stats_reset,
       CASE WHEN f.prev_snapshot_id IS NULL OR f.reset_seen OR f.decreased
            THEN NULL ELSE f.xact_commit - p.xact_commit END,
       CASE WHEN f.prev_snapshot_id IS NULL OR f.reset_seen OR f.decreased
            THEN NULL ELSE f.xact_rollback - p.xact_rollback END,
       CASE WHEN f.prev_snapshot_id IS NULL OR f.reset_seen OR f.decreased
            THEN NULL ELSE f.blks_read - p.blks_read END,
       CASE WHEN f.prev_snapshot_id IS NULL OR f.reset_seen OR f.decreased
            THEN NULL ELSE f.blks_hit - p.blks_hit END
FROM flagged f
LEFT JOIN monitor.db_stat_sample p
       ON p.snapshot_id = f.prev_snapshot_id AND p.datname = f.datname
"""

EVALUATE_ALERTS_EXISTS = """
SELECT to_regprocedure('monitor.evaluate_alerts(bigint)') IS NOT NULL
"""

CAPTURE_FUNCTION_EXISTS = """
SELECT to_regprocedure('monitor.capture_snapshot()') IS NOT NULL
"""

CAPTURE_SNAPSHOT = """
SELECT snapshot_id, captured_at, sessions, lock_edges, statements, tables,
       indexes, alerts_opened, alerts_resolved
FROM monitor.capture_snapshot()
"""


class SnapshotError(RuntimeError):
    """A snapshot could not be captured (transaction already rolled back)."""


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def take_snapshot(conn: psycopg.Connection, log=print) -> dict:
    """Capture one consistent snapshot in a single transaction.

    Returns a summary dict and prints exactly one line to stdout:
        snapshot_id=.. captured_at=.. sessions=.. lock_edges=..
        alerts_opened=.. alerts_resolved=.. evaluate_alerts=..
        [statements=.. tables=.. indexes=..]   (capture_snapshot() path)
    Raises SnapshotError on failure (transaction rolled back, connection usable).
    """
    conn.rollback()  # discard anything left over from a previous failed attempt
    try:
        if conn.execute(CAPTURE_FUNCTION_EXISTS).fetchone()[0]:
            summary = _capture_in_database(conn)
        else:
            summary = _capture_inline(conn)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the loop
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - connection already broken
            pass
        raise SnapshotError(str(exc)) from exc

    log(format_line(summary))
    return summary


def _capture_in_database(conn: psycopg.Connection) -> dict:
    """One snapshot via monitor.capture_snapshot() (caller commits)."""
    (snapshot_id, captured_at, sessions, edges, statements, tables, indexes,
     opened, resolved) = conn.execute(CAPTURE_SNAPSHOT).fetchone()
    return {
        "snapshot_id": snapshot_id,
        "captured_at": captured_at,
        "sessions": sessions,
        "lock_edges": edges,
        "alerts_opened": opened,
        "alerts_resolved": resolved,
        "evaluate_alerts": conn.execute(EVALUATE_ALERTS_EXISTS).fetchone()[0],
        "statements": statements,
        "tables": tables,
        "indexes": indexes,
    }


def _capture_inline(conn: psycopg.Connection) -> dict:
    """One snapshot from the inline SQL above (pre-capture_snapshot databases)."""
    snapshot_id, captured_at = conn.execute(
        "INSERT INTO monitor.snapshot (captured_at) "
        "VALUES (clock_timestamp()) RETURNING snapshot_id, captured_at"
    ).fetchone()

    conn.execute(INSERT_SESSIONS, {"sid": snapshot_id})
    sessions = conn.execute(
        "SELECT count(*) FROM monitor.session_sample WHERE snapshot_id = %s",
        (snapshot_id,),
    ).fetchone()[0]

    conn.execute(INSERT_LOCK_WAITS, {"sid": snapshot_id})
    edges = conn.execute(
        "SELECT count(*) FROM monitor.lock_wait WHERE snapshot_id = %s",
        (snapshot_id,),
    ).fetchone()[0]

    conn.execute(INSERT_DB_STAT, {"sid": snapshot_id})

    has_alerts = conn.execute(EVALUATE_ALERTS_EXISTS).fetchone()[0]
    opened = resolved = None
    if has_alerts:
        result = conn.execute(
            "SELECT opened, resolved FROM monitor.evaluate_alerts(%s)",
            (snapshot_id,),
        ).fetchone()
        opened, resolved = result if result is not None else (0, 0)

    return {
        "snapshot_id": snapshot_id,
        "captured_at": captured_at,
        "sessions": sessions,
        "lock_edges": edges,
        "alerts_opened": opened,
        "alerts_resolved": resolved,
        "evaluate_alerts": has_alerts,
    }


def format_line(s: dict) -> str:
    captured = s["captured_at"]
    captured_txt = captured.isoformat() if isinstance(captured, datetime) else str(captured)
    opened = "na" if s["alerts_opened"] is None else s["alerts_opened"]
    resolved = "na" if s["alerts_resolved"] is None else s["alerts_resolved"]
    line = (
        f"snapshot_id={s['snapshot_id']} captured_at={captured_txt} "
        f"sessions={s['sessions']} lock_edges={s['lock_edges']} "
        f"alerts_opened={opened} alerts_resolved={resolved} "
        f"evaluate_alerts={'present' if s['evaluate_alerts'] else 'absent'}"
    )
    # appended (not interleaved) so existing log parsers keep matching
    if "statements" in s:
        line += f" statements={s['statements']} tables={s['tables']} indexes={s['indexes']}"
    return line


# ---------------------------------------------------------------------------
# CLI / scheduler
# ---------------------------------------------------------------------------

def _default_interval(env: dict[str, str]) -> float:
    raw = env.get("COLLECT_INTERVAL_SECONDS", "60").strip() or "60"
    try:
        value = float(raw)
    except ValueError as exc:
        raise SystemExit(f"COLLECT_INTERVAL_SECONDS is not numeric: {raw!r}") from exc
    if value <= 0:
        raise SystemExit(f"COLLECT_INTERVAL_SECONDS must be > 0, got {value}")
    return value


def run_loop(interval: float, count: int | None, dbname: str | None, log=print) -> int:
    """Capture snapshots on a fixed cadence; returns process exit code."""
    conn: psycopg.Connection | None = None
    tick = 0
    failures = 0
    print(
        f"collector: db={dbname or 'env-default'} interval={interval:g}s "
        f"count={count if count is not None else 'unlimited'}",
        file=sys.stderr,
        flush=True,
    )
    start = time.monotonic()
    try:
        while count is None or tick < count:
            try:
                if conn is None or conn.closed:
                    conn = connect("monitor_collector", dbname=dbname)
                take_snapshot(conn, log=log)
                failures = 0
            except (SnapshotError, psycopg.Error, OSError) as exc:
                failures += 1
                print(f"collector: snapshot failed (tick {tick}): {exc}",
                      file=sys.stderr, flush=True)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # pragma: no cover
                        pass
                    conn = None
            tick += 1
            if count is not None and tick >= count:
                break
            # fixed cadence: sleep until start + tick*interval, not interval+runtime
            delay = (start + tick * interval) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        print("collector: interrupted", file=sys.stderr, flush=True)
    finally:
        if conn is not None:
            conn.close()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    env = load_env()
    parser = argparse.ArgumentParser(
        description="Capture PostgreSQL monitoring snapshots into monitor.* history."
    )
    parser.add_argument("--once", action="store_true",
                        help="take exactly one snapshot and exit")
    parser.add_argument("--interval", type=float, default=None,
                        help="seconds between snapshots in loop mode "
                             "(default: COLLECT_INTERVAL_SECONDS, 60)")
    parser.add_argument("--count", type=int, default=0,
                        help="number of snapshots in loop mode; 0 = run until stopped")
    parser.add_argument("--db", default=None,
                        help="database name (default: PGDATABASE from .env, shopdb)")
    args = parser.parse_args(argv)

    if args.once and args.interval is not None:
        parser.error("--once and --interval are mutually exclusive")
    if args.once and args.count:
        parser.error("--once and --count are mutually exclusive")
    if args.count < 0:
        parser.error("--count must be >= 0")

    dbname = args.db or env.get("PGDATABASE") or "shopdb"

    if args.once:
        conn = None
        try:
            conn = connect("monitor_collector", dbname=dbname)
            take_snapshot(conn)
        except (SnapshotError, psycopg.Error, OSError) as exc:
            print(f"collector: snapshot failed: {exc}", file=sys.stderr, flush=True)
            return 1
        finally:
            if conn is not None:
                conn.close()
        return 0

    interval = args.interval if args.interval is not None else _default_interval(env)
    if interval <= 0:
        parser.error("--interval must be > 0")
    count = args.count if args.count > 0 else None
    return run_loop(interval, count, dbname)


if __name__ == "__main__":
    raise SystemExit(main())
