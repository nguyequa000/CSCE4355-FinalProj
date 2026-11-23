#!/usr/bin/env python3
"""Automated test runner: one command, every suite, a pass/fail table.

    python tests/run_tests.py             # run all suites against the existing test DBs
    python tests/run_tests.py --rebuild   # drop/recreate each test DB first (clean run)
    python tests/run_tests.py --only alerts more_rules
    python tests/run_tests.py --list

Each suite is paired with the test database it needs and the behaviour it proves
(for example the live lock-contention test must open a blocked_session alert and
resolve it after the demo releases the lock).  The runner:

  1. checks the cluster is reachable (scripts/start_cluster.sh starts it),
  2. optionally rebuilds the test databases with scripts/setup_db.sh,
  3. runs pytest once per suite with ambient PG* variables removed (stale shell
     credentials otherwise override .env and cause authentication failures),
  4. prints a summary table and writes it to docs/results/test_run_<timestamp>.txt,
  5. exits 0 only if every suite passed.

Credentials stay in the git-ignored .env; nothing secret is printed or stored.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# name -> (test file, database, what it proves)
SUITES: dict[str, tuple[str, str, str]] = {
    "capture":     ("tests/test_capture.py",     "shopdb_t2",
                    "snapshots are written, lock edges survive, deltas are non-negative"),
    "privileges":  ("tests/test_privileges.py",  "shopdb_t2",
                    "reader sees views only; app cannot touch monitor schema"),
    "history":     ("tests/test_history.py",     "shopdb_t2",
                    "all history tables fill; retention purge; no password text stored"),
    "alerts":      ("tests/test_alert.py",       "shopdb_t3",
                    "lock contention opens a blocked_session alert and resolves it"),
    "extra_rules": ("tests/test_extra_rules.py", "shopdb_t5",
                    "idle_in_transaction and long_active_query open and resolve"),
    "more_rules":  ("tests/test_more_rules.py",  "shopdb_r2",
                    "connection_saturation, cache_hit_ratio, dead_tuple_risk open and resolve"),
}

PG_ENV_KEYS = ("PGPASSWORD", "PG_SUPERUSER", "PGHOST", "PGPORT", "PGDATABASE",
               "PG_SUPERUSER_PASSWORD", "SHOP_APP_PASSWORD",
               "MONITOR_COLLECTOR_PASSWORD", "MONITOR_READER_PASSWORD")


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in PG_ENV_KEYS}
    env.update(extra)
    return env


def cluster_ready() -> bool:
    from scripts.dbconf import connect
    try:
        with connect("postgres", dbname="postgres", autocommit=True) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001 - report any connection failure
        print(f"cluster not reachable: {type(exc).__name__}", file=sys.stderr)
        return False


def rebuild(databases: list[str]) -> bool:
    bash = shutil.which("bash")
    if not bash:
        print("bash not found; run from Git Bash or rebuild the databases manually",
              file=sys.stderr)
        return False
    from scripts.dbconf import connect
    for db in databases:
        print(f"rebuilding {db} ...", flush=True)
        # setup_db.sh leaves an existing database in place, so drop it first
        with connect("postgres", dbname="postgres", autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        proc = subprocess.run([bash, "scripts/setup_db.sh", db], cwd=ROOT,
                              capture_output=True, text=True, env=clean_env())
        if proc.returncode != 0:
            print(proc.stdout[-800:], proc.stderr[-800:], file=sys.stderr)
            return False
    return True


def run_suite(name: str) -> dict:
    path, db, proves = SUITES[name]
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", path, "-q", "--tb=short", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, env=clean_env(TEST_DB=db),
    )
    elapsed = time.monotonic() - start
    out = proc.stdout + proc.stderr
    counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|error|errors|skipped)", out)}
    return {
        "name": name, "path": path, "db": db, "proves": proves, "rc": proc.returncode,
        "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0),
        "seconds": elapsed, "output": out,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild", action="store_true",
                    help="recreate every needed test database before running")
    ap.add_argument("--only", nargs="+", choices=sorted(SUITES), metavar="SUITE",
                    help="run only these suites")
    ap.add_argument("--list", action="store_true", help="list suites and exit")
    ap.add_argument("--no-save", action="store_true", help="do not write docs/results/")
    args = ap.parse_args(argv)

    if args.list:
        for n, (p, db, proves) in SUITES.items():
            print(f"{n:12} {p:28} {db:10} {proves}")
        return 0

    names = args.only or list(SUITES)
    if not cluster_ready():
        print("start it with: bash scripts/start_cluster.sh", file=sys.stderr)
        return 2
    if args.rebuild and not rebuild(sorted({SUITES[n][1] for n in names})):
        return 2

    results = []
    for n in names:
        print(f"running {n} ({SUITES[n][0]} on {SUITES[n][1]}) ...", flush=True)
        r = run_suite(n)
        results.append(r)
        print(f"  {'PASS' if r['rc'] == 0 else 'FAIL'}  {r['passed']} passed, "
              f"{r['failed']} failed, {r['seconds']:.1f}s", flush=True)

    stamp = datetime.now().astimezone()
    lines = [f"Test run {stamp:%Y-%m-%d %H:%M:%S %Z}",
             f"{'suite':12} {'result':6} {'passed':>6} {'failed':>6} {'secs':>6}  proves", "-" * 90]
    for r in results:
        lines.append(f"{r['name']:12} {'PASS' if r['rc'] == 0 else 'FAIL':6} {r['passed']:>6} "
                     f"{r['failed']:>6} {r['seconds']:>6.1f}  {r['proves']}")
    total_p = sum(r["passed"] for r in results)
    total_f = sum(r["failed"] for r in results)
    ok = all(r["rc"] == 0 for r in results)
    lines += ["-" * 90, f"TOTAL: {total_p} passed, {total_f} failed -> {'PASS' if ok else 'FAIL'}"]
    summary = "\n".join(lines)
    print("\n" + summary)

    for r in results:
        if r["rc"] != 0:
            print(f"\n--- {r['name']} output ---\n{r['output'][-3000:]}")

    if not args.no_save:
        out_dir = ROOT / "docs" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"test_run_{stamp:%Y%m%d_%H%M%S}.txt"
        target.write_text(summary + "\n", encoding="utf-8")
        print(f"\nsaved {target.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
