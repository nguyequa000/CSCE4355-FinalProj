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
from typing import Any
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
    # optional history views (another lane owns them) — probed with to_regclass
    "table_stats": "monitor.v_table_stats",
    "statement_stats": "monitor.v_statement_stats",
    "hourly_rollup": "monitor.v_hourly_rollup",
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
def load_data(dbname: str, window: int) -> dict[str, Any]:
    """One reader connection per refresh; returns every table the page needs."""
    out: dict[str, Any] = {}
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
        # Optional history views are created by another lane. Probe with
        # to_regclass (never a direct SELECT) so a database without them still
        # renders the original panels.
        out["available"] = {
            str(row.view_name): bool(row.present)
            for row in _q(
                conn,
                "SELECT 'monitor.v_table_stats' AS view_name, "
                "to_regclass('monitor.v_table_stats') IS NOT NULL AS present "
                "UNION ALL SELECT 'monitor.v_statement_stats', "
                "to_regclass('monitor.v_statement_stats') IS NOT NULL "
                "UNION ALL SELECT 'monitor.v_hourly_rollup', "
                "to_regclass('monitor.v_hourly_rollup') IS NOT NULL",
            ).itertuples()
        }
        out["table_stats_latest"] = pd.DataFrame()
        out["table_stats_history"] = pd.DataFrame()
        out["statement_stats"] = pd.DataFrame()
        out["hourly_rollup"] = pd.DataFrame()
        out["dead_table_names"] = {}
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

        # Optional history panels: query each view only after to_regclass said
        # it exists, so a database without the new views keeps working.
        if out["available"].get("monitor.v_table_stats"):
            out["table_stats_latest"] = _q(
                conn,
                "SELECT snapshot_id, captured_at, schemaname, relname, n_live_tup, "
                "n_dead_tup, dead_ratio, seq_scan, idx_scan "
                "FROM monitor.v_table_stats "
                "WHERE snapshot_id = (SELECT max(snapshot_id) FROM monitor.v_table_stats) "
                "ORDER BY n_dead_tup DESC NULLS LAST, schemaname, relname",
            )
            out["table_stats_history"] = _q(
                conn,
                "SELECT snapshot_id, captured_at, schemaname, relname, n_dead_tup "
                "FROM monitor.v_table_stats WHERE snapshot_id = ANY(%s) "
                "ORDER BY snapshot_id, schemaname, relname",
                (list(ids),),
            )
            # dead_tuple_risk stores the table relid in blocked_pid. Resolve the
            # name from the latest v_table_stats snapshot (matched via pg_class,
            # since the view exposes schemaname/relname, not relid); fall back
            # to the bare relid in the app when this returns nothing.
            try:
                refs = _q(
                    conn,
                    "SELECT a.alert_id, ts.relname "
                    "FROM monitor.v_alerts a "
                    "LEFT JOIN pg_class c ON c.oid::bigint = a.blocked_pid::bigint "
                    "LEFT JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN LATERAL ("
                    "  SELECT t.relname FROM monitor.v_table_stats t "
                    "  WHERE t.snapshot_id = (SELECT max(snapshot_id) FROM monitor.v_table_stats) "
                    "    AND t.schemaname = n.nspname AND t.relname = c.relname "
                    "  LIMIT 1"
                    ") ts ON true "
                    "WHERE a.rule_name = 'dead_tuple_risk'",
                )
                out["dead_table_names"] = {
                    int(row.alert_id): str(row.relname)
                    for row in refs.itertuples()
                    if row.relname is not None and not pd.isna(row.relname)
                }
            except Exception:  # optional label enrichment only; keep the page up
                out["dead_table_names"] = {}

        if out["available"].get("monitor.v_statement_stats"):
            out["statement_stats"] = _q(
                conn,
                "SELECT snapshot_id, captured_at, queryid, calls_delta, "
                "total_exec_time_delta, mean_exec_time "
                "FROM monitor.v_statement_stats "
                "WHERE snapshot_id = (SELECT max(snapshot_id) FROM monitor.v_statement_stats) "
                "ORDER BY total_exec_time_delta DESC NULLS LAST, queryid LIMIT 20",
            )

        if out["available"].get("monitor.v_hourly_rollup"):
            out["hourly_rollup"] = _q(
                conn,
                "SELECT hour, snapshots, max_lock_edges, max_sessions, "
                "xact_commit_delta_sum, blks_hit_ratio, alerts_fired "
                "FROM monitor.v_hourly_rollup ORDER BY hour DESC NULLS LAST LIMIT 48",
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


ALERT_DB_RULES = {"connection_saturation", "cache_hit_ratio"}
ALERT_PID_RULES = {"idle_in_transaction", "long_active_query"}


def alert_target(rule_name, blocked_pid, blocker_pid, alert_id, dead_tables) -> str:
    """Rule-aware human target for one alert row (never raises)."""
    rule = str(rule_name) if rule_name is not None and not pd.isna(rule_name) else ""

    def as_int(value):
        return None if value is None or pd.isna(value) else int(value)

    blocked, blocker = as_int(blocked_pid), as_int(blocker_pid)
    alert = as_int(alert_id)

    if rule == "blocked_session":
        if blocked is not None and blocker is not None:
            return f"pid {blocked} blocked by {blocker}"
        return f"pid {blocked}" if blocked is not None else "—"
    if rule in ALERT_PID_RULES:
        return f"pid {blocked}" if blocked is not None else "—"
    if rule == "dead_tuple_risk":
        relname = dead_tables.get(alert) if alert is not None else None
        if relname:
            return f"table {relname}"
        return f"table {blocked}" if blocked is not None else "table —"
    if rule in ALERT_DB_RULES:
        return "database"
    # Unknown rule: infer from the PID convention used by v_alerts.
    if blocked is not None and blocker is not None and blocker not in (0, blocked):
        return f"pid {blocked} blocked by {blocker}"
    if blocked is not None and blocked != 0:
        return f"pid {blocked}"
    return "database"


def with_alert_target(df: pd.DataFrame, dead_tables: dict[int, str]) -> pd.DataFrame:
    """Return a copy of an alerts frame with a `target` column after rule_name."""
    if not len(df):
        return df.assign(target=pd.Series(dtype="object"))
    out = df.copy()
    targets = [
        alert_target(row.rule_name, row.blocked_pid, row.blocker_pid, row.alert_id, dead_tables)
        for row in df.itertuples()
    ]
    out.insert(out.columns.get_loc("rule_name") + 1, "target", targets)
    return out


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
    dead_tables = data.get("dead_table_names", {})
    open_alerts = with_alert_target(data["alerts_open"], dead_tables)
    resolved = with_alert_target(data["alerts_resolved"], dead_tables)

    known_rules = sorted(
        {str(x) for x in open_alerts["rule_name"].dropna()}
        | {str(x) for x in resolved["rule_name"].dropna()}
    )
    picked_rules = known_rules
    if known_rules:
        picked_rules = st.multiselect(
            "Filter alerts by rule_name",
            known_rules,
            default=known_rules,
            key="alert_rule_filter",
            help="Applies to both the open and resolved alert tables below.",
        )
    open_view = open_alerts[open_alerts["rule_name"].astype(str).isin(picked_rules)]
    resolved_view = resolved[resolved["rule_name"].astype(str).isin(picked_rules)]

    a1, a2 = st.columns([1, 1])
    a1.metric("Open alerts", len(open_view))
    a2.metric("Resolved alerts (shown)", len(resolved_view))

    if len(open_view):
        st.error(f"{len(open_view)} open alert(s) — blocking threshold currently exceeded.")
        show = open_view.assign(
            fired_at=open_view["fired_at"].map(ct),
            first_seen_at=open_view["first_seen_at"].map(ct),
            eligible_at=open_view["eligible_at"].map(ct),
            notified_at=open_view["notified_at"].map(ct),
        )
        styled = show.style.apply(
            lambda _row: ["background-color: #5c1a1a"] * len(_row), axis=1
        )
        st.dataframe(styled, width="stretch", hide_index=True)
    elif known_rules and len(picked_rules) != len(known_rules):
        st.success("No open alerts match the selected rule filter.")
    else:
        st.success("No open alerts.")

    st.markdown("**Resolved alerts**")
    if len(resolved_view):
        show = resolved_view.assign(
            fired_at=resolved_view["fired_at"].map(ct),
            resolved_at=resolved_view["resolved_at"].map(ct),
            notified_at=resolved_view["notified_at"].map(ct),
            resolve_notified_at=resolved_view["resolve_notified_at"].map(ct),
        )
        st.dataframe(show, width="stretch", hide_index=True)
    else:
        st.caption("No resolved alerts match the current filter.")

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

    # ---------------- 5. table health ----------------
    st.subheader("5 · Table health")
    if not data["available"].get("monitor.v_table_stats"):
        st.info(
            "Table health: `monitor.v_table_stats` **not available in this database** — "
            "the optional history views have not been installed here."
        )
    else:
        tables = data["table_stats_latest"]
        if len(tables):
            tables = tables.copy()
            tables["table"] = tables["schemaname"].astype(str) + "." + tables["relname"].astype(str)
            tables["dead_pct"] = tables["dead_ratio"].map(
                lambda v: f"{float(v):.2%}" if pd.notna(v) else "—"
            )
            t1, t2, t3 = st.columns(3)
            t1.metric("Tables in latest snapshot", len(tables))
            t2.metric("Dead tuples (latest)", f"{int(tables['n_dead_tup'].fillna(0).sum()):,}")
            t3.metric("Max dead ratio", f"{float(tables['dead_ratio'].fillna(0).max()):.2%}")
            st.caption(
                "Latest snapshot per table — live/dead tuples, dead ratio, "
                "sequential vs index scans."
            )
            st.dataframe(
                tables[["table", "n_live_tup", "n_dead_tup", "dead_pct", "seq_scan", "idx_scan"]],
                width="stretch",
                hide_index=True,
            )

            hist = data["table_stats_history"]
            if len(hist):
                hist = hist.copy()
                hist["table"] = hist["schemaname"].astype(str) + "." + hist["relname"].astype(str)
                top_tables = (
                    tables.sort_values("n_dead_tup", ascending=False)["table"].head(6).tolist()
                )
                hist = hist[hist["table"].isin(top_tables)]
                pivot = (
                    hist.pivot_table(
                        index=["snapshot_id", "captured_at"],
                        columns="table",
                        values="n_dead_tup",
                        aggfunc="max",
                    )
                    .reset_index()
                    .sort_values("snapshot_id")
                )
                chart_cols = [c for c in top_tables if c in pivot.columns]
                if chart_cols:
                    pivot["snapshot"] = [
                        f"#{int(r.snapshot_id)} {ct(r.captured_at)[11:19]}"
                        for r in pivot.itertuples()
                    ]
                    st.caption("Dead tuples over time (six tables with the most dead tuples)")
                    st.line_chart(pivot.set_index("snapshot")[chart_cols], height=240)
        else:
            st.info("`monitor.v_table_stats` exists but has no samples yet.")

    # ---------------- 6. top statements ----------------
    st.subheader("6 · Top statements by total execution time")
    if not data["available"].get("monitor.v_statement_stats"):
        st.info(
            "Top statements: `monitor.v_statement_stats` **not available in this database** — "
            "the optional history views have not been installed here."
        )
    else:
        stmt = data["statement_stats"]
        if len(stmt):
            show = stmt.copy()
            show["queryid"] = show["queryid"].map(lambda v: str(int(v)) if pd.notna(v) else "—")
            show["calls_delta"] = show["calls_delta"].map(
                lambda v: f"{int(v):,}" if pd.notna(v) else "unknown"
            )
            show["total_exec_time_delta"] = show["total_exec_time_delta"].map(
                lambda v: f"{float(v):,.1f} ms" if pd.notna(v) else "unknown"
            )
            show["mean_exec_time"] = show["mean_exec_time"].map(
                lambda v: f"{float(v):,.3f} ms" if pd.notna(v) else "—"
            )
            st.caption(
                "Latest snapshot only, ordered by `total_exec_time_delta`. "
                "`queryid` only — the contract stores no SQL text."
            )
            st.dataframe(show, width="stretch", hide_index=True)

            chart = stmt.dropna(subset=["total_exec_time_delta"]).head(10).copy()
            if len(chart):
                chart["label"] = chart["queryid"].map(lambda v: f"q{int(v)}")
                st.caption("total_exec_time_delta (ms), top 10 queryids")
                st.bar_chart(chart.set_index("label")["total_exec_time_delta"], height=220)
        else:
            st.info("`monitor.v_statement_stats` exists but has no samples in its latest snapshot.")

    # ---------------- 7. hourly rollup ----------------
    st.subheader("7 · Hourly rollup")
    if not data["available"].get("monitor.v_hourly_rollup"):
        st.info(
            "Hourly rollup: `monitor.v_hourly_rollup` **not available in this database** — "
            "the optional history views have not been installed here."
        )
    else:
        roll = data["hourly_rollup"]
        if len(roll):
            show = roll.copy()
            show["hour"] = show["hour"].map(ct)
            show["blks_hit_ratio"] = show["blks_hit_ratio"].map(
                lambda v: f"{float(v):.2%}" if pd.notna(v) else "unknown"
            )
            st.caption(
                "Per-hour rollup (most recent first): snapshots, peak lock edges/sessions, "
                "commit delta sum, block cache hit ratio, alerts fired."
            )
            st.dataframe(show, width="stretch", hide_index=True)
        else:
            st.info("`monitor.v_hourly_rollup` exists but has no hourly rows yet.")

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
