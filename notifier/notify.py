#!/usr/bin/env python3
"""Console notifier for monitor.alert_log.

Connects as monitor_collector (owner of alert_log), and in one transaction:

  * prints one line for every fired alert that has not been notified yet, then
    stamps notified_at = now();
  * prints one line for every resolved alert whose resolution has not been
    notified yet, then stamps resolve_notified_at = now().

Both queries use SELECT ... FOR UPDATE SKIP LOCKED, so two notifier processes
can run concurrently without double-notifying: a row locked by one is skipped
by the other, and the stamp commits with the print.

Usage:
    python notifier/notify.py --once --database shopdb_t3
    python notifier/notify.py --interval 30 --log docs/results/notify.log
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect  # noqa: E402

FIRED_SQL = """
    SELECT alert_id, rule_name, severity, blocked_pid, blocker_pid,
           fired_at, observed_seconds
      FROM monitor.alert_log
     WHERE notified_at IS NULL
     ORDER BY alert_id
     FOR UPDATE SKIP LOCKED
"""

RESOLVED_SQL = """
    SELECT alert_id, resolved_at
      FROM monitor.alert_log
     WHERE resolved_at IS NOT NULL
       AND resolve_notified_at IS NULL
     ORDER BY alert_id
     FOR UPDATE SKIP LOCKED
"""


def notify_once(conn, emit) -> tuple[int, int]:
    """One notification pass inside a single transaction. Returns counts."""
    fired = 0
    resolved = 0
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(FIRED_SQL)
            fired_rows = cur.fetchall()
            for (alert_id, rule, severity, blocked_pid, blocker_pid,
                 fired_at, observed) in fired_rows:
                emit(
                    f"[ALERT FIRED] id={alert_id} rule={rule} severity={severity} "
                    f"blocked_pid={blocked_pid} blocker_pid={blocker_pid} "
                    f"fired_at={fired_at.isoformat()} observed={observed}s"
                )
                cur.execute(
                    "UPDATE monitor.alert_log SET notified_at = now() WHERE alert_id = %s",
                    (alert_id,),
                )
                fired += 1

            cur.execute(RESOLVED_SQL)
            resolved_rows = cur.fetchall()
            for alert_id, resolved_at in resolved_rows:
                emit(f"[ALERT RESOLVED] id={alert_id} resolved_at={resolved_at.isoformat()}")
                cur.execute(
                    "UPDATE monitor.alert_log SET resolve_notified_at = now() "
                    "WHERE alert_id = %s",
                    (alert_id,),
                )
                resolved += 1
    return fired, resolved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="one pass, then exit (default)")
    mode.add_argument("--interval", type=float, metavar="N",
                      help="keep running, one pass every N seconds")
    ap.add_argument("--database", default="shopdb",
                    help="database name (default: shopdb; use shopdb_t3 for lane tests)")
    ap.add_argument("--log", default=None,
                    help="optional file to append the same console lines to")
    args = ap.parse_args()

    if args.interval is not None and args.interval <= 0:
        ap.error("--interval must be > 0")

    emit_targets = []

    def emit(line: str) -> None:
        print(line, flush=True)
        for fh in emit_targets:
            fh.write(line + "\n")
            fh.flush()

    if args.log:
        log_path = Path(args.log)
        if not log_path.is_absolute():
            log_path = ROOT / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        emit_targets.append(log_path.open("a", encoding="utf-8"))

    conn = connect("monitor_collector", dbname=args.database,
                   application_name="alert_notifier")
    try:
        if args.interval is None:
            notify_once(conn, emit)
            return 0
        while True:
            notify_once(conn, emit)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()
        for fh in emit_targets:
            fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
