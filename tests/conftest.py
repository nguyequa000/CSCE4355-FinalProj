"""Shared fixtures for the T2 capture / privilege tests.

These tests talk to a REAL PostgreSQL database — there are no mocks.  The
database defaults to ``shopdb_t2`` (override with ``TEST_DB``) and must have
been created with::

    bash scripts/setup_db.sh shopdb_t2

Credentials stay in the git-ignored ``.env``; nothing is printed here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # make `scripts` importable regardless of cwd
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect  # noqa: E402

TEST_DB = os.environ.get("TEST_DB", "shopdb_t2")


@pytest.fixture(scope="session")
def db_name() -> str:
    if TEST_DB == "shopdb":
        pytest.exit(
            "refusing to run destructive T2 tests against 'shopdb' "
            "(integration/demo database); set TEST_DB=shopdb_t2",
            returncode=2,
        )
    return TEST_DB


@pytest.fixture(scope="session")
def admin(db_name: str):
    """Superuser connection to the test database (autocommit, for setup/probes)."""
    conn = connect("postgres", dbname=db_name, autocommit=True)
    missing = conn.execute(
        """
        SELECT to_regclass('monitor.snapshot'), to_regclass('monitor.session_sample'),
               to_regclass('monitor.lock_wait'), to_regclass('monitor.db_stat_sample'),
               to_regclass('monitor.alert_log'), to_regclass('shop.customers')
        """
    ).fetchone()
    if any(m is None for m in missing):
        conn.close()
        pytest.fail(
            f"database {db_name!r} is not initialized "
            f"(missing objects: {missing}); run: bash scripts/setup_db.sh {db_name}"
        )
    yield conn
    conn.close()


@pytest.fixture
def clean_history(admin):
    """Start each test from an empty monitor history (dedicated test DB only)."""
    admin.execute("TRUNCATE monitor.snapshot RESTART IDENTITY CASCADE")
    yield


@pytest.fixture(scope="session")
def collector_conn(db_name: str):
    """Long-lived monitor_collector connection, one transaction per snapshot."""
    conn = connect("monitor_collector", dbname=db_name)
    yield conn
    try:
        conn.rollback()
    finally:
        conn.close()


@pytest.fixture
def collect(collector_conn):
    """Callable that takes one real snapshot and returns its summary dict."""
    from scripts.collect import take_snapshot

    def _collect():
        return take_snapshot(collector_conn, log=lambda line: None)

    return _collect


@pytest.fixture
def shop_connect(db_name: str):
    """Factory for shop_app connections; all are rolled back/closed on teardown."""
    conns: list[psycopg.Connection] = []

    def _connect(application_name: str = "t2_pytest_shop") -> psycopg.Connection:
        conn = connect("shop_app", dbname=db_name, application_name=application_name)
        conns.append(conn)
        return conn

    yield _connect

    for conn in conns:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


@pytest.fixture
def reader(db_name: str):
    conn = connect("monitor_reader", dbname=db_name, application_name="t2_pytest_reader")
    yield conn
    try:
        conn.rollback()
    finally:
        conn.close()
