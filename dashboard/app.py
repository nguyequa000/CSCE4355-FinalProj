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
# page styling + sidebar
# --------------------------------------------------------------------------- #
CSS = """
<style>
#MainMenu, header[data-testid="stHeader"], footer {visibility: hidden;}
.block-container {padding-top: 1.3rem; padding-bottom: 2.5rem; max-width: 1300px;}
div[data-testid="stMetric"] {
  border: 1px solid rgba(128,128,128,.22);
  border-radius: 12px;
  padding: 12px 16px;
  background: rgba(128,128,128,.06);
}
div[data-testid="stMetricLabel"] p {font-size: .82rem; opacity: .78;}
.pill {display:inline-block; padding:2px 10px; border-radius:999px; font-size:.78rem; font-weight:650; vertical-align:middle;}
.pill-live {background:rgba(35,134,54,.15); color:#2ea043;}
.pill-warn {background:rgba(210,153,34,.16); color:#bf8700;}
.pill-down {background:rgba(207,34,46,.15); color:#d1242f;}
.conn {border:1px solid rgba(128,128,128,.22); border-radius:10px; padding:8px 12px; margin:4px 0 8px;}
.conn div {display:flex; justify-content:space-between; gap:12px; font-size:.85rem; padding:2px 0;}
.conn span {opacity:.68;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

env = load_env()
DBNAME = os.environ.get("DASH_DB") or env.get("PGDATABASE", "shopdb")
INTERVAL = int(os.environ.get("COLLECT_INTERVAL_SECONDS") or env.get("COLLECT_INTERVAL_SECONDS") or 60)
STALE_AFTER = 2.5 * INTERVAL
PGHOST = env.get("PGHOST", "127.0.0.1")
PGPORT = env.get("PGPORT", "55432")

with st.sidebar:
    st.markdown("### Controls")
    window = st.slider(
        "History window", 3, 60, DEFAULT_WINDOW, 1,
        help="How many recent snapshots the charts and lock history cover.",
    )
    auto = st.toggle(
        f"Live refresh ({REFRESH_SECONDS}s)", value=True,
        help="Re-runs the page on a timer. Turn it off to freeze a view.",
    )
    if st.button("Refresh now", type="primary"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.markdown("### Connection")
    st.markdown(
        f"""
        <div class="conn">
          <div><span>Database</span><b>{DBNAME}</b></div>
          <div><span>Host</span><b>{PGHOST}:{PGPORT}</b></div>
          <div><span>Role</span><b>monitor_reader</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("Read-only: SELECT on the sanitized `monitor.v_*` views only.")

    with st.expander("Create demo traffic"):
        st.markdown(
            "Use this when the dashboard looks quiet. It opens a real row lock "
            "for two minutes so alerts fire and then resolve:\n\n"
            "```bash\ndocker compose run --rm demo\n```"
        )

    with st.expander("Troubleshooting"):
        st.markdown(
            "- **Data looks stale** → `docker compose ps`; collection is the "
            "in-database `pg_cron` job, so the postgres container must be healthy.\n"
            "- **Dashboard is down** → `docker compose logs dashboard --tail 100`.\n"
            "- **Start over** → `docker compose down -v && docker compose up -d --build` "
            "(this deletes all history)."
        )

    st.divider()
    st.caption(
        f"Times: America/Chicago (CT). A snapshot older than 2.5 × {INTERVAL}s "
        f"({STALE_AFTER:.0f}s) is flagged stale."
    )


# --------------------------------------------------------------------------- #
# small chart helpers
# --------------------------------------------------------------------------- #
def lock_series(locks: pd.DataFrame) -> pd.Series:
    edges = (
        locks.groupby(["snapshot_id", "captured_at"], as_index=False)
        .size()
        .rename(columns={"size": "lock_edges"})
        .sort_values("snapshot_id")
    )
    edges["snapshot"] = [
        f"#{int(r.snapshot_id)} {ct(r.captured_at)[11:19]}" for r in edges.itertuples()
    ]
    return edges.set_index("snapshot")["lock_edges"]


def session_frame(sessions: pd.DataFrame) -> pd.DataFrame:
    pivot = (
        sessions.pivot_table(
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
    return pivot.set_index("snapshot").drop(columns=["snapshot_id", "captured_at"])


RULE_HELP = pd.DataFrame(
    [
        ("blocked_session", "A session has been waiting on another session's lock past the threshold.", "critical"),
        ("idle_in_transaction", "A transaction has been idle (holding locks) past the threshold.", "warning"),
        ("long_active_query", "A query has been running longer than the threshold.", "warning"),
        ("connection_saturation", "The database is using a high share of its allowed connections.", "critical"),
        ("cache_hit_ratio", "The buffer cache hit ratio fell below the configured floor.", "warning"),
        ("dead_tuple_risk", "A table has accumulated too many dead tuples; VACUUM is due.", "warning"),
    ],
    columns=["rule", "what it means", "default severity"],
)


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #
def render() -> None:
    try:
        data = load_data(DBNAME, window)
    except Exception as exc:  # noqa: BLE001 - surface any driver/auth error plainly
        st.markdown("## Database health at a glance")
        st.error("The dashboard can't reach the monitoring database right now.")
        st.markdown(
            "Check that the Docker stack is running (`docker compose ps`), then press "
            "**Refresh now** in the sidebar."
        )
        with st.expander("Technical details"):
            st.exception(exc)
        return

    who = data["who"].iloc[0]
    snaps = data["snapshot"]
    latest_id = num(snaps["snapshot_id"].iloc[0]) if len(snaps) else None
    latest_ts = snaps["captured_at"].iloc[0] if len(snaps) else None
    age = age_seconds(latest_ts)
    stale = age is not None and age > STALE_AFTER

    open_alerts = with_alert_target(data["alerts_open"], data.get("dead_table_names", {}))
    resolved = with_alert_target(data["alerts_resolved"], data.get("dead_table_names", {}))
    blockers = data["blockers"]
    tables_latest = data["table_stats_latest"]

    st.markdown("## Database health at a glance")
    st.caption(
        f"`{who['dbname']}` · read-only session as `{who['connected_role']}` · "
        "all times America/Chicago (CT)"
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Open alerts", len(open_alerts), help="Alerts currently firing.")
    m2.metric("Waiting on locks", len(blockers), help="Blocked/blocker pairs in the latest snapshot.")
    m3.metric("Snapshot age", f"{age:.0f}s" if age is not None else "—", help=f"Collector cadence: {INTERVAL}s.")
    m4.metric(
        "Tables sampled",
        len(tables_latest) if data["available"].get("monitor.v_table_stats") else "—",
        help="Tables captured in the latest history snapshot.",
    )

    if latest_id is None:
        st.warning(
            "Waiting for the first snapshot. The in-database collector runs every "
            f"{INTERVAL}s — this page will fill in automatically."
        )
    elif stale:
        st.error(
            f"Data is stale: snapshot #{latest_id} is {age:.0f}s old "
            f"(limit {STALE_AFTER:.0f}s). Check `docker compose ps` — the collector "
            "runs inside the postgres container."
        )
    else:
        st.success(
            f"Live · snapshot #{latest_id} captured {ct(latest_ts)} ({age:.0f}s ago). "
            f"Server clock {ct(who['server_now'])}."
        )

    tab_overview, tab_alerts, tab_locks, tab_perf, tab_help = st.tabs(
        ["Overview", "Alerts", "Locks & sessions", "Performance", "Help"]
    )

    # ------------------------------------------------------------------ #
    # Overview
    # ------------------------------------------------------------------ #
    with tab_overview:
        left, right = st.columns([1.2, 1])
        with left:
            st.markdown("#### What needs attention")
            if len(open_alerts):
                st.error(f"{len(open_alerts)} alert(s) open")
                show = open_alerts.assign(fired_at=open_alerts["fired_at"].map(ct))
                st.dataframe(
                    show[["rule_name", "severity", "target", "fired_at", "observed_seconds"]],
                    hide_index=True, width="stretch",
                )
            else:
                st.success("All clear — no open alerts.")

            st.markdown("#### Blocking right now")
            if len(blockers):
                st.warning(f"{len(blockers)} session(s) waiting on a lock")
                show = blockers.assign(
                    captured_at=blockers["captured_at"].map(ct),
                    wait_started=blockers["wait_started"].map(ct),
                )
                st.dataframe(
                    show[["blocked_pid", "blocker_pid", "blocked_user",
                          "blocker_state", "waited_seconds", "wait_started"]],
                    hide_index=True, width="stretch",
                )
            else:
                st.success("No sessions are waiting on a lock.")

        with right:
            st.markdown("#### Recent activity")
            st.caption("Lock edges per snapshot")
            if len(data["locks"]):
                st.bar_chart(lock_series(data["locks"]), height=200)
            else:
                st.info("No lock waits in this window. Use **Create demo traffic** in the sidebar.")

            st.caption("Last resolved alerts")
            if len(resolved):
                recent = resolved.head(5).assign(resolved_at=resolved.head(5)["resolved_at"].map(ct))
                st.dataframe(
                    recent[["rule_name", "target", "resolved_at", "observed_seconds"]],
                    hide_index=True, width="stretch",
                )
            else:
                st.caption("Nothing resolved yet.")

    # ------------------------------------------------------------------ #
    # Alerts
    # ------------------------------------------------------------------ #
    with tab_alerts:
        known_rules = sorted(
            {str(x) for x in open_alerts["rule_name"].dropna()}
            | {str(x) for x in resolved["rule_name"].dropna()}
        )
        picked = (
            st.multiselect(
                "Filter by rule", known_rules, default=known_rules,
                help="Applies to both tables on this tab.",
            )
            if known_rules else known_rules
        )
        open_view = open_alerts[open_alerts["rule_name"].astype(str).isin(picked)]
        resolved_view = resolved[resolved["rule_name"].astype(str).isin(picked)]

        a1, a2 = st.columns(2)
        a1.metric("Open alerts", len(open_view))
        a2.metric("Resolved alerts (shown)", len(resolved_view))

        st.markdown("#### Open alerts")
        if len(open_view):
            show = open_view.assign(
                fired_at=open_view["fired_at"].map(ct),
                notified_at=open_view["notified_at"].map(ct),
            )
            st.dataframe(
                show[["alert_id", "rule_name", "severity", "target", "fired_at",
                      "observed_seconds", "notified_at"]],
                hide_index=True, width="stretch",
            )
            st.download_button(
                "Download open alerts (CSV)",
                show.to_csv(index=False).encode("utf-8"),
                "open_alerts.csv", "text/csv",
            )
        else:
            st.success("No open alerts right now.")

        st.markdown("#### Resolved alerts")
        if len(resolved_view):
            show = resolved_view.assign(
                fired_at=resolved_view["fired_at"].map(ct),
                resolved_at=resolved_view["resolved_at"].map(ct),
                notified_at=resolved_view["notified_at"].map(ct),
            )
            st.dataframe(
                show[["alert_id", "rule_name", "severity", "target", "fired_at",
                      "resolved_at", "observed_seconds"]],
                hide_index=True, width="stretch",
            )
            st.download_button(
                "Download resolved alerts (CSV)",
                show.to_csv(index=False).encode("utf-8"),
                "resolved_alerts.csv", "text/csv",
            )
        else:
            st.info("No resolved alerts match the current filter.")

    # ------------------------------------------------------------------ #
    # Locks & sessions
    # ------------------------------------------------------------------ #
    with tab_locks:
        st.markdown("#### Blocking at the latest snapshot")
        if len(blockers):
            show = blockers.assign(
                captured_at=blockers["captured_at"].map(ct),
                wait_started=blockers["wait_started"].map(ct),
            )
            st.dataframe(
                show[["blocked_pid", "blocker_pid", "blocked_user", "blocker_user",
                      "blocked_app", "blocker_state", "waited_seconds", "wait_started"]],
                hide_index=True, width="stretch",
            )
        else:
            st.success("No blocking at the latest snapshot.")

        st.markdown(f"#### Lock history — last {len(snaps)} snapshots")
        if len(data["locks"]):
            st.bar_chart(lock_series(data["locks"]), height=220)
            show = data["locks"].assign(
                captured_at=data["locks"]["captured_at"].map(ct),
                wait_started=data["locks"]["wait_started"].map(ct),
            )
            st.dataframe(
                show[["snapshot_id", "captured_at", "blocked_pid", "blocker_pid",
                      "blocked_user", "blocker_user", "blocked_app", "waited_seconds"]],
                hide_index=True, width="stretch",
            )
        else:
            st.info("No lock waits recorded in the selected window.")

        st.markdown("#### Session states")
        if len(data["sessions"]):
            st.line_chart(session_frame(data["sessions"]), height=240)
        else:
            st.info("No session samples in the selected window.")

    # ------------------------------------------------------------------ #
    # Performance
    # ------------------------------------------------------------------ #
    with tab_perf:
        stats = data["dbstats"]
        st.markdown("#### Database counters")
        if len(stats):
            delta_cols = ["xact_commit_delta", "xact_rollback_delta",
                          "blks_read_delta", "blks_hit_delta"]
            shown = stats.assign(
                captured_at=stats["captured_at"].map(ct),
                **{
                    c: stats[c].map(lambda v: "unknown" if pd.isna(v) else f"{int(v):,}")
                    for c in delta_cols
                },
            )
            st.caption("Deltas cover the interval since the previous sample; `unknown` means no comparable sample (not zero).")
            st.dataframe(
                shown[["snapshot_id", "captured_at", "numbackends", "deadlocks",
                       *delta_cols]],
                hide_index=True, width="stretch",
            )
            chart = stats.dropna(subset=["xact_commit_delta"]).copy()
            if len(chart):
                chart["snapshot"] = [
                    f"#{int(r.snapshot_id)} {ct(r.captured_at)[11:19]}" for r in chart.itertuples()
                ]
                st.caption("Transactions committed per interval")
                st.bar_chart(chart.set_index("snapshot")["xact_commit_delta"], height=200)
        else:
            st.info("No database counter samples in the selected window.")

        st.markdown("#### Table health")
        if not data["available"].get("monitor.v_table_stats"):
            st.info("Table health views are not installed in this database.")
        elif len(tables_latest):
            tbl = tables_latest.copy()
            tbl["table"] = tbl["schemaname"].astype(str) + "." + tbl["relname"].astype(str)
            tbl["dead_pct"] = tbl["dead_ratio"].map(
                lambda v: f"{float(v):.2%}" if pd.notna(v) else "—"
            )
            t1, t2, t3 = st.columns(3)
            t1.metric("Tables in latest snapshot", len(tbl))
            t2.metric("Dead tuples", f"{int(tbl['n_dead_tup'].fillna(0).sum()):,}")
            t3.metric("Max dead ratio", f"{float(tbl['dead_ratio'].fillna(0).max()):.2%}")
            st.dataframe(
                tbl[["table", "n_live_tup", "n_dead_tup", "dead_pct", "seq_scan", "idx_scan"]],
                hide_index=True, width="stretch",
            )

            hist = data["table_stats_history"]
            if len(hist):
                hist = hist.copy()
                hist["table"] = hist["schemaname"].astype(str) + "." + hist["relname"].astype(str)
                top_tables = tbl.sort_values("n_dead_tup", ascending=False)["table"].head(6).tolist()
                hist = hist[hist["table"].isin(top_tables)]
                pivot = (
                    hist.pivot_table(
                        index=["snapshot_id", "captured_at"], columns="table",
                        values="n_dead_tup", aggfunc="max",
                    )
                    .reset_index().sort_values("snapshot_id")
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
            st.info("No table samples yet — the next collector run will fill this in.")

        st.markdown("#### Top statements by total execution time")
        if not data["available"].get("monitor.v_statement_stats"):
            st.info("Statement history views are not installed in this database.")
        elif len(data["statement_stats"]):
            stmt = data["statement_stats"].copy()
            stmt["queryid"] = stmt["queryid"].map(lambda v: str(int(v)) if pd.notna(v) else "—")
            stmt["calls_delta"] = stmt["calls_delta"].map(
                lambda v: f"{int(v):,}" if pd.notna(v) else "unknown"
            )
            stmt["total_exec_time_delta"] = stmt["total_exec_time_delta"].map(
                lambda v: f"{float(v):,.1f} ms" if pd.notna(v) else "unknown"
            )
            stmt["mean_exec_time"] = stmt["mean_exec_time"].map(
                lambda v: f"{float(v):,.3f} ms" if pd.notna(v) else "—"
            )
            st.caption("Latest snapshot only, ordered by total execution time. Query IDs only — no SQL text is stored.")
            st.dataframe(
                stmt[["queryid", "calls_delta", "total_exec_time_delta", "mean_exec_time"]],
                hide_index=True, width="stretch",
            )
            chart = data["statement_stats"].dropna(subset=["total_exec_time_delta"]).head(10).copy()
            if len(chart):
                chart["label"] = chart["queryid"].map(lambda v: f"q{int(v)}")
                st.bar_chart(chart.set_index("label")["total_exec_time_delta"], height=220)
        else:
            st.info("No statement samples in the latest snapshot.")

        st.markdown("#### Hourly rollup")
        if not data["available"].get("monitor.v_hourly_rollup"):
            st.info("Hourly rollup is not installed in this database.")
        elif len(data["hourly_rollup"]):
            roll = data["hourly_rollup"].copy()
            roll["hour"] = roll["hour"].map(ct)
            roll["blks_hit_ratio"] = roll["blks_hit_ratio"].map(
                lambda v: f"{float(v):.2%}" if pd.notna(v) else "unknown"
            )
            st.dataframe(roll, hide_index=True, width="stretch")
        else:
            st.info("No hourly rows yet.")

    # ------------------------------------------------------------------ #
    # Help
    # ------------------------------------------------------------------ #
    with tab_help:
        st.markdown(
            "#### What this page is\n"
            "A read-only view of the `monitor` schema. The database collects one "
            "snapshot a minute; this page only ever runs SELECTs against sanitized "
            "views, so it cannot change data or read customer rows."
        )
        st.markdown(
            "#### Alert rules\n"
            "Each rule opens one alert, waits for its threshold, then resolves the "
            "same alert when the condition clears."
        )
        st.dataframe(RULE_HELP, hide_index=True, width="stretch")
        st.markdown(
            "#### Run the demo\n"
            "```bash\n"
            "# 1. open the dashboard\n"
            "bash scripts/start_dashboard.sh     # or: docker compose up -d --build\n\n"
            "# 2. in a second terminal, create a real lock wait\n"
            "docker compose run --rm demo\n"
            "```\n"
            "The demo holds a row lock for two minutes. Alerts appear under **Alerts** "
            "while it is held and resolve on the snapshot after it releases."
        )
        st.markdown(
            "#### Under the hood\n"
            "- `pg_cron` calls `monitor.capture_snapshot()` every 60 s inside postgres.\n"
            "- The function writes the snapshot, session, lock and counter rows and "
            "evaluates all six rules in the same transaction.\n"
            "- The notifier container prints fired/resolved alerts and stamps delivery.\n"
            "- The dashboard reads only `monitor.v_*` as `monitor_reader`."
        )

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
