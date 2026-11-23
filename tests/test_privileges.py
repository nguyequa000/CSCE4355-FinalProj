"""Least-privilege tests against the real database (no mocks).

monitor_reader : SELECT on every monitor.v_* view, nothing else
shop_app       : no access to monitor history at all
Every denial must be SQLSTATE 42501 (insufficient_privilege).
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

INSUFFICIENT_PRIVILEGE = "42501"


def assert_insufficient_privilege(exc: psycopg.Error, what: str) -> None:
    assert exc.sqlstate == INSUFFICIENT_PRIVILEGE, (
        f"{what}: expected SQLSTATE {INSUFFICIENT_PRIVILEGE} "
        f"(insufficient_privilege), got {exc.sqlstate}: {exc}"
    )


def test_reader_can_select_every_monitor_view(admin, reader):
    views = [
        row[0]
        for row in admin.execute(
            """
            SELECT table_name
            FROM information_schema.views
            WHERE table_schema = 'monitor'
            ORDER BY table_name
            """
        ).fetchall()
    ]
    assert len(views) >= 6, f"expected the six reporting views, found {views}"
    expected = {
        "v_snapshot", "v_session_history", "v_lock_history",
        "v_current_blockers", "v_db_stats", "v_alerts",
    }
    assert expected <= set(views), f"missing reporting views: {expected - set(views)}"

    for view in views:
        # identifier is quoted, so a view name can never be injected
        reader.execute(
            sql.SQL("SELECT * FROM monitor.{} LIMIT 0").format(sql.Identifier(view))
        )
    reader.rollback()


def test_reader_cannot_insert_alert_log(reader):
    with pytest.raises(psycopg.Error) as excinfo:
        reader.execute(
            """
            INSERT INTO monitor.alert_log
                (rule_name, severity, blocked_pid, blocker_pid, first_seen_at,
                 eligible_at, fired_at, fired_snapshot_id, observed_seconds,
                 last_seen_snapshot_id)
            VALUES ('blocked_session', 'critical', 1, 2, now(), now(), now(),
                    1, 1.0, 1)
            """
        )
    assert_insufficient_privilege(excinfo.value, "reader INSERT monitor.alert_log")
    reader.rollback()


def test_reader_cannot_select_snapshot_base_table(reader):
    with pytest.raises(psycopg.Error) as excinfo:
        reader.execute("SELECT count(*) FROM monitor.snapshot")
    assert_insufficient_privilege(excinfo.value, "reader SELECT monitor.snapshot")
    reader.rollback()


def test_reader_cannot_select_shop_customers(reader):
    with pytest.raises(psycopg.Error) as excinfo:
        reader.execute("SELECT count(*) FROM shop.customers")
    assert_insufficient_privilege(excinfo.value, "reader SELECT shop.customers")
    reader.rollback()


def test_shop_app_cannot_select_monitor_alerts_view(shop_connect):
    app = shop_connect("t2_privilege_app")
    with pytest.raises(psycopg.Error) as excinfo:
        app.execute("SELECT count(*) FROM monitor.v_alerts")
    assert_insufficient_privilege(excinfo.value, "shop_app SELECT monitor.v_alerts")
    app.rollback()


def test_shop_app_cannot_select_monitor_snapshot(shop_connect):
    app = shop_connect("t2_privilege_app2")
    with pytest.raises(psycopg.Error) as excinfo:
        app.execute("SELECT count(*) FROM monitor.snapshot")
    assert_insufficient_privilege(excinfo.value, "shop_app SELECT monitor.snapshot")
    app.rollback()
