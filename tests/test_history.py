"""M1-M3 tests (Dustin Dowell): shop schema, roles, capture_snapshot(), purge.

Real database, no mocks -- same fixtures and database as the T2 capture tests
(``shopdb_t2``, see tests/conftest.py). Build it with:

    bash scripts/setup_db.sh shopdb_t2

Covers proposal 8.3 "unit-level SQL checks": after each collector run a new
snapshot exists, every history table received rows for it, and computed deltas
are NULL on the first sample and non-negative afterwards.
"""
from __future__ import annotations

import time

import psycopg
import pytest

INSUFFICIENT_PRIVILEGE = "42501"
SHOP_TABLES = {"customers", "products", "inventory", "orders", "order_items", "payments"}


def _poll(fn, timeout: float = 10.0, every: float = 0.1):
    """Return fn()'s first truthy value; statistics flush asynchronously."""
    deadline = time.monotonic() + timeout
    value = fn()
    while not value and time.monotonic() < deadline:
        time.sleep(every)
        value = fn()
    return value


def _seq_scans(admin, relname: str) -> int:
    return admin.execute(
        "SELECT seq_scan FROM pg_stat_user_tables WHERE schemaname = 'shop' AND relname = %s",
        (relname,),
    ).fetchone()[0]


# --------------------------------------------------------------------------- #
# M1 / M2: schema and roles
# --------------------------------------------------------------------------- #
def test_shop_has_six_tables_and_the_intentional_missing_index(admin):
    tables = {
        r[0] for r in admin.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'shop'"
        ).fetchall()
    }
    assert tables == SHOP_TABLES, f"shop tables: {sorted(tables)}"

    leading_product_id = admin.execute(
        """
        SELECT count(*)
        FROM pg_index x
        JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = x.indkey[0]
        WHERE x.indrelid = 'shop.order_items'::regclass AND a.attname = 'product_id'
        """
    ).fetchone()[0]
    assert leading_product_id == 0, (
        "shop.order_items(product_id) must stay unindexed for the missing-index scenario"
    )

    # seed consistency: every order total equals the sum of its lines
    mismatched = admin.execute(
        """
        SELECT count(*) FROM shop.orders o
        JOIN (SELECT order_id, sum(quantity * unit_price) AS s
                FROM shop.order_items GROUP BY order_id) i USING (order_id)
        WHERE o.total_amount <> i.s
        """
    ).fetchone()[0]
    assert mismatched == 0


def test_collector_is_pg_monitor_member_and_not_superuser(admin):
    is_member, is_super = admin.execute(
        "SELECT pg_has_role('monitor_collector', 'pg_monitor', 'MEMBER'), "
        "rolsuper FROM pg_roles WHERE rolname = 'monitor_collector'"
    ).fetchone()
    assert is_member and not is_super


# --------------------------------------------------------------------------- #
# M3: collector
# --------------------------------------------------------------------------- #
def test_capture_snapshot_fills_every_history_table(clean_history, admin, collect):
    summary = collect()
    sid = summary["snapshot_id"]
    assert "statements" in summary, "collector did not use monitor.capture_snapshot()"

    counts = admin.execute(
        """
        SELECT (SELECT count(*) FROM monitor.db_stat_sample      WHERE snapshot_id = %(s)s),
               (SELECT count(*) FROM monitor.statement_history   WHERE snapshot_id = %(s)s),
               (SELECT count(*) FROM monitor.table_stats_history WHERE snapshot_id = %(s)s
                                                                 AND schemaname = 'shop'),
               (SELECT count(*) FROM monitor.index_stats_history WHERE snapshot_id = %(s)s
                                                                 AND schemaname = 'shop')
        """,
        {"s": sid},
    ).fetchone()
    db_rows, stmt_rows, shop_tables, shop_indexes = counts
    assert db_rows == 1
    assert stmt_rows > 0 and stmt_rows == summary["statements"]
    assert shop_tables == len(SHOP_TABLES)
    assert shop_indexes >= 9  # 6 PKs + customers.email + products.sku + 2 FK indexes
    assert summary["tables"] >= shop_tables and summary["indexes"] >= shop_indexes

    sizes = admin.execute(
        "SELECT count(*) FROM monitor.table_stats_history "
        "WHERE snapshot_id = %s AND schemaname = 'shop' AND total_bytes > 0",
        (sid,),
    ).fetchone()[0]
    assert sizes == len(SHOP_TABLES), "table sizes should be read when nothing is locked"


def test_table_deltas_null_first_then_count_new_seq_scans(
    clean_history, admin, shop_connect, collect
):
    first = collect()
    first_deltas = admin.execute(
        "SELECT seq_scan_delta, n_tup_ins_delta FROM monitor.table_stats_history "
        "WHERE snapshot_id = %s AND schemaname = 'shop' AND relname = 'order_items'",
        (first["snapshot_id"],),
    ).fetchone()
    assert first_deltas == (None, None), f"first sample deltas must be NULL: {first_deltas}"

    before = _seq_scans(admin, "order_items")
    app = shop_connect("history_seqscan")
    for product_id in (3, 5, 7):  # no index on product_id -> sequential scans
        app.execute("SELECT count(*) FROM shop.order_items WHERE product_id = %s",
                    (product_id,)).fetchone()
    app.commit()
    app.close()  # backend exit flushes its pending statistics
    assert _poll(lambda: _seq_scans(admin, "order_items") >= before + 3), (
        "seq-scan counter did not advance"
    )

    second = collect()
    seq_delta, = admin.execute(
        "SELECT seq_scan_delta FROM monitor.table_stats_history "
        "WHERE snapshot_id = %s AND schemaname = 'shop' AND relname = 'order_items'",
        (second["snapshot_id"],),
    ).fetchone()
    assert seq_delta is not None and seq_delta >= 3, f"seq_scan_delta={seq_delta}"

    negative = admin.execute(
        """
        SELECT count(*) FROM monitor.table_stats_history
        WHERE snapshot_id = %(s)s
          AND least(seq_scan_delta, seq_tup_read_delta, idx_scan_delta, n_tup_ins_delta,
                    n_tup_upd_delta, n_tup_del_delta, heap_blks_read_delta,
                    heap_blks_hit_delta) < 0
        UNION ALL
        SELECT count(*) FROM monitor.index_stats_history
        WHERE snapshot_id = %(s)s AND least(idx_scan_delta, idx_tup_read_delta) < 0
        UNION ALL
        SELECT count(*) FROM monitor.statement_history
        WHERE snapshot_id = %(s)s
          AND least(calls_delta, rows_delta, shared_blks_hit_delta,
                    shared_blks_read_delta) < 0
        """,
        {"s": second["snapshot_id"]},
    ).fetchall()
    assert [n for (n,) in negative] == [0, 0, 0], f"negative deltas: {negative}"


def test_statement_history_keeps_normalized_text_and_no_password_text(
    clean_history, admin, shop_connect, collect
):
    app = shop_connect("history_statements")
    app.execute("SELECT count(*) FROM shop.order_items WHERE product_id = %s", (11,)).fetchone()
    app.commit()
    # a statement whose text contains "password" must never be stored
    admin.execute("SELECT 1 AS password_probe")

    sid = collect()["snapshot_id"]
    texts = [
        r[0] for r in admin.execute(
            "SELECT query_text FROM monitor.statement_history WHERE snapshot_id = %s",
            (sid,),
        ).fetchall()
    ]
    # Scope the probe to the exact SELECT executed above.  Schema load and seed
    # DDL/DML also mention shop.order_items/product_id, and with
    # pg_stat_statements.track = all those unnormalized texts are captured too;
    # a broad substring probe would mistake them for the query under test.
    select_prefix = "SELECT count(*) FROM shop.order_items WHERE product_id ="
    probe = [t for t in texts if t.lstrip().startswith(select_prefix)]
    assert probe, "the order_items lookup is missing from statement_history"
    assert all("$1" in t and "11" not in t for t in probe), f"not normalized: {probe}"
    assert not [t for t in texts if "password" in t.lower()], "password text was stored"


def test_reader_sees_new_views_but_not_history_tables(reader):
    for view in ("v_statement_history", "v_table_stats_history", "v_index_stats_history"):
        reader.execute(f"SELECT * FROM monitor.{view} LIMIT 0")
    reader.rollback()
    with pytest.raises(psycopg.Error) as excinfo:
        reader.execute("SELECT count(*) FROM monitor.table_stats_history")
    assert excinfo.value.sqlstate == INSUFFICIENT_PRIVILEGE
    reader.rollback()


# --------------------------------------------------------------------------- #
# M3: retention
# --------------------------------------------------------------------------- #
def test_purge_keeps_recent_newest_and_alert_evidence(clean_history, admin, collector_conn):
    old, evidence, recent = [
        admin.execute(
            "INSERT INTO monitor.snapshot (captured_at) VALUES (now() - %s::interval) "
            "RETURNING snapshot_id",
            (age,),
        ).fetchone()[0]
        for age in ("10 days", "9 days", "1 hour")
    ]
    admin.execute(
        "INSERT INTO monitor.table_stats_history (snapshot_id, relid, schemaname, relname) "
        "VALUES (%s, 1, 'shop', 'probe')",
        (old,),
    )
    admin.execute(
        """
        INSERT INTO monitor.alert_log
            (rule_name, severity, blocked_pid, blocker_pid, first_seen_at, eligible_at,
             fired_at, fired_snapshot_id, observed_seconds, last_seen_snapshot_id,
             resolved_at, resolved_snapshot_id)
        VALUES ('blocked_session', 'critical', 1, 2, now() - interval '9 days',
                now() - interval '9 days', now() - interval '9 days', %(e)s, 31.0, %(e)s,
                now() - interval '9 days', %(e)s)
        """,
        {"e": evidence},
    )

    deleted = collector_conn.execute("SELECT monitor.purge_old_snapshots()").fetchone()[0]
    collector_conn.commit()
    assert deleted == 1

    remaining = {
        r[0] for r in admin.execute("SELECT snapshot_id FROM monitor.snapshot").fetchall()
    }
    assert remaining == {evidence, recent}
    orphans = admin.execute(
        "SELECT count(*) FROM monitor.table_stats_history WHERE snapshot_id = %s", (old,)
    ).fetchone()[0]
    assert orphans == 0, "detail rows must cascade with their snapshot"

    with pytest.raises(psycopg.Error, match="at least 1 hour"):
        collector_conn.execute("SELECT monitor.purge_old_snapshots(interval '5 minutes')")
    collector_conn.rollback()
