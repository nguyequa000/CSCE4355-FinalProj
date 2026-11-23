"""T3 tests: blocked-session alert rule, live lock contention, console notifier.

These tests run against a real PostgreSQL database (no DB mocks). Set TEST_DB
(default shopdb_t3) to a disposable database created with:
    bash scripts/setup_db.sh shopdb_t3

Snapshots/lock_wait rows are inserted directly as postgres for the rule unit
tests (schema-true synthetic rows). The live test creates real row-lock
contention between two shop_app connections and captures it with the same
pg_stat_activity/pg_blocking_pids SQL shape the collector uses.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect  # noqa: E402

TEST_DB = os.environ.get("TEST_DB", "shopdb_t3")
THRESHOLD = 30
CUSTOMER_ID = 42


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
def _truncate_monitor(cur) -> None:
    cur.execute(
        "TRUNCATE monitor.alert_log, monitor.lock_wait, monitor.session_sample, "
        "monitor.db_stat_sample, monitor.snapshot RESTART IDENTITY CASCADE"
    )
    cur.execute(
        "INSERT INTO monitor.alert_rule (rule_name, threshold_seconds, severity, enabled, description) "
        "VALUES ('blocked_session', %s, 'critical', true, 'test row') "
        "ON CONFLICT (rule_name) DO UPDATE SET threshold_seconds = EXCLUDED.threshold_seconds, "
        "severity = EXCLUDED.severity, enabled = true",
        (THRESHOLD,),
    )


@pytest.fixture()
def admin():
    conn = connect("postgres", dbname=TEST_DB)
    conn.autocommit = True
    with conn.cursor() as cur:
        _truncate_monitor(cur)
    yield conn
    conn.close()


def make_snapshot(admin, captured_at: datetime | None = None, waits=()) -> tuple[int, datetime]:
    """Insert a monitor.snapshot plus lock_wait edges. waits: dicts with
    blocked, blocker, waited and optional wait_started."""
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
        for w in waits:
            wait_started = w.get("wait_started", cap - timedelta(seconds=float(w["waited"])))
            cur.execute(
                """
                INSERT INTO monitor.lock_wait
                       (snapshot_id, blocked_pid, blocker_pid, blocked_user, blocker_user,
                        blocked_app, blocker_app, blocker_state, wait_started, waited_seconds)
                VALUES (%s, %s, %s, 'shop_app', 'shop_app', 'waiter', 'blocker',
                        'idle in transaction', %s, %s)
                """,
                (sid, w["blocked"], w["blocker"], wait_started, w["waited"]),
            )
    return sid, cap


def evaluate(snapshot_id: int) -> tuple[int, int]:
    with connect("monitor_collector", dbname=TEST_DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT opened, resolved FROM monitor.evaluate_alerts(%s)",
                (snapshot_id,),
            )
            return cur.fetchone()


def alerts(admin) -> list[dict]:
    with admin.cursor() as cur:
        cur.execute(
            "SELECT alert_id, rule_name, severity, blocked_pid, blocker_pid, "
            "       first_seen_at, eligible_at, fired_at, fired_snapshot_id, "
            "       observed_seconds, last_seen_snapshot_id, resolved_at, "
            "       resolved_snapshot_id, notified_at, resolve_notified_at "
            "  FROM monitor.alert_log ORDER BY alert_id"
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def close_time(a: datetime, b: datetime, tol_seconds: float = 0.001) -> bool:
    return abs((a - b).total_seconds()) <= tol_seconds


# --------------------------------------------------------------------------- #
# evaluate_alerts unit tests (synthetic, schema-true snapshots)
# --------------------------------------------------------------------------- #
def test_below_threshold_does_not_alert(admin):
    sid, cap = make_snapshot(admin, waits=[{"blocked": 501, "blocker": 701, "waited": 29.9}])
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_at_threshold_opens_alert_with_correct_times(admin):
    cap = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    sid, _ = make_snapshot(
        admin, captured_at=cap,
        waits=[{"blocked": 501, "blocker": 701, "waited": 30.0,
                "wait_started": cap - timedelta(seconds=30)}],
    )
    assert evaluate(sid) == (1, 0)
    rows = alerts(admin)
    assert len(rows) == 1
    a = rows[0]
    assert a["rule_name"] == "blocked_session"
    assert a["severity"] == "critical"
    assert a["blocked_pid"] == 501
    assert a["blocker_pid"] == 701
    assert close_time(a["first_seen_at"], cap - timedelta(seconds=30))
    assert close_time(a["eligible_at"], cap)
    assert close_time(a["fired_at"], cap)
    assert a["fired_snapshot_id"] == sid
    assert a["observed_seconds"] == 30.0
    assert a["resolved_at"] is None


def test_two_blockers_picks_lowest_blocker_pid(admin):
    cap = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    sid, _ = make_snapshot(
        admin, captured_at=cap,
        waits=[
            {"blocked": 502, "blocker": 900, "waited": 45.0,
             "wait_started": cap - timedelta(seconds=45)},
            {"blocked": 502, "blocker": 800, "waited": 31.0,
             "wait_started": cap - timedelta(seconds=31)},
        ],
    )
    assert evaluate(sid) == (1, 0)
    a = alerts(admin)[0]
    assert a["blocked_pid"] == 502
    assert a["blocker_pid"] == 800          # lowest blocker pid
    assert a["observed_seconds"] == 45.0    # longest observed wait
    assert close_time(a["first_seen_at"], cap - timedelta(seconds=45))
    assert close_time(a["eligible_at"], cap - timedelta(seconds=15))


def test_repeat_snapshot_keeps_single_open_alert_and_updates_observed(admin):
    cap = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    sid1, _ = make_snapshot(
        admin, captured_at=cap,
        waits=[{"blocked": 501, "blocker": 701, "waited": 30.0,
                "wait_started": cap - timedelta(seconds=30)}],
    )
    assert evaluate(sid1) == (1, 0)

    cap2 = cap + timedelta(seconds=60)
    sid2, _ = make_snapshot(
        admin, captured_at=cap2,
        waits=[{"blocked": 501, "blocker": 701, "waited": 90.0,
                "wait_started": cap - timedelta(seconds=30)}],
    )
    assert evaluate(sid2) == (0, 0)

    rows = alerts(admin)
    assert len(rows) == 1                     # no duplicate open alert
    a = rows[0]
    assert a["resolved_at"] is None
    assert a["observed_seconds"] == 90.0
    assert a["last_seen_snapshot_id"] == sid2
    assert a["fired_snapshot_id"] == sid1
    assert close_time(a["fired_at"], cap)

    # Idempotent: replaying the same snapshot changes nothing and opens nothing.
    assert evaluate(sid2) == (0, 0)
    assert len(alerts(admin)) == 1


def test_clean_snapshot_resolves_open_alert_once(admin):
    cap = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    sid1, _ = make_snapshot(
        admin, captured_at=cap,
        waits=[{"blocked": 501, "blocker": 701, "waited": 60.0,
                "wait_started": cap - timedelta(seconds=60)}],
    )
    assert evaluate(sid1) == (1, 0)

    cap2 = cap + timedelta(seconds=60)
    sid2, _ = make_snapshot(admin, captured_at=cap2)   # no lock_wait rows
    assert evaluate(sid2) == (0, 1)

    a = alerts(admin)[0]
    assert close_time(a["resolved_at"], cap2)
    assert a["resolved_snapshot_id"] == sid2

    # A second clean snapshot must not resolve it again.
    sid3, _ = make_snapshot(admin, captured_at=cap2 + timedelta(seconds=60))
    assert evaluate(sid3) == (0, 0)
    assert len(alerts(admin)) == 1


def test_disabled_rule_does_nothing(admin):
    cap = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    with admin.cursor() as cur:
        cur.execute("UPDATE monitor.alert_rule SET enabled = false WHERE rule_name = 'blocked_session'")
    sid, _ = make_snapshot(
        admin, captured_at=cap,
        waits=[{"blocked": 503, "blocker": 703, "waited": 300.0,
                "wait_started": cap - timedelta(seconds=300)}],
    )
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_pid_reuse_after_previous_observation_starts_new_alert(admin):
    cap = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    sid1, _ = make_snapshot(
        admin, captured_at=cap,
        waits=[{"blocked": 504, "blocker": 704, "waited": 60.0,
                "wait_started": cap - timedelta(seconds=60)}],
    )
    assert evaluate(sid1) == (1, 0)

    cap2 = cap + timedelta(seconds=60)
    # Same blocked PID, but this wait began 30 s after the previous sample:
    # PID reuse or a new wait episode, so the old alert is closed and a new one
    # is opened at this snapshot.
    sid2, _ = make_snapshot(
        admin, captured_at=cap2,
        waits=[{"blocked": 504, "blocker": 704, "waited": 30.0,
                "wait_started": cap2 - timedelta(seconds=30)}],
    )
    assert evaluate(sid2) == (1, 1)
    rows = alerts(admin)
    assert len(rows) == 2
    assert rows[0]["resolved_at"] is not None
    assert rows[1]["resolved_at"] is None
    assert close_time(rows[1]["fired_at"], cap2)


# --------------------------------------------------------------------------- #
# live test: real lock contention between two shop_app backends
# --------------------------------------------------------------------------- #
def _wait_until_blocked(monitor_conn, waiter_pid: int, blocker_pid: int,
                        timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    with monitor_conn.cursor() as cur:
        while time.monotonic() < deadline:
            cur.execute(
                """
                SELECT 1
                  FROM pg_stat_activity a
                  JOIN LATERAL unnest(pg_blocking_pids(a.pid)) AS b(blocker_pid)
                       ON true
                 WHERE a.pid = %s AND b.blocker_pid = %s
                   AND a.wait_event_type = 'Lock'
                """,
                (waiter_pid, blocker_pid),
            )
            if cur.fetchone():
                return True
            time.sleep(0.05)
    return False


def _capture_lock_snapshot(monitor_conn, waiter_pid: int, blocker_pid: int) -> tuple[int, datetime]:
    """Same shape as the collector's capture: snapshot row + lock_wait edges
    computed from pg_stat_activity / pg_blocking_pids for this database."""
    with monitor_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO monitor.snapshot DEFAULT VALUES RETURNING snapshot_id, captured_at"
        )
        sid, cap = cur.fetchone()
        cur.execute(
            """
            INSERT INTO monitor.lock_wait
                   (snapshot_id, blocked_pid, blocker_pid, blocked_user, blocker_user,
                    blocked_app, blocker_app, blocker_state, wait_started, waited_seconds)
            SELECT %s, w.pid, k.pid, w.usename, k.usename,
                   w.application_name, k.application_name, k.state, w.state_change,
                   round(extract(epoch FROM (%s - w.state_change))::numeric, 1)
              FROM pg_stat_activity w
              JOIN pg_stat_activity k ON k.pid = ANY (pg_blocking_pids(w.pid))
             WHERE w.pid = %s AND k.pid = %s
               AND w.datname = current_database()
            """,
            (sid, cap, waiter_pid, blocker_pid),
        )
    monitor_conn.commit()
    return sid, cap


def test_live_lock_contention_opens_then_resolves(admin):
    blocker = connect("shop_app", dbname=TEST_DB, application_name="t3_live_blocker")
    blocker.autocommit = True
    monitor = connect("monitor_collector", dbname=TEST_DB, application_name="t3_live_capture")
    waiter_conn = None
    waiter_pid: int | None = None
    waiter_state: dict[str, object] = {}
    thread: threading.Thread | None = None

    def waiter_body() -> None:
        nonlocal waiter_conn, waiter_pid
        try:
            waiter_conn = connect("shop_app", dbname=TEST_DB, application_name="t3_live_waiter")
            waiter_conn.autocommit = True
            waiter_pid = waiter_conn.info.backend_pid
            with waiter_conn.cursor() as cur:
                cur.execute("SET lock_timeout = '20s'")
                cur.execute("BEGIN")
                cur.execute(
                    "UPDATE shop.customers SET balance = balance + 1 WHERE customer_id = %s",
                    (CUSTOMER_ID,),
                )
            waiter_state["finished"] = True
            waiter_conn.rollback()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            waiter_state["error"] = f"{type(exc).__name__}: {exc}"
            try:
                if waiter_conn is not None:
                    waiter_conn.rollback()
            except Exception:
                pass

    try:
        # A live wait starts from zero, so lower the threshold for this test and
        # let the waiter age past it before sampling (the rule itself is
        # threshold-agnostic).
        with admin.cursor() as cur:
            cur.execute(
                "UPDATE monitor.alert_rule SET threshold_seconds = 1 "
                "WHERE rule_name = 'blocked_session'"
            )
        with blocker.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(
                "UPDATE shop.customers SET balance = balance + 1 WHERE customer_id = %s",
                (CUSTOMER_ID,),
            )
        blocker_pid = blocker.info.backend_pid

        thread = threading.Thread(target=waiter_body, name="t3_live_waiter")
        thread.start()
        deadline = time.monotonic() + 10
        while waiter_pid is None and "error" not in waiter_state and time.monotonic() < deadline:
            time.sleep(0.05)
        assert waiter_pid is not None, f"waiter did not connect: {waiter_state}"

        assert _wait_until_blocked(monitor, waiter_pid, blocker_pid), \
            "waiter never showed wait_event_type=Lock behind the blocker"
        time.sleep(1.2)   # observe a wait older than the 1 s test threshold

        sid_open, cap_open = _capture_lock_snapshot(monitor, waiter_pid, blocker_pid)
        opened, resolved = evaluate(sid_open)
        assert (opened, resolved) == (1, 0)
        a = alerts(admin)[0]
        assert a["blocked_pid"] == waiter_pid
        assert a["blocker_pid"] == blocker_pid
        assert a["observed_seconds"] >= 0.0
        assert a["resolved_at"] is None
        assert a["fired_snapshot_id"] == sid_open

        # Real recovery: release the blocker, let the waiter finish.
        blocker.rollback()
        if thread is not None:
            thread.join(timeout=25)
        assert not thread.is_alive(), "waiter did not finish after blocker release"
        assert "error" not in waiter_state, waiter_state

        sid_clean, cap_clean = _capture_lock_snapshot(monitor, waiter_pid, blocker_pid)
        with monitor.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM monitor.lock_wait WHERE snapshot_id = %s",
                (sid_clean,),
            )
            assert cur.fetchone()[0] == 0   # waiter no longer blocked
        monitor.commit()
        assert evaluate(sid_clean) == (0, 1)
        a = alerts(admin)[0]
        assert close_time(a["resolved_at"], cap_clean)
        assert a["resolved_snapshot_id"] == sid_clean
    finally:
        try:
            blocker.rollback()
        except Exception:
            pass
        blocker.close()
        monitor.close()
        if thread is not None:
            thread.join(timeout=25)
        if waiter_conn is not None:
            try:
                waiter_conn.rollback()
                waiter_conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# console notifier
# --------------------------------------------------------------------------- #
def _run_notifier(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["TEST_DB"] = TEST_DB
    return subprocess.run(
        [sys.executable, str(ROOT / "notifier" / "notify.py"), "--database", TEST_DB, *args],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(ROOT),
    )


def test_notifier_prints_each_alert_once(admin):
    cap = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    sid, _ = make_snapshot(
        admin, captured_at=cap,
        waits=[{"blocked": 505, "blocker": 705, "waited": 60.0,
                "wait_started": cap - timedelta(seconds=60)}],
    )
    assert evaluate(sid) == (1, 0)
    cap2 = cap + timedelta(seconds=60)
    sid2, _ = make_snapshot(admin, captured_at=cap2)
    assert evaluate(sid2) == (0, 1)

    first = _run_notifier("--once")
    assert first.returncode == 0, first.stderr
    lines = [ln for ln in first.stdout.splitlines() if ln.startswith("[ALERT")]
    assert len(lines) == 2, first.stdout
    fired = [ln for ln in lines if ln.startswith("[ALERT FIRED]")]
    resolved = [ln for ln in lines if ln.startswith("[ALERT RESOLVED]")]
    assert len(fired) == 1 and len(resolved) == 1
    assert re.search(
        r"^\[ALERT FIRED\] id=\d+ rule=blocked_session severity=critical "
        r"blocked_pid=505 blocker_pid=705 fired_at=\S+ observed=60\.0s$",
        fired[0],
    ), fired[0]
    assert re.search(r"^\[ALERT RESOLVED\] id=\d+ resolved_at=\S+$", resolved[0]), resolved[0]

    # Second run: nothing new is printed for either state.
    second = _run_notifier("--once")
    assert second.returncode == 0, second.stderr
    assert "[ALERT FIRED]" not in second.stdout
    assert "[ALERT RESOLVED]" not in second.stdout

    with admin.cursor() as cur:
        cur.execute("SELECT notified_at, resolve_notified_at FROM monitor.alert_log")
        notified_at, resolve_notified_at = cur.fetchone()
    assert notified_at is not None
    assert resolve_notified_at is not None
