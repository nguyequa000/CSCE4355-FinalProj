#!/usr/bin/env python3
"""Live end-to-end verification of the three sample-based alert rules.

Drives REAL load against a disposable database and takes snapshots with the real
collector entry point (monitor.capture_snapshot(), which calls evaluate_alerts()).
It never calls evaluate_more_rules() directly and never edits alert_rule: all
thresholds are the shipped defaults.  Each rule must OPEN on the violating
snapshot and RESOLVE on a later clean one, or the script exits non-zero.

    python workload/live_verify_more_rules.py --database shopdb_live | tee docs/results/live_more_rules.txt

Every scenario starts from a freshly dropped and rebuilt database (scripts/setup_db.sh),
because the cache rule judges a five-minute window and earlier activity in the same
database would dilute it.  Pass --no-rebuild to reuse the database as it is.

  dead_tuple_risk        UPDATE 3000 of 10000 shop.orders rows (workload/stress_demo.py);
                         opens; VACUUM shop.orders; resolves.
  connection_saturation  hold >= 80 % of max_connections as shop_app connections;
                         opens; close them; resolves.
  cache_hit_ratio        restart the cluster (cold shared buffers), seq-scan a ~100 MB
                         scratch table (mostly disk reads); opens; run the cached
                         read_heavy pgbench profile; ratio recovers; resolves.

The cache scenario restarts the local cluster started by scripts/start_cluster.sh.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect, load_env  # noqa: E402

PGBIN = Path("C:/Program Files/PostgreSQL/17/bin")
FAILURES: list[str] = []


def say(event: str, **fields) -> None:
    print(json.dumps({"t": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                      "event": event, **fields}, default=str), flush=True)


def check(label: str, ok: bool, **fields) -> None:
    say("PASS" if ok else "FAIL", check=label, **fields)
    if not ok:
        FAILURES.append(label)


def snapshot(db: str) -> dict:
    with connect("monitor_collector", dbname=db, autocommit=True) as c:
        r = c.execute("SELECT * FROM monitor.capture_snapshot()").fetchone()
    snap = {"snapshot_id": r[0], "captured_at": r[1], "opened": r[-2], "resolved": r[-1]}
    say("snapshot", **snap)
    return snap


def alerts(db: str, rule: str) -> list[dict]:
    with connect("postgres", dbname=db, autocommit=True) as c:
        rows = c.execute(
            """SELECT alert_id, rule_name, severity, blocked_pid, fired_at, resolved_at,
                      fired_snapshot_id, resolved_snapshot_id
                 FROM monitor.alert_log WHERE rule_name = %s ORDER BY alert_id""", (rule,)
        ).fetchall()
    cols = ["alert_id", "rule", "severity", "key", "fired_at", "resolved_at",
            "fired_snapshot", "resolved_snapshot"]
    return [dict(zip(cols, r)) for r in rows]


def sql(db: str, stmt: str, *params):
    with connect("postgres", dbname=db, autocommit=True) as c:
        cur = c.execute(stmt, params or None)
        return cur.fetchall() if cur.description else None


def fresh_database(db: str) -> None:
    with connect("postgres", dbname="postgres", autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    subprocess.run(["bash", str(ROOT / "scripts" / "setup_db.sh"), db], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)
    say("database_rebuilt", database=db)


def settle(seconds: float = 2.0) -> None:
    time.sleep(seconds)  # let backends flush their cumulative statistics


def dead_tuple_risk(db: str) -> None:
    say("scenario_start", rule="dead_tuple_risk")
    snapshot(db)
    before = alerts(db, "dead_tuple_risk")
    check("dead_tuple_risk: no alert before the workload", not before)
    subprocess.run([sys.executable, str(ROOT / "workload" / "stress_demo.py"),
                    "--database", db, "--rows", "3000"], check=True)
    settle()
    snap = snapshot(db)
    a = alerts(db, "dead_tuple_risk")
    check("dead_tuple_risk: opens on the violating snapshot",
          len(a) == 1 and a[0]["resolved_at"] is None and a[0]["fired_snapshot"] == snap["snapshot_id"],
          alerts=a)
    sql(db, "VACUUM shop.orders")
    settle()
    snap = snapshot(db)
    a = alerts(db, "dead_tuple_risk")
    check("dead_tuple_risk: resolves after VACUUM",
          len(a) == 1 and a[0]["resolved_at"] is not None
          and a[0]["resolved_snapshot"] == snap["snapshot_id"], alerts=a)


def connection_saturation(db: str) -> None:
    say("scenario_start", rule="connection_saturation")
    max_conn = int(sql(db, "SHOW max_connections")[0][0])
    need = math.ceil(0.80 * max_conn)
    snapshot(db)
    check("connection_saturation: no alert at idle", not alerts(db, "connection_saturation"))
    held = [connect("shop_app", dbname=db, application_name=f"live_conn_{i}")
            for i in range(need)]
    say("connections_open", count=len(held), max_connections=max_conn)
    try:
        settle(1.0)
        snap = snapshot(db)
        a = alerts(db, "connection_saturation")
        check("connection_saturation: opens at >= 80 % of max_connections",
              len(a) == 1 and a[0]["resolved_at"] is None
              and a[0]["fired_snapshot"] == snap["snapshot_id"], alerts=a)
    finally:
        for c in held:
            c.close()
    say("connections_closed", count=len(held))
    settle(1.5)
    snap = snapshot(db)
    a = alerts(db, "connection_saturation")
    check("connection_saturation: resolves after the connections close",
          len(a) == 1 and a[0]["resolved_at"] is not None
          and a[0]["resolved_snapshot"] == snap["snapshot_id"], alerts=a)


def restart_cluster() -> None:
    env = load_env()
    say("cluster_restart", reason="empty shared buffers so the next scan reads from disk")
    subprocess.run([str(PGBIN / "pg_ctl.exe"), "-D", str(ROOT / ".pgdata"), "-m", "fast", "-w",
                    "-l", str(ROOT / ".pgdata" / "server.log"), "restart"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)
    port = env.get("PGPORT", "55432")
    for _ in range(60):
        ready = subprocess.run([str(PGBIN / "pg_isready.exe"), "-h", "127.0.0.1", "-p", port],
                               capture_output=True)
        if ready.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("cluster did not come back after restart")


def cache_hit_ratio(db: str) -> None:
    say("scenario_start", rule="cache_hit_ratio")
    sql(db, "CREATE SCHEMA IF NOT EXISTS scratch")
    sql(db, "DROP TABLE IF EXISTS scratch.cache_probe")
    sql(db, "CREATE TABLE scratch.cache_probe AS "
            "SELECT g AS id, md5(g::text) || md5((g * 7)::text) AS pad "
            "FROM generate_series(1, 1500000) g")
    sql(db, "CHECKPOINT")
    mb = sql(db, "SELECT pg_total_relation_size('scratch.cache_probe') / 1048576")[0][0]
    say("scratch_table_ready", megabytes=int(mb))
    try:
        snapshot(db)  # baseline: first sample has NULL deltas and is excluded
        check("cache_hit_ratio: no alert at baseline", not alerts(db, "cache_hit_ratio"))
        restart_cluster()
        sql(db, "SELECT count(*), max(pad) FROM scratch.cache_probe")  # cold: reads from disk
        settle()
        snap = snapshot(db)
        a = alerts(db, "cache_hit_ratio")
        check("cache_hit_ratio: opens after a cold-cache scan",
              len(a) == 1 and a[0]["resolved_at"] is None
              and a[0]["fired_snapshot"] == snap["snapshot_id"], alerts=a)
        for attempt in range(1, 4):
            say("recovery_load", profile="read", seconds=20, attempt=attempt)
            subprocess.run(["bash", str(ROOT / "workload" / "run_pgbench.sh"), "--profile", "read",
                            "--clients", "4", "--seconds", "20", "--database", db],
                           check=True, stdout=subprocess.DEVNULL)
            settle()
            snap = snapshot(db)
            a = alerts(db, "cache_hit_ratio")
            if a and a[0]["resolved_at"] is not None:
                break
        check("cache_hit_ratio: resolves once cached reads dominate the 5-minute window",
              len(a) == 1 and a[0]["resolved_at"] is not None
              and a[0]["resolved_snapshot"] == snap["snapshot_id"], alerts=a)
    finally:
        sql(db, "DROP SCHEMA IF EXISTS scratch CASCADE")
        say("scratch_table_dropped")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", required=True,
                    help="disposable database built with scripts/setup_db.sh (not shopdb)")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="reuse the database instead of rebuilding it for each scenario")
    ap.add_argument("--only", nargs="+", choices=["dead_tuple_risk", "connection_saturation",
                                                  "cache_hit_ratio"])
    args = ap.parse_args()
    if args.database == "shopdb":
        print("refusing to run against the demo database 'shopdb'", file=sys.stderr)
        return 2
    if not args.no_rebuild:
        fresh_database(args.database)
    say("run_start", database=args.database,
        server=sql(args.database, "SELECT version()")[0][0])
    scenarios = {"dead_tuple_risk": dead_tuple_risk,
                 "connection_saturation": connection_saturation,
                 "cache_hit_ratio": cache_hit_ratio}
    for i, name in enumerate(args.only or list(scenarios)):
        if i and not args.no_rebuild:
            fresh_database(args.database)
        scenarios[name](args.database)
    say("run_end", failures=FAILURES, result="FAIL" if FAILURES else "PASS")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
