"""Lane R2 tests: connection_saturation, cache_hit_ratio, dead_tuple_risk.

Real PostgreSQL database, no mocks. Set TEST_DB (default shopdb_r2) to a
disposable database built with:

    bash scripts/setup_db.sh shopdb_r2

Synthetic tests insert schema-true monitor.snapshot / monitor.db_stat_sample /
monitor.table_stat_sample rows as postgres and call
monitor.evaluate_more_rules() as monitor_collector. Thresholds are set per test;
the numbers below assume the server default max_connections (the connection
test derives its values from current_setting('max_connections') instead).
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect  # noqa: E402

TEST_DB = os.environ.get("TEST_DB", "shopdb_r2")
RULES = ("connection_saturation", "cache_hit_ratio", "dead_tuple_risk")
DEFAULTS = {"connection_saturation": 0.80, "cache_hit_ratio": 0.90,
            "dead_tuple_risk": 0.20}
CAP = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def admin():
    if TEST_DB == "shopdb":
        pytest.exit("refusing to run destructive tests against shopdb", 2)
    conn = connect("postgres", dbname=TEST_DB)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE monitor.alert_log, monitor.table_stat_sample, "
            "monitor.db_stat_sample, monitor.lock_wait, monitor.session_sample, "
            "monitor.snapshot RESTART IDENTITY CASCADE"
        )
        for rule, value in DEFAULTS.items():
            cur.execute(
                "INSERT INTO monitor.alert_rule "
                "  (rule_name, threshold_seconds, severity, enabled, description, "
                "   threshold_value) "
                "VALUES (%s, 0, 'warning', true, 'test row', %s) "
                "ON CONFLICT (rule_name) DO UPDATE SET "
                "  threshold_seconds = 0, severity = 'warning', enabled = true, "
                "  threshold_value = EXCLUDED.threshold_value",
                (rule, value),
            )
    yield conn
    conn.close()


def max_connections(admin) -> int:
    with admin.cursor() as cur:
        cur.execute("SELECT current_setting('max_connections')::int")
        return cur.fetchone()[0]


def set_threshold(admin, rule: str, value: float) -> None:
    with admin.cursor() as cur:
        cur.execute(
            "UPDATE monitor.alert_rule SET threshold_value = %s WHERE rule_name = %s",
            (value, rule),
        )


def make_snapshot(admin, captured_at: datetime | None = None,
                  db_stats=(), tables=()) -> tuple[int, datetime]:
    """Insert one snapshot plus optional db_stat_sample / table_stat_sample rows."""
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

        for d in db_stats:
            cur.execute(
                "INSERT INTO monitor.db_stat_sample "
                "  (snapshot_id, datname, numbackends, blks_read, blks_hit, "
                "   blks_read_delta, blks_hit_delta) "
                "VALUES (%s, %s, %s, 0, 0, %s, %s)",
                (sid, d.get("datname", TEST_DB), d.get("numbackends"),
                 d.get("blks_read_delta"), d.get("blks_hit_delta")),
            )

        for t in tables:
            cur.execute(
                "INSERT INTO monitor.table_stat_sample "
                "  (snapshot_id, relid, schemaname, relname, n_live_tup, "
                "   n_dead_tup, seq_scan, idx_scan, n_tup_ins, n_tup_upd, "
                "   n_tup_del) "
                "VALUES (%s, %s, 'shop', %s, %s, %s, 0, 0, 0, 0, 0)",
                (sid, t["relid"], t.get("relname", f"t_{t['relid']}"),
                 t.get("n_live_tup"), t.get("n_dead_tup")),
            )
    return sid, cap


def evaluate(snapshot_id: int) -> tuple[int, int]:
    with connect("monitor_collector", dbname=TEST_DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT opened, resolved FROM monitor.evaluate_more_rules(%s)",
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
        sql += "WHERE rule_name = %s "
        params = (rule,)
    sql += "ORDER BY alert_id"
    with admin.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def close_time(a: datetime, b: datetime, tol_seconds: float = 0.001) -> bool:
    return abs((a - b).total_seconds()) <= tol_seconds


# --------------------------------------------------------------------------- #
# schema contract
# --------------------------------------------------------------------------- #
def test_table_stat_sample_contract_and_rule_defaults(admin):
    """22_more_rules.sql creates the contract table and the ratio rules."""
    with admin.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            " WHERE table_schema = 'monitor' AND table_name = 'table_stat_sample' "
            " ORDER BY ordinal_position"
        )
        cols = cur.fetchall()
        cur.execute(
            "SELECT rule_name, threshold_value FROM monitor.alert_rule "
            " WHERE rule_name = ANY(%s) ORDER BY rule_name",
            (list(RULES),),
        )
        rules = {name: float(value) for name, value in cur.fetchall()}
    assert [c[0] for c in cols] == [
        "snapshot_id", "relid", "schemaname", "relname", "n_live_tup",
        "n_dead_tup", "seq_scan", "idx_scan", "n_tup_ins", "n_tup_upd",
        "n_tup_del", "last_autovacuum", "last_autoanalyze",
    ]
    assert rules == DEFAULTS


# --------------------------------------------------------------------------- #
# connection_saturation
# --------------------------------------------------------------------------- #
def test_connection_saturation_below_threshold_no_alert(admin):
    max_conn = max_connections(admin)
    set_threshold(admin, "connection_saturation", 0.50)
    sid, _ = make_snapshot(admin, CAP, db_stats=[
        {"numbackends": math.floor(max_conn * 0.49)},
    ])
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_connection_saturation_at_threshold_opens_warning(admin):
    max_conn = max_connections(admin)
    set_threshold(admin, "connection_saturation", 0.50)
    sid, cap = make_snapshot(admin, CAP, db_stats=[
        {"numbackends": math.floor(max_conn * 0.50)},
    ])
    assert evaluate(sid) == (1, 0)

    rows = alerts(admin)
    assert len(rows) == 1
    a = rows[0]
    assert a["rule_name"] == "connection_saturation"
    assert a["severity"] == "warning"
    assert a["blocked_pid"] == 0 and a["blocker_pid"] == 0
    assert close_time(a["first_seen_at"], cap)
    assert close_time(a["eligible_at"], cap)
    assert close_time(a["fired_at"], cap)
    assert a["fired_snapshot_id"] == sid
    assert float(a["observed_seconds"]) == 0.0
    assert a["resolved_at"] is None


def test_connection_saturation_persists_without_duplicate_then_resolves(admin):
    max_conn = max_connections(admin)
    high = math.floor(max_conn * 0.50)
    low = math.floor(max_conn * 0.10)
    set_threshold(admin, "connection_saturation", 0.50)

    sid1, _ = make_snapshot(admin, CAP, db_stats=[{"numbackends": high}])
    assert evaluate(sid1) == (1, 0)

    cap2 = CAP + timedelta(seconds=30)
    sid2, _ = make_snapshot(admin, cap2, db_stats=[{"numbackends": high}])
    assert evaluate(sid2) == (0, 0)
    assert evaluate(sid2) == (0, 0)              # idempotent replay
    rows = alerts(admin)
    assert len(rows) == 1
    assert rows[0]["resolved_at"] is None
    assert rows[0]["last_seen_snapshot_id"] == sid2
    assert rows[0]["fired_snapshot_id"] == sid1
    assert float(rows[0]["observed_seconds"]) == 30.0

    cap3 = cap2 + timedelta(seconds=30)
    sid3, _ = make_snapshot(admin, cap3, db_stats=[{"numbackends": low}])
    assert evaluate(sid3) == (0, 1)
    rows = alerts(admin)
    assert len(rows) == 1
    assert close_time(rows[0]["resolved_at"], cap3)
    assert rows[0]["resolved_snapshot_id"] == sid3

    sid4, _ = make_snapshot(admin, cap3 + timedelta(seconds=30),
                            db_stats=[{"numbackends": low}])
    assert evaluate(sid4) == (0, 0)              # resolve happens once


# --------------------------------------------------------------------------- #
# cache_hit_ratio
# --------------------------------------------------------------------------- #
def test_cache_hit_ratio_below_threshold_no_alert(admin):
    sid, _ = make_snapshot(admin, CAP, db_stats=[
        {"blks_hit_delta": 95, "blks_read_delta": 5},      # 0.95 >= 0.90
    ])
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_cache_hit_ratio_exactly_at_threshold_no_alert(admin):
    sid, _ = make_snapshot(admin, CAP, db_stats=[
        {"blks_hit_delta": 90, "blks_read_delta": 10},     # exactly 0.90
    ])
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_cache_hit_ratio_insufficient_window_is_unknown(admin):
    # Ratio would be 0.667 but only 30 blocks: below the 100-block evidence bar.
    sid, _ = make_snapshot(admin, CAP, db_stats=[
        {"blks_hit_delta": 20, "blks_read_delta": 10},
    ])
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_cache_hit_ratio_opens_persists_and_resolves(admin):
    sid1, cap1 = make_snapshot(admin, CAP, db_stats=[
        {"blks_hit_delta": 89, "blks_read_delta": 11},     # 0.89 < 0.90
    ])
    assert evaluate(sid1) == (1, 0)
    rows = alerts(admin)
    assert len(rows) == 1
    a = rows[0]
    assert a["rule_name"] == "cache_hit_ratio"
    assert a["severity"] == "warning"
    assert a["blocked_pid"] == 0 and a["blocker_pid"] == 0
    assert close_time(a["first_seen_at"], cap1)
    assert close_time(a["eligible_at"], cap1)
    assert close_time(a["fired_at"], cap1)
    assert a["fired_snapshot_id"] == sid1

    cap2 = CAP + timedelta(seconds=60)
    sid2, _ = make_snapshot(admin, cap2, db_stats=[
        {"blks_hit_delta": 89, "blks_read_delta": 11},
    ])
    assert evaluate(sid2) == (0, 0)
    rows = alerts(admin)
    assert len(rows) == 1
    assert rows[0]["resolved_at"] is None
    assert rows[0]["last_seen_snapshot_id"] == sid2
    assert float(rows[0]["observed_seconds"]) == 60.0

    # Six minutes later the earlier low-ratio snapshots have rolled out of the
    # five-minute window; one clean 200-block sample resolves the alert.
    cap3 = CAP + timedelta(minutes=6)
    sid3, _ = make_snapshot(admin, cap3, db_stats=[
        {"blks_hit_delta": 200, "blks_read_delta": 0},
    ])
    assert evaluate(sid3) == (0, 1)
    rows = alerts(admin)
    assert close_time(rows[0]["resolved_at"], cap3)
    assert rows[0]["resolved_snapshot_id"] == sid3


def test_cache_hit_ratio_insufficient_window_does_not_resolve(admin):
    sid1, _ = make_snapshot(admin, CAP, db_stats=[
        {"blks_hit_delta": 89, "blks_read_delta": 11},
    ])
    assert evaluate(sid1) == (1, 0)

    # Old samples fall out of the window and the new snapshot has only 30
    # blocks: unknown, so the open alert must stay open.
    sid2, _ = make_snapshot(admin, CAP + timedelta(minutes=6), db_stats=[
        {"blks_hit_delta": 20, "blks_read_delta": 10},
    ])
    assert evaluate(sid2) == (0, 0)
    rows = alerts(admin)
    assert len(rows) == 1
    assert rows[0]["resolved_at"] is None
    assert rows[0]["last_seen_snapshot_id"] == sid1


# --------------------------------------------------------------------------- #
# dead_tuple_risk
# --------------------------------------------------------------------------- #
def test_dead_tuple_risk_below_dead_count_no_alert(admin):
    sid, _ = make_snapshot(admin, CAP, tables=[
        {"relid": 70001, "n_live_tup": 1, "n_dead_tup": 999},   # ratio .999
    ])
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_dead_tuple_risk_below_ratio_no_alert(admin):
    sid, _ = make_snapshot(admin, CAP, tables=[
        {"relid": 70001, "n_live_tup": 4001, "n_dead_tup": 1000},  # .19999
    ])
    assert evaluate(sid) == (0, 0)
    assert alerts(admin) == []


def test_dead_tuple_risk_at_threshold_opens_per_table(admin):
    sid, cap = make_snapshot(admin, CAP, tables=[
        {"relid": 70001, "relname": "orders",
         "n_live_tup": 4000, "n_dead_tup": 1000},               # exactly 0.20
        {"relid": 70002, "relname": "customers",
         "n_live_tup": 9000, "n_dead_tup": 100},                # 100 dead
    ])
    assert evaluate(sid) == (1, 0)

    rows = alerts(admin)
    assert len(rows) == 1
    a = rows[0]
    assert a["rule_name"] == "dead_tuple_risk"
    assert a["severity"] == "warning"
    assert a["blocked_pid"] == 70001             # relid::int
    assert a["blocker_pid"] == 0
    assert close_time(a["first_seen_at"], cap)
    assert close_time(a["eligible_at"], cap)
    assert close_time(a["fired_at"], cap)
    assert a["fired_snapshot_id"] == sid
    assert float(a["observed_seconds"]) == 0.0


def test_dead_tuple_risk_persists_without_duplicate_then_resolves(admin):
    sid1, _ = make_snapshot(admin, CAP, tables=[
        {"relid": 70001, "n_live_tup": 4000, "n_dead_tup": 1000},
    ])
    assert evaluate(sid1) == (1, 0)

    cap2 = CAP + timedelta(seconds=30)
    sid2, _ = make_snapshot(admin, cap2, tables=[
        {"relid": 70001, "n_live_tup": 4000, "n_dead_tup": 1200},
    ])
    assert evaluate(sid2) == (0, 0)
    assert evaluate(sid2) == (0, 0)              # idempotent replay
    rows = alerts(admin)
    assert len(rows) == 1
    assert rows[0]["resolved_at"] is None
    assert rows[0]["last_seen_snapshot_id"] == sid2
    assert float(rows[0]["observed_seconds"]) == 30.0

    # VACUUM-like snapshot: dead tuples gone -> resolved on first clean sample.
    cap3 = cap2 + timedelta(seconds=30)
    sid3, _ = make_snapshot(admin, cap3, tables=[
        {"relid": 70001, "n_live_tup": 5000, "n_dead_tup": 0},
    ])
    assert evaluate(sid3) == (0, 1)
    rows = alerts(admin)
    assert close_time(rows[0]["resolved_at"], cap3)
    assert rows[0]["resolved_snapshot_id"] == sid3

    sid4, _ = make_snapshot(admin, cap3 + timedelta(seconds=30), tables=[
        {"relid": 70001, "n_live_tup": 5000, "n_dead_tup": 0},
    ])
    assert evaluate(sid4) == (0, 0)


def test_dead_tuple_risk_empty_table_sample_is_unknown_not_clean(admin):
    sid1, _ = make_snapshot(admin, CAP, tables=[
        {"relid": 70001, "n_live_tup": 4000, "n_dead_tup": 1000},
    ])
    assert evaluate(sid1) == (1, 0)

    # No table rows at all for this snapshot (for example collect_extra is not
    # installed): unknown, so do not resolve the open alert.
    sid2, _ = make_snapshot(admin, CAP + timedelta(seconds=30))
    assert evaluate(sid2) == (0, 0)
    rows = alerts(admin)
    assert len(rows) == 1
    assert rows[0]["resolved_at"] is None


# --------------------------------------------------------------------------- #
# pipeline wiring: the real collector must reach these rules
# --------------------------------------------------------------------------- #
def test_collector_pipeline_reaches_the_more_rules(admin):
    """capture_snapshot() -> evaluate_alerts() -> evaluate_more_rules(), and
    capture_snapshot() -> collect_extra() (fills table_stat_sample for dead_tuple_risk).

    The other tests call evaluate_more_rules() directly; without this check the
    rules can pass every test and still never fire from the scheduled collector.
    """
    def body(signature: str) -> str:
        return admin.execute("SELECT pg_get_functiondef(%s::regprocedure)",
                             (signature,)).fetchone()[0]

    assert "evaluate_more_rules" in body("monitor.evaluate_alerts(bigint)")
    assert "collect_extra" in body("monitor.capture_snapshot()")
