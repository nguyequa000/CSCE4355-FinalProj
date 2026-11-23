#!/usr/bin/env python3
"""Reproducible lock-contention workload for the blocked-session alert rule.

The blocker opens a transaction and updates one seeded shop.customers row.
A second connection (the waiter) runs the same UPDATE and blocks behind it.
The blocker holds the row lock for --hold seconds (default 150 s, deliberately
spanning more than two 60 s collector intervals), then both transactions are
rolled back.

Prints ISO-8601 timestamps for injection (waiter actually observed waiting on a
lock) and release, both PIDs, and appends one JSON line to --log so evidence can
separate injection, alert eligibility/firing, and recovery.

Usage:
    python workload/lock_demo.py --database shopdb_t3 --hold 150
    python workload/lock_demo.py --hold 5 --log docs/results/lock_demo_events.jsonl

Cleanup runs in a finally block: Ctrl+C while holding still rolls both
transactions back and unblocks the waiter.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect  # noqa: E402

CUSTOMER_ID = 42
UPDATE_SQL = "UPDATE shop.customers SET balance = balance + 1 WHERE customer_id = %s"


def iso(ts: datetime | None = None) -> str:
    """UTC ISO-8601 with milliseconds, e.g. 2026-11-22T22:10:01.234+00:00."""
    ts = ts or datetime.now(timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def wait_for_lock(observer, waiter_pid: int, timeout: float = 10.0) -> bool:
    """Poll pg_stat_activity until waiter_pid is waiting on a heavyweight lock.

    shop_app may inspect its own sessions; pg_blocking_pids() is callable by any
    role and confirms there is a real blocker edge, not just a wait event name.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = observer.execute(
            """
            SELECT state, wait_event_type, wait_event,
                   cardinality(pg_blocking_pids(pid)) AS nblockers
              FROM pg_stat_activity
             WHERE pid = %s
            """,
            (waiter_pid,),
        ).fetchone()
        if row and row[1] == "Lock" and row[3] > 0:
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="shopdb",
                    help="database name (default: shopdb; use shopdb_t3 for lane tests)")
    ap.add_argument("--hold", type=float, default=150.0,
                    help="seconds to hold the row lock (default 150, >2 collector intervals)")
    ap.add_argument("--log", default="docs/results/lock_demo_events.jsonl",
                    help="JSONL evidence file (default: docs/results/lock_demo_events.jsonl)")
    ap.add_argument("--customer-id", type=int, default=CUSTOMER_ID,
                    help="seeded shop.customers row to contend on (default 42)")
    args = ap.parse_args()

    if args.hold < 0:
        ap.error("--hold must be >= 0")

    blocker = None
    waiter = None
    waiter_thread: threading.Thread | None = None
    waiter_state: dict[str, object] = {}
    event: dict[str, object] = {
        "database": args.database,
        "customer_id": args.customer_id,
        "hold_seconds": args.hold,
        "injected_at": None,
        "released_at": None,
        "waiter_finished_at": None,
        "blocker_pid": None,
        "waiter_pid": None,
        "ok": False,
    }
    rc = 0

    def waiter_body() -> None:
        """Separate connection: run the same UPDATE and block behind the blocker."""
        nonlocal waiter
        try:
            waiter = connect("shop_app", dbname=args.database,
                             application_name="lock_demo_waiter")
            waiter.autocommit = True
            with waiter.cursor() as cur:
                # Generous compared with the hold so a healthy run never trips it.
                cur.execute("SELECT set_config('lock_timeout', %s, false)",
                            (f"{int(args.hold) + 60}s",))
                cur.execute("BEGIN")
                waiter_state["started_at"] = datetime.now(timezone.utc)
                # This call blocks on the blocker's row lock; the timestamp just
                # before it is the injection time.
                cur.execute(UPDATE_SQL, (args.customer_id,))
                waiter_state["finished_at"] = datetime.now(timezone.utc)
            waiter.rollback()
            waiter.close()
        except BaseException as exc:  # noqa: BLE001 - recorded as evidence
            waiter_state["error"] = f"{type(exc).__name__}: {exc}"
            try:
                if waiter is not None:
                    waiter.rollback()
                    waiter.close()
            except Exception:
                pass

    try:
        blocker = connect("shop_app", dbname=args.database,
                          application_name="lock_demo_blocker")
        blocker.autocommit = True
        with blocker.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(UPDATE_SQL, (args.customer_id,))
        event["blocker_pid"] = blocker.info.backend_pid
        print(f"blocker_pid={blocker.info.backend_pid} holds row customer_id={args.customer_id}")

        waiter_thread = threading.Thread(target=waiter_body, name="lock_demo_waiter", daemon=True)
        waiter_thread.start()

        # Wait until the waiter is genuinely blocked before declaring injection.
        observer = connect("shop_app", dbname=args.database,
                           application_name="lock_demo_observer")
        try:
            deadline = time.monotonic() + 15.0
            while event["waiter_pid"] is None and time.monotonic() < deadline:
                if "error" in waiter_state:
                    break
                # The waiter's backend pid appears once its connection exists;
                # find it by application_name (only this demo uses that name).
                row = observer.execute(
                    "SELECT pid FROM pg_stat_activity WHERE application_name = %s",
                    ("lock_demo_waiter",),
                ).fetchone()
                if row:
                    event["waiter_pid"] = row[0]
                time.sleep(0.05)
        finally:
            observer.close()

        if event["waiter_pid"] is None:
            raise RuntimeError(f"waiter connection did not appear: {waiter_state}")

        injected = wait_for_lock(blocker, int(event["waiter_pid"]), timeout=10.0)
        if not injected:
            raise RuntimeError(f"waiter did not reach a lock wait: {waiter_state}")
        injected_at = datetime.now(timezone.utc)
        event["injected_at"] = iso(injected_at)
        print(f"waiter_pid={event['waiter_pid']} injection={event['injected_at']} "
              f"(blocked by blocker_pid={event['blocker_pid']})")

        print(f"holding for {args.hold:g}s ...")
        time.sleep(args.hold)

        released_at = datetime.now(timezone.utc)
        event["released_at"] = iso(released_at)
        blocker.rollback()
        print(f"release={event['released_at']}")

        if waiter_thread is not None:
            waiter_thread.join(timeout=30.0)
        if waiter_thread is not None and waiter_thread.is_alive():
            waiter_state["error"] = "waiter did not finish within 30 s after release"
            raise RuntimeError(waiter_state["error"])

        event["waiter_finished_at"] = iso(waiter_state.get("finished_at")) \
            if waiter_state.get("finished_at") else None
        if "error" in waiter_state:
            raise RuntimeError(str(waiter_state["error"]))
        event["ok"] = True
        print(f"waiter_finished={event['waiter_finished_at']} ok=true")
    except KeyboardInterrupt:
        rc = 130
        event["error"] = "interrupted"
        print("interrupted; cleaning up", file=sys.stderr)
    except BaseException as exc:  # noqa: BLE001
        rc = 1
        event["error"] = f"{type(exc).__name__}: {exc}"
        print(f"ERROR: {event['error']}", file=sys.stderr)
    finally:
        # Cleanup must always run, including Ctrl+C: releasing the blocker's
        # transaction is what unblocks the waiter. The waiter thread owns its
        # own connection and rolls it back; the main thread must not touch a
        # connection another thread is still using.
        if blocker is not None:
            try:
                blocker.rollback()
            except Exception as exc:  # noqa: BLE001
                print(f"cleanup rollback blocker failed: {exc}", file=sys.stderr)
            finally:
                try:
                    blocker.close()
                except Exception:
                    pass
        if waiter_thread is not None:
            waiter_thread.join(timeout=10.0)
        if waiter is not None and (waiter_thread is None or not waiter_thread.is_alive()):
            try:
                waiter.rollback()
                waiter.close()
            except Exception:
                pass

        log_path = Path(args.log)
        if not log_path.is_absolute():
            log_path = ROOT / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        print(f"event_log={log_path}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
