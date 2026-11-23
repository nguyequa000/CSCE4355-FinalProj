"""Lane F tests: idle_in_transaction and long_active_query alert rules.

Real PostgreSQL database, no mocks. Set TEST_DB (default shopdb_t5) to a
disposable database built with:

    bash scripts/setup_db.sh shopdb_t5

Synthetic tests insert schema-true monitor.snapshot / monitor.session_sample
rows as postgres and call monitor.evaluate_alerts() as monitor_collector. The
live tests start a real shop_app session (workload/session_demo.py), lower the
rule threshold temporarily so the test runs in seconds, capture with
scripts/collect.py --once, and check the alert opens and resolves.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect  # noqa: E402

TEST_DB = os.environ.get("TEST_DB", "shopdb_t5")
RULES = ("idle_in_transaction", "long_active_query")
DEFAULT_THRESHOLD = 60
LIVE_THRESHOLD = 2


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
def _reset_monitor(cur) -> None:
    cur.execute(
        "TRUNCATE monitor.alert_log, monitor.lock_wait, monitor.session_sample, "
        "monitor.db_stat_sample, monitor.snapshot RESTART IDENTITY CASCADE"
    )
    for rule in RULES:
        cur.execute(
            "INSERT INTO monitor.alert_rule "
            "  (rule_name, threshold_seconds, severity, enabled, description) "
            "VALUES (%s, %s, 'warning', true, 'test row') "
            "ON CONFLICT (rule_name) DO UPDATE SET "
            "  threshold_seconds = EXCLUDED.threshold_seconds, "
            "  severity = EXCLUDED.severity, enabled = true",
            (rule, DEFAULT_THRESHOLD),
        )


@pytest.fixture()
def admin():
    conn = connect("postgres", dbname=TEST_DB)
    conn.autocommit = True
    with conn.cursor() as cur:
        _reset_monitor(cur)
    yield conn
    conn.close()


def make_snapshot(admin, captured_at: datetime | None = None, sessions=()) -> tuple[int, datetime]:
    """Insert a snapshot plus session_sample rows.

    sessions: dicts with pid, state, state_change, query_start and optional
    wait_event_type / application_name.
    """
    with admin.cursor() as cur:
        if captured_at is None:
            cur.execute(
                "INSERT INTO monitor.snapshot DEFAULT VALUES "
                "RETURNING snapshot_id, captured_at"
            )
        else:
            cur.execute(
                "INSERT INTO monitor.snapshot (captured_at) VALUES (%s) "
                "RETURNING snapshot_id, captured_at",
                (captured_at,),
            )
        sid, cap = cur.fetchone()
        for s in sessions:
            cur.execute(
                """
                INSERT INTO monitor.session_sample
                       (snapshot_id, pid, usename, application_name, state,
                        wait_event_type, wait_event, xact_start, query_start,
                        state_change, blocked_by, query_excerpt)
                VALUES (%s, %s, 'shop_app', %s, %s, %s, NULL, %s, %s, %s,
                        '{}', %s)
                """,
                (sid, s["pid"], s.get("application_name", "t5_synthetic"),
                 s["state"], s.get("wait_event_type"),
                 s.get("xact_start", s["state_change"]), s["query_start"],
                 s["state_change"], s.get("query_excerpt", "SELECT 1")),
            )
    return sid, cap


def idle_session(pid: int, first_seen: datetime, **kw) -> dict:
    return {"pid": pid, "state": "idle in transaction",
            "state_change": first_seen, "query_start": first_seen, **kw}


def active_session(pid: int, first_seen: datetime, **kw) -> dict:
    return {"pid": pid, "state": "active", "state_change": first_seen,
            "query_start": first_seen, "application_name": "t5_synthetic_long",
            **kw}


def evaluate(snapshot_id: int) -> tuple[int, int]:
    with connect("monitor_collector", dbname=TEST_DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT opened, resolved FROM monitor.evaluate_alerts(%s)",
                (snapshot_id,),
            )
            return cur.fetchone()


def alerts(admin, rule: str | None = None) -> list[dict]:
    sql = (
        "SELECT alert_id, rule_name, severity, blocked_pid, blocker_pid, "
        "       first_seen_at, eligible_at, fired_at, fired_snapshot_id, "
        "       observed_seconds, last_seen_snapshot_id, resolved_at, "
        "       resolved_snapshot_id "
        "  FROM monitor.alert_log "
    )
    params: tuple = ()
    if rule is not None:
        sql += " WHERE rule_name = %s"
        params = (rule,)
    sql += " ORDER BY alert_id"
    with admin.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def close_time(a: datetime, b: datetime, tol_seconds: float = 0.001) -> bool:
    return abs((a - b).total_seconds()) <= tol_seconds


# --------------------------------------------------------------------------- #
# synthetic rule tests
# --------------------------------------------------------------------------- #
def test_idle_below_threshold_does_not_alert(admin):
    cap = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
    sid, _ = make_snapshot(admin, cap, [
        idle_session(801, cap - timedelta(seconds=59.9)),
    ])
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_idle_at_threshold_opens_warning_alert(admin):
    cap = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
    session_start = cap - timedelta(seconds=60)
    sid, _ = make_snapshot(admin, cap, [idle_session(801, session_start)])
    assert evaluate(sid) == (1, 0)

    rows = alerts(admin)
    assert len(rows) == 1
    a = rows[0]
    assert a["rule_name"] == "idle_in_transaction"
    assert a["severity"] == "warning"
    assert a["blocked_pid"] == 801
    assert a["blocker_pid"] == 801          # documented self-mapping
    assert close_time(a["first_seen_at"], session_start)
    assert close_time(a["eligible_at"], cap)
    assert close_time(a["fired_at"], cap)
    assert a["fired_snapshot_id"] == sid
    assert a["observed_seconds"] == 60.0
    assert a["resolved_at"] is None


def test_long_active_query_below_threshold_does_not_alert(admin):
    cap = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
    sid, _ = make_snapshot(admin, cap, [
        active_session(901, cap - timedelta(seconds=59.5)),
    ])
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_long_active_query_at_threshold_opens_and_ignores_lock_waits(admin):
    cap = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
    query_start = cap - timedelta(seconds=60)
    sid, _ = make_snapshot(admin, cap, [
        active_session(901, query_start),
        # A session blocked on a lock for 300 s is blocked_session's job; the
        # long_active_query rule must not count it twice.
        active_session(902, cap - timedelta(seconds=300),
                       wait_event_type="Lock", wait_event="transactionid",
                       query_excerpt="UPDATE shop.customers ..."),
    ])
    assert evaluate(sid) == (1, 0)

    rows = alerts(admin)
    assert len(rows) == 1
    a = rows[0]
    assert a["rule_name"] == "long_active_query"
    assert a["severity"] == "warning"
    assert a["blocked_pid"] == 901
    assert a["blocker_pid"] == 901
    assert close_time(a["first_seen_at"], query_start)
    assert close_time(a["eligible_at"], cap)
    assert a["observed_seconds"] == 60.0
    assert a["resolved_at"] is None


def test_idle_persists_single_open_alert_and_updates_observed(admin):
    cap = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
    session_start = cap - timedelta(seconds=60)
    sid1, _ = make_snapshot(admin, cap, [idle_session(801, session_start)])
    assert evaluate(sid1) == (1, 0)

    cap2 = cap + timedelta(seconds=30)
    sid2, _ = make_snapshot(admin, cap2, [idle_session(801, session_start)])
    assert evaluate(sid2) == (0, 0)

    rows = alerts(admin)
    assert len(rows) == 1                       # no duplicate open alert
    a = rows[0]
    assert a["resolved_at"] is None
    assert a["observed_seconds"] == 90.0
    assert a["last_seen_snapshot_id"] == sid2
    assert a["fired_snapshot_id"] == sid1

    # Idempotent replay of the same snapshot.
    assert evaluate(sid2) == (0, 0)
    assert len(alerts(admin)) == 1


def test_long_active_query_persists_without_duplicate(admin):
    cap = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
    query_start = cap - timedelta(seconds=75)
    sid1, _ = make_snapshot(admin, cap, [active_session(901, query_start)])
    assert evaluate(sid1) == (1, 0)

    cap2 = cap + timedelta(seconds=30)
    sid2, _ = make_snapshot(admin, cap2, [active_session(901, query_start)])
    assert evaluate(sid2) == (0, 0)
    rows = alerts(admin)
    assert len(rows) == 1
    assert rows[0]["observed_seconds"] == 105.0
    assert rows[0]["last_seen_snapshot_id"] == sid2


def test_both_rules_resolve_on_first_clean_snapshot(admin):
    cap = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
    sid1, _ = make_snapshot(admin, cap, [
        idle_session(801, cap - timedelta(seconds=60)),
        active_session(901, cap - timedelta(seconds=60)),
    ])
    assert evaluate(sid1) == (2, 0)

    cap2 = cap + timedelta(seconds=30)
    sid2, _ = make_snapshot(admin, cap2, [
        # Same PIDs, but both conditions are gone: transaction committed, and
        # the session is idle (not active).
        {"pid": 801, "state": "idle", "state_change": cap2 - timedelta(seconds=1),
         "query_start": cap2 - timedelta(seconds=1)},
        {"pid": 901, "state": "idle", "state_change": cap2 - timedelta(seconds=1),
         "query_start": cap2 - timedelta(seconds=1)},
    ])
    assert evaluate(sid2) == (0, 2)

    rows = alerts(admin)
    assert len(rows) == 2
    for a in rows:
        assert close_time(a["resolved_at"], cap2)
        assert a["resolved_snapshot_id"] == sid2

    # Resolving is not repeated on the next clean snapshot.
    sid3, _ = make_snapshot(admin, cap2 + timedelta(seconds=30))
    assert evaluate(sid3) == (0, 0)
    assert len(alerts(admin)) == 2


def test_collector_own_backend_is_excluded(admin):
    cap = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
    with admin.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        collector_pid = cur.fetchone()[0]
    # The admin connection played the collector when it inserted the snapshot;
    # an old idle-in-transaction row for that PID must not alert.
    sid, _ = make_snapshot(admin, cap, [
        idle_session(collector_pid, cap - timedelta(seconds=600)),
    ])
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_new_episode_same_pid_reopens_alert(admin):
    cap = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
    sid1, _ = make_snapshot(admin, cap, [
        idle_session(801, cap - timedelta(seconds=60)),
    ])
    assert evaluate(sid1) == (1, 0)

    cap2 = cap + timedelta(seconds=120)
    # Same PID, new transaction: state_change is after the previous sample and
    # already 65 s old at this snapshot, so it is a fresh qualifying episode.
    sid2, _ = make_snapshot(admin, cap2, [
        idle_session(801, cap2 - timedelta(seconds=65)),
    ])
    assert evaluate(sid2) == (1, 1)
    rows = alerts(admin)
    assert len(rows) == 2
    assert rows[0]["resolved_at"] is not None
    assert rows[1]["resolved_at"] is None
    assert close_time(rows[1]["fired_at"], cap2)


# --------------------------------------------------------------------------- #
# live tests: real shop_app session, real collector snapshot
# --------------------------------------------------------------------------- #
def _run_collect_once() -> str:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "collect.py"),
         "--once", "--db", TEST_DB],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _start_demo(mode: str, hold: float, log_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(ROOT / "workload" / "session_demo.py"),
         "--mode", mode, "--hold", str(hold), "--database", TEST_DB,
         "--log", str(log_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(ROOT), env=os.environ.copy(),
    )


def _wait_for_session(admin, app_name: str, state: str,
                      timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    with admin.cursor() as cur:
        while time.monotonic() < deadline:
            cur.execute(
                "SELECT pid FROM pg_stat_activity "
                " WHERE application_name = %s AND state = %s AND datname = %s",
                (app_name, state, TEST_DB),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            time.sleep(0.05)
    raise AssertionError(f"{app_name} never reached state {state!r}")


def _set_threshold(admin, rule: str, seconds: int) -> None:
    with admin.cursor() as cur:
        cur.execute(
            "UPDATE monitor.alert_rule SET threshold_seconds = %s WHERE rule_name = %s",
            (seconds, rule),
        )


@pytest.mark.parametrize(
    ("mode", "rule", "state", "app_name"),
    [
        ("idle", "idle_in_transaction", "idle in transaction", "session_demo_idle"),
        ("long", "long_active_query", "active", "session_demo_long"),
    ],
)
def test_live_session_opens_then_resolves(admin, tmp_path, mode, rule, state, app_name):
    """Two real backends, two rules: collector --once fires and later resolves."""
    demo_log = tmp_path / f"session_demo_{mode}.jsonl"
    proc = _start_demo(mode, hold=8.0, log_path=demo_log)
    try:
        _set_threshold(admin, rule, LIVE_THRESHOLD)
        pid = _wait_for_session(admin, app_name, state)
        time.sleep(LIVE_THRESHOLD + 0.6)          # age the session past threshold

        _run_collect_once()
        rows = alerts(admin, rule)
        assert len(rows) == 1, rows
        a = rows[0]
        assert a["blocked_pid"] == pid
        assert a["blocker_pid"] == pid
        assert a["resolved_at"] is None
        assert a["observed_seconds"] >= LIVE_THRESHOLD
        assert a["first_seen_at"] < a["eligible_at"] <= a["fired_at"]

        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, f"{out}\n{err}"
        assert demo_log.exists()
        event = json.loads(demo_log.read_text(encoding="utf-8").splitlines()[-1])
        assert event["pid"] == pid and event["ok"] is True

        _run_collect_once()                       # session is gone now
        rows = alerts(admin, rule)
        assert len(rows) == 1
        assert rows[0]["resolved_at"] is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)
        with admin.cursor() as cur:
            cur.execute(
                "UPDATE monitor.alert_rule SET threshold_seconds = %s WHERE rule_name = %s",
                (DEFAULT_THRESHOLD, rule),
            )
