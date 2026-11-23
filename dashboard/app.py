"""CSCE 4355 — PostgreSQL monitoring dashboard.

Read-only Streamlit console for the project's monitor history. It connects as
``monitor_reader`` (the only role with SELECT on the sanitized ``monitor.v_*``
views), and every statement it issues is a SELECT executed on a session marked
``default_transaction_read_only=on``.

Run:  .venv/Scripts/python.exe -m streamlit run dashboard/app.py --server.port 8501
Env:  DASH_DB   database name (default: PGDATABASE from .env, else shopdb)
      COLLECT_INTERVAL_SECONDS  collector cadence used for the staleness check
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect, load_env  # noqa: E402

CT = ZoneInfo("America/Chicago")
REFRESH_SECONDS = 15
DEFAULT_WINDOW = 12

st.set_page_config(
    page_title="PostgreSQL Monitoring Dashboard (CSCE 4355)",
    page_icon="🛢️",
    layout="wide",
)

# --------------------------------------------------------------------------- #
# data access — SELECT only, reader role, read-only transaction
# --------------------------------------------------------------------------- #
VIEWS = {
    "snapshot": "monitor.v_snapshot",
    "sessions": "monitor.v_session_history",
    "locks": "monitor.v_lock_history",
    "blockers": "monitor.v_current_blockers",
    "dbstats": "monitor.v_db_stats",
    "alerts": "monitor.v_alerts",
}


def _guard(sql: str) -> str:
    """Refuse anything that is not a single read-only SELECT."""
    body = sql.strip().lstrip("(").lower()
    if not body.startswith(("select", "with")):
        raise RuntimeError(f"dashboard is read-only; blocked statement: {sql[:60]}")
    for bad in ("insert ", "update ", "delete ", "drop ", "alter ", "truncate ", "grant "):
        if bad in body:
            raise RuntimeError(f"dashboard is read-only; blocked keyword {bad!r}")
    return sql


def _q(conn, sql: str, params=None) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(_guard(sql), params)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


@st.cache_data(ttl=5, show_spinner=False)
def load_data(dbname: str, window: int) -> dict[str, pd.DataFrame]:
    """One reader connection per refresh; returns every table the page needs."""
    out: dict[str, pd.DataFrame] = {}
    with connect(
        "monitor_reader",
        dbname=dbname,
        connect_timeout=5,
        options="-c default_transaction_read_only=on -c statement_timeout=8000",
    ) as conn:
        out["who"] = _q(
            conn,
            "SELECT current_user AS connected_role, current_database() AS dbname, "
            "now() AS server_now",
        )
        out["snapshot"] = _q(
            conn,
            "SELECT snapshot_id, captured_at FROM monitor.v_snapshot "
            "ORDER BY snapshot_id DESC LIMIT %s",
            (window,),
        )
        ids = tuple(int(x) for x in out["snapshot"]["snapshot_id"]) or (0,)
        out["locks"] = _q(
            conn,
            "SELECT snapshot_id, captured_at, blocked_pid, blocker_pid, blocked_user, "
            "blocker_user, blocked_app, blocker_app, blocker_state, wait_started, "
            "waited_seconds FROM monitor.v_lock_history "
            "WHERE snapshot_id = ANY(%s) ORDER BY snapshot_id, waited_seconds DESC",
            (list(ids),),
        )
        out["sessions"] = _q(
            conn,
            "SELECT snapshot_id, captured_at, state, count(*) AS sessions "
            "FROM monitor.v_session_history WHERE snapshot_id = ANY(%s) "
            "GROUP BY 1, 2, 3 ORDER BY 1, 3",
            (list(ids),),
        )
        out["dbstats"] = _q(
            conn,
            "SELECT snapshot_id, captured_at, datname, numbackends, deadlocks, "
            "xact_commit_delta, xact_rollback_delta, blks_read_delta, blks_hit_delta "
            "FROM monitor.v_db_stats WHERE snapshot_id = ANY(%s) ORDER BY snapshot_id",
            (list(ids),),
        )
        out["blockers"] = _q(
            conn,
            "SELECT snapshot_id, captured_at, blocked_pid, blocker_pid, blocked_user, "
            "blocker_user, blocked_app, blocker_app, blocker_state, wait_started, "
            "waited_seconds FROM monitor.v_current_blockers "
            "ORDER BY waited_seconds DESC NULLS LAST",
        )
        out["alerts_open"] = _q(
            conn,
            "SELECT alert_id, rule_name, severity, blocked_pid, blocker_pid, "
            "first_seen_at, eligible_at, fired_at, observed_seconds, notified_at "
            "FROM monitor.v_alerts WHERE status = 'open' ORDER BY fired_at DESC",
        )
        out["alerts_resolved"] = _q(
            conn,
            "SELECT alert_id, rule_name, severity, blocked_pid, blocker_pid, fired_at, "
            "resolved_at, observed_seconds, notified_at, resolve_notified_at "
            "FROM monitor.v_alerts WHERE status = 'resolved' "
            "ORDER BY resolved_at DESC NULLS LAST LIMIT 25",
        )
    return out


# --------------------------------------------------------------------------- #
# formatting helpers — every timestamp is shown in America/Chicago
# --------------------------------------------------------------------------- #
def ct(ts) -> str:
    if ts is None or pd.isna(ts):
        return "—"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(CT).strftime("%Y-%m-%d %H:%M:%S %Z")


def age_seconds(ts) -> float | None:
    if ts is None or pd.isna(ts):
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def num(x):
    return int(x) if x is not None and not pd.isna(x) else None


# --------------------------------------------------------------------------- #
# sidebar (outside the auto-refreshing fragment so its widgets stay live)
# --------------------------------------------------------------------------- #
env = load_env()
DBNAME = os.environ.get("DASH_DB") or env.get("PGDATABASE", "shopdb")
INTERVAL = int(os.environ.get("COLLECT_INTERVAL_SECONDS") or env.get("COLLECT_INTERVAL_SECONDS") or 60)
STALE_AFTER = 2.5 * INTERVAL

with st.sidebar:
    st.header("Connection")
    st.caption("read-only role · SELECT on monitor.v_* views only")
    st.code(f"database: {DBNAME}\nrole:     monitor_reader\nhost:     {env.get('PGHOST', '127.0.0.1')}:{env.get('PGPORT', '55432')}",
            language="text")
    st.header("View")
    window = st.slider("Snapshots in history", 3, 60, DEFAULT_WINDOW, 1)
    auto = st.checkbox(f"Auto-refresh every {REFRESH_SECONDS} s", value=True)
    if st.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.caption(
        f"Timestamps: America/Chicago (CT). Staleness warning when the latest "
        f"snapshot is older than 2.5 x {INTERVAL}s collector interval "
        f"({STALE_AFTER:.0f}s)."
    )


def render() -> None:
    try:
        data = load_data(DBNAME, window)
    except Exception as exc:  # noqa: BLE001 - surface any driver/auth error plainly
        st.error("Could not read the monitoring views as `monitor_reader`.")
        st.exception(exc)
        return

    who = data["who"].iloc[0]
    snaps = data["snapshot"]
    latest_id = num(snaps["snapshot_id"].iloc[0]) if len(snaps) else None
    latest_ts = snaps["captured_at"].iloc[0] if len(snaps) else None
    age = age_seconds(latest_ts)
    stale = age is not None and age > STALE_AFTER

    st.title("PostgreSQL Monitoring Dashboard (CSCE 4355)")
    st.caption(
        "Historical lock/wait capture, current blockers and alert state. "
        f"Read-only session as `{who['connected_role']}` · all times America/Chicago (CT)."
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Database", str(who["dbname"]))
    c2.metric("Connected role", str(who["connected_role"]))
    c3.metric("Latest snapshot", f"#{latest_id}" if latest_id else "none")
    c4.metric("Snapshot age", f"{age:.0f} s" if age is not None else "—")

    if latest_id is None:
        st.warning("No snapshots yet — start the collector (`scripts/collect.py`).")
    elif stale:
        st.error(
            f"STALE DATA: latest snapshot #{latest_id} captured {ct(latest_ts)} is "
            f"{age:.0f}s old (> {STALE_AFTER:.0f}s = 2.5 x {INTERVAL}s interval). "
            "The collector may have stopped."
        )
    else:
        st.success(
            f"Collector current: latest snapshot #{latest_id} at {ct(latest_ts)} "
            f"({age:.0f}s ago). Server clock {ct(who['server_now'])}."
        )

    # ---------------- 1. alerts ----------------
    st.subheader("1 · Alerts")
    open_alerts = data["alerts_open"]
    resolved = data["alerts_resolved"]
    a1, a2 = st.columns([1, 1])
    a1.metric("Open alerts", len(open_alerts))
    a2.metric("Resolved alerts (shown)", len(resolved))

    if len(open_alerts):
        st.error(f"{len(open_alerts)} open alert(s) — blocking threshold currently exceeded.")
        show = open_alerts.assign(
            fired_at=open_alerts["fired_at"].map(ct),
            first_seen_at=open_alerts["first_seen_at"].map(ct),
            eligible_at=open_alerts["eligible_at"].map(ct),
            notified_at=open_alerts["notified_at"].map(ct),
        )
        styled = show.style.apply(
            lambda _row: ["background-color: #5c1a1a"] * len(_row), axis=1
        )
        st.dataframe(styled, width="stretch", hide_index=True)
    else:
        st.success("No open alerts.")

    st.markdown("**Resolved alerts**")
    if len(resolved):
        show = resolved.assign(
            fired_at=resolved["fired_at"].map(ct),
            resolved_at=resolved["resolved_at"].map(ct),
            notified_at=resolved["notified_at"].map(ct),
            resolve_notified_at=resolved["resolve_notified_at"].map(ct),
        )
        st.dataframe(show, width="stretch", hide_index=True)
    else:
        st.caption("No resolved alerts in `monitor.v_alerts`.")

    # ---------------- 2. current blockers ----------------
    st.subheader("2 · Current blockers")
    blockers = data["blockers"]
    if len(blockers):
        st.warning(f"{len(blockers)} blocked/blocker edge(s) at snapshot #{latest_id}.")
        show = blockers.assign(
            captured_at=blockers["captured_at"].map(ct),
            wait_started=blockers["wait_started"].map(ct),
        )
        st.dataframe(show, width="stretch", hide_index=True)
    else:
        st.info("No blocking at latest snapshot.")

    # ---------------- 3. lock history ----------------
    st.subheader(f"3 · Lock history (last {len(snaps)} snapshots)")
    locks = data["locks"]
    if len(locks):
        show = locks.assign(
            captured_at=locks["captured_at"].map(ct),
            wait_started=locks["wait_started"].map(ct),
        )
        st.dataframe(show, width="stretch", hide_index=True)

        edges = (
            locks.groupby(["snapshot_id", "captured_at"], as_index=False)
            .size()
            .rename(columns={"size": "lock_edges"})
            .sort_values("snapshot_id")
        )
        edges["snapshot"] = [
            f"#{r.snapshot_id} {ct(r.captured_at)[11:19]}" for r in edges.itertuples()
        ]
        st.caption("Lock edges captured per snapshot (waiter → blocker pairs)")
        st.line_chart(edges.set_index("snapshot")["lock_edges"], height=220)
    else:
        st.info("No lock waits recorded in the selected window.")

    # ---------------- 4. sessions + database stats ----------------
    st.subheader("4 · Session state and database counters")
    sess = data["sessions"]
    if len(sess):
        pivot = (
            sess.pivot_table(
                index=["snapshot_id", "captured_at"],
                columns="state",
                values="sessions",
                aggfunc="sum",
                fill_value=0,
            )
            .reset_index()
            .sort_values("snapshot_id")
        )
        pivot["snapshot"] = [
            f"#{int(r.snapshot_id)} {ct(r.captured_at)[11:19]}" for r in pivot.itertuples()
        ]
        state_cols = [c for c in pivot.columns if c not in ("snapshot_id", "captured_at", "snapshot")]
        st.caption("Client backends per state, per snapshot")
        st.line_chart(pivot.set_index("snapshot")[state_cols], height=240)
    else:
        st.info("No session samples in the selected window.")

    stats = data["dbstats"]
    if len(stats):
        delta_cols = ["xact_commit_delta", "xact_rollback_delta", "blks_read_delta", "blks_hit_delta"]
        # NULL delta = unknown (first sample / stats reset), never 0. Values are
        # rendered as text so an "unknown" cell keeps one Arrow-safe column type.
        shown = stats.assign(
            captured_at=stats["captured_at"].map(ct),
            **{
                c: stats[c].map(lambda v: "unknown" if pd.isna(v) else f"{int(v):,}")
                for c in delta_cols
            },
        )
        st.caption("Database counters per snapshot — `unknown` means no comparable previous sample (NULL delta), not zero.")
        st.dataframe(shown, width="stretch", hide_index=True)

        chart = stats.dropna(subset=["xact_commit_delta"]).copy()
        if len(chart):
            chart["snapshot"] = [
                f"#{int(r.snapshot_id)} {ct(r.captured_at)[11:19]}" for r in chart.itertuples()
            ]
            st.caption("xact_commit delta per interval")
            st.bar_chart(chart.set_index("snapshot")["xact_commit_delta"], height=200)
    else:
        st.info("No database counter samples in the selected window.")

    st.divider()
    st.caption(
        f"Rendered {ct(datetime.now(timezone.utc))} · source views: "
        + ", ".join(VIEWS.values())
        + " · auto-refresh " + (f"{REFRESH_SECONDS}s" if auto else "off")
    )


# st.fragment(run_every=...) exists in Streamlit >= 1.37; fall back gracefully.
if hasattr(st, "fragment") and auto:
    st.fragment(run_every=f"{REFRESH_SECONDS}s")(render)()
elif hasattr(st, "fragment"):
    st.fragment(render)()
else:  # pragma: no cover - very old Streamlit
    render()
    if auto:
        st.caption(f"(fallback refresh: reloading in {REFRESH_SECONDS}s)")
        time.sleep(REFRESH_SECONDS)
        st.rerun()
