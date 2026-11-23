"""Real-database capture tests for the T2 collector (no mocks).

Covers:
  * two unattended snapshots have distinct, increasing captured_at
  * a real row-lock wait between two shop_app backends is captured as a
    (blocked_pid, blocker_pid) edge; after release the current edge is gone
    but the historical row survives
  * db_stat_sample deltas are NULL on the first sample and never negative later
"""
from __future__ import annotations

import threading
import time

WAIT_TIMEOUT = 15.0


def _wait_for_lock(admin, waiter_pid: int, timeout: float = 10.0) -> list[int]:
    """Poll pg_blocking_pids() until the waiter reports a blocker (or time out)."""
    deadline = time.monotonic() + timeout
    seen: list[int] = []
    while time.monotonic() < deadline:
        seen = admin.execute(
            "SELECT pg_blocking_pids(%s)", (waiter_pid,)
        ).fetchone()[0]
        if seen:
            return seen
        time.sleep(0.05)
    raise AssertionError(
        f"backend {waiter_pid} never blocked (last blockers={seen})"
    )


def _lock_rows(admin, snapshot_id: int) -> list[tuple]:
    return admin.execute(
        """
        SELECT blocked_pid, blocker_pid, blocked_app, blocker_app,
               blocker_state, wait_started, waited_seconds
        FROM monitor.lock_wait
        WHERE snapshot_id = %s
        ORDER BY blocked_pid, blocker_pid
        """,
        (snapshot_id,),
    ).fetchall()


def test_unattended_snapshots_have_distinct_increasing_timestamps(clean_history, collect):
    first = collect()
    second = collect()

    assert second["snapshot_id"] > first["snapshot_id"]
    assert second["captured_at"] > first["captured_at"], (
        f"captured_at did not increase: {first['captured_at']} -> {second['captured_at']}"
    )


def test_lock_wait_edge_captured_then_gone_but_history_kept(
    clean_history, admin, shop_connect, collect
):
    blocker = shop_connect("t2_blocker")
    waiter = shop_connect("t2_waiter")
    events: dict[str, object] = {}
    thread = threading.Thread(target=lambda: None, name="t2-waiter")

    def run_waiter() -> None:
        try:
            # pid must be read from the owning thread (libpq caches it at connect)
            events["pid"] = waiter.info.backend_pid
            waiter.execute("SET lock_timeout = '20s'")
            waiter.execute(
                "UPDATE shop.customers SET balance = balance WHERE customer_id = 1"
            )
            waiter.commit()
            events["done"] = True
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions
            events["error"] = exc
            try:
                waiter.rollback()
            except Exception:
                pass

    try:
        # blocker: BEGIN + UPDATE holds the row lock, then sits idle in transaction
        blocker.execute(
            "UPDATE shop.customers SET balance = balance WHERE customer_id = 1"
        )
        blocker_pid = blocker.info.backend_pid

        thread = threading.Thread(target=run_waiter, name="t2-waiter", daemon=True)
        thread.start()

        deadline = time.monotonic() + WAIT_TIMEOUT
        while "pid" not in events and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "pid" in events, "waiter thread did not start"
        waiter_pid = int(events["pid"])

        blockers = _wait_for_lock(admin, waiter_pid)
        assert blocker_pid in blockers, (
            f"expected blocker {blocker_pid} in pg_blocking_pids({waiter_pid})={blockers}"
        )

        during = collect()
        rows = _lock_rows(admin, during["snapshot_id"])
        assert (waiter_pid, blocker_pid) in [(r[0], r[1]) for r in rows], (
            f"missing lock_wait edge ({waiter_pid} -> {blocker_pid}); rows={rows}"
        )
        row = next(r for r in rows if r[0] == waiter_pid and r[1] == blocker_pid)
        assert row[2] == "t2_waiter"      # blocked_app
        assert row[3] == "t2_blocker"     # blocker_app
        assert row[4] == "idle in transaction"  # blocker_state
        assert row[5] is not None         # wait_started = waiter's state_change
        assert row[6] is not None and row[6] >= 0, f"waited_seconds={row[6]!r}"

        sample = admin.execute(
            """
            SELECT blocked_by FROM monitor.session_sample
            WHERE snapshot_id = %s AND pid = %s
            """,
            (during["snapshot_id"], waiter_pid),
        ).fetchone()
        assert sample is not None and blocker_pid in list(sample[0]), (
            f"session_sample.blocked_by={sample!r} missing {blocker_pid}"
        )

        # release the lock; the waiter's UPDATE must complete on its own
        blocker.rollback()
        thread.join(WAIT_TIMEOUT)
        assert not thread.is_alive(), "waiter still blocked after blocker rollback"
        assert "error" not in events, f"waiter failed: {events['error']!r}"

        after = collect()
        assert after["snapshot_id"] > during["snapshot_id"]
        assert (waiter_pid, blocker_pid) not in [
            (r[0], r[1]) for r in _lock_rows(admin, after["snapshot_id"])
        ], "released lock still reported in the newest snapshot"

        # history is immutable: the earlier edge is still queryable
        assert (waiter_pid, blocker_pid) in [
            (r[0], r[1]) for r in _lock_rows(admin, during["snapshot_id"])
        ], "historical lock_wait row disappeared after the lock was released"
    finally:
        try:
            blocker.rollback()
        except Exception:
            pass
        if thread.is_alive():
            try:
                waiter.close()  # cancel the blocked statement, then reap the thread
            except Exception:
                pass
            thread.join(5)
        for conn in (blocker, waiter):
            try:
                conn.close()
            except Exception:
                pass


def test_db_stat_deltas_null_first_then_non_negative(clean_history, admin, shop_connect, collect):
    first = collect()
    row = admin.execute(
        """
        SELECT xact_commit_delta, xact_rollback_delta, blks_read_delta, blks_hit_delta
        FROM monitor.db_stat_sample
        WHERE snapshot_id = %s AND datname = current_database()
        """,
        (first["snapshot_id"],),
    ).fetchone()
    assert row is not None, "no db_stat_sample row for the first snapshot"
    assert all(delta is None for delta in row), (
        f"first sample deltas should be NULL (unknown interval), got {row}"
    )

    # create a little real activity so the second interval has measurable counts
    app = shop_connect("t2_delta_activity")
    for _ in range(3):
        app.execute("SELECT count(*) FROM shop.customers").fetchone()
    app.commit()

    second = collect()
    assert second["snapshot_id"] > first["snapshot_id"]
    row2 = admin.execute(
        """
        SELECT xact_commit_delta, xact_rollback_delta, blks_read_delta, blks_hit_delta
        FROM monitor.db_stat_sample
        WHERE snapshot_id = %s AND datname = current_database()
        """,
        (second["snapshot_id"],),
    ).fetchone()
    assert row2 is not None, "no db_stat_sample row for the second snapshot"
    assert all(delta is not None for delta in row2), (
        f"later sample deltas should be known numbers, got {row2}"
    )
    assert all(delta >= 0 for delta in row2), f"negative delta in {row2}"
