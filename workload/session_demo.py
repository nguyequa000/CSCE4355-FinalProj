#!/usr/bin/env python3
"""Session-shape workload for the extra alert rules (Lane F).

Two modes, both connecting as shop_app:

    --mode idle   BEGIN; SELECT 1; then sleep --hold seconds while the session
                  sits 'idle in transaction', then ROLLBACK. This is the
                  workload for monitor.evaluate_extra_rules' idle_in_transaction
                  rule. (The blocked_session demo's *blocker* also sits idle in
                  transaction while it holds a row lock; raising
                  idle_in_transaction there is correct, not a false positive.)

    --mode long   SELECT pg_sleep(--hold): one statement that stays 'active'
                  for the whole hold. This is the workload for long_active_query.

Prints the backend PID and ISO-8601 start/end timestamps and appends one JSON
line per run to --log, so alert evidence can compare injection time with
eligible_at/fired_at/resolved_at from monitor.v_alerts.

Usage:
    python workload/session_demo.py --mode idle --hold 100 --database shopdb_t5 \
        --log docs/results/t5_session_demo.jsonl
    python workload/session_demo.py --mode long --hold 100 --database shopdb_t5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect  # noqa: E402


def iso(ts: datetime | None = None) -> str:
    ts = ts or datetime.now(timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("idle", "long"), required=True,
                    help="idle = idle in transaction; long = one long-running statement")
    ap.add_argument("--database", default="shopdb_t5",
                    help="database name (default shopdb_t5)")
    ap.add_argument("--hold", type=float, default=100.0,
                    help="seconds to hold the state (default 100)")
    ap.add_argument("--log", default="docs/results/t5_session_demo.jsonl",
                    help="JSONL evidence file (default docs/results/t5_session_demo.jsonl)")
    args = ap.parse_args()

    if args.hold < 0:
        ap.error("--hold must be >= 0")

    app_name = f"session_demo_{args.mode}"
    event: dict[str, object] = {
        "mode": args.mode,
        "database": args.database,
        "hold_seconds": args.hold,
        "pid": None,
        "started_at": None,
        "ended_at": None,
        "ok": False,
    }
    conn = None
    rc = 0
    try:
        conn = connect("shop_app", dbname=args.database, application_name=app_name)
        conn.autocommit = True
        event["pid"] = conn.info.backend_pid
        print(f"mode={args.mode} pid={event['pid']} hold={args.hold:g}s", flush=True)

        with conn.cursor() as cur:
            if args.mode == "idle":
                cur.execute("BEGIN")
                cur.execute("SELECT 1")
                # state is now 'idle in transaction'; state_change is ~now.
                event["started_at"] = iso()
                print(f"pid={event['pid']} state=idle in transaction "
                      f"start={event['started_at']}", flush=True)
                time.sleep(args.hold)
                conn.rollback()
            else:
                event["started_at"] = iso()
                print(f"pid={event['pid']} state=active query=pg_sleep "
                      f"start={event['started_at']}", flush=True)
                cur.execute("SELECT pg_sleep(%s)", (args.hold,))
                cur.fetchone()
        event["ended_at"] = iso()
        event["ok"] = True
        print(f"pid={event['pid']} end={event['ended_at']} ok=true", flush=True)
    except KeyboardInterrupt:
        rc = 130
        event["error"] = "interrupted"
        print("interrupted; rolling back", file=sys.stderr, flush=True)
    except (psycopg.Error, OSError) as exc:
        rc = 1
        event["error"] = f"{type(exc).__name__}: {exc}"
        print(f"ERROR: {event['error']}", file=sys.stderr, flush=True)
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if event["ended_at"] is None:
            event["ended_at"] = iso()
        log_path = Path(args.log)
        if not log_path.is_absolute():
            log_path = ROOT / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        print(f"event_log={log_path}", flush=True)

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
