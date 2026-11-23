"""Static HTML fallback for the CSCE 4355 monitoring dashboard.

If Streamlit cannot start during the demo, this renders the same reader views
(``monitor.v_*`` as ``monitor_reader``) to one self-contained HTML file.

    .venv/Scripts/python.exe dashboard/static_report.py \
        --out docs/results/dashboard_snapshot.html [--db shopdb] [--window 12]

Every statement is a SELECT on a session with default_transaction_read_only=on.
"""
from __future__ import annotations

import argparse
import html
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect, load_env  # noqa: E402

CT = ZoneInfo("America/Chicago")


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
    return (datetime.now(timezone.utc) - ts).total_seconds()


def fmt_table(df: pd.DataFrame, time_cols=(), unknown_cols=(), bar_col: str | None = None) -> str:
    df = df.copy()
    for c in time_cols:
        if c in df.columns:
            df[c] = df[c].map(ct)
    for c in unknown_cols:
        if c in df.columns:
            df[c] = df[c].map(lambda v: "unknown" if pd.isna(v) else int(v))
    if bar_col and bar_col in df.columns and len(df):
        top = max(1, int(pd.to_numeric(df[bar_col], errors="coerce").fillna(0).max()))
        df[bar_col] = [
            f'<div class="bar"><span style="width:{max(0, int(v)) / top * 100:.0f}%"></span>'
            f"<b>{int(v)}</b></div>"
            for v in pd.to_numeric(df[bar_col], errors="coerce").fillna(0)
        ]
    if df.empty:
        return '<p class="empty">no rows</p>'
    return df.to_html(index=False, escape=False, border=0, classes="tbl")


def bars(values: pd.Series, labels: pd.Series) -> str:
    if len(values) == 0:
        return '<p class="empty">no data</p>'
    top = max(1, int(values.max()))
    rows = []
    for v, lab in zip(values, labels):
        pct = max(1, int(float(v) / top * 100))
        rows.append(
            f'<div class="hbar"><span class="lab">{html.escape(str(lab))}</span>'
            f'<span class="track"><i style="width:{pct}%"></i></span><b>{int(v)}</b></div>'
        )
    return "".join(rows)


def line_chart(df: pd.DataFrame, labels: pd.Series, height: int = 180) -> str:
    """Small dependency-free SVG line chart: one line per column."""
    if df.empty or not len(labels):
        return '<p class="empty">no data</p>'
    width, pad_l, pad_r, pad_t, pad_b = 760, 44, 12, 16, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    values = df.fillna(0).to_numpy(dtype=float)
    top = max(1.0, float(values.max()))
    n = max(1, len(df) - 1)
    palette = ["#3f6fb5", "#b5533f", "#3f8f5a", "#8a5fb5", "#b5953f", "#3f8fb5"]
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img">']
    for frac in (0.25, 0.5, 0.75, 1.0):
        y = pad_t + plot_h - frac * plot_h
        parts.append(f'<line x1="{pad_l}" y1="{y:.0f}" x2="{width - pad_r}" y2="{y:.0f}" stroke="#e3e7ec"/>')
        parts.append(
            f'<text x="{pad_l - 6}" y="{y + 4:.0f}" text-anchor="end" font-size="10" '
            f'fill="#68717f">{int(top * frac)}</text>'
        )
    for i, col in enumerate(df.columns):
        color = palette[i % len(palette)]
        pts = []
        for j in range(len(df)):
            x = pad_l + (plot_w * j / n)
            y = pad_t + plot_h - (values[j, i] / top * plot_h)
            pts.append(f"{x:.1f},{y:.1f}")
        poly = " ".join(pts)
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{poly}"/>')
        for j, point in enumerate(pts):
            x, y = point.split(",")
            parts.append(
                f'<circle cx="{x}" cy="{y}" r="2.5" fill="{color}">'
                f"<title>{html.escape(str(col))}: {values[j, i]:.0f}</title></circle>"
            )
        lx = pad_l + i * 150
        parts.append(f'<rect x="{lx}" y="2" width="10" height="10" fill="{color}"/>')
        parts.append(
            f'<text x="{lx + 14}" y="11" font-size="10" fill="#4a5464">'
            f"{html.escape(str(col))}</text>"
        )
    for j in sorted({0, len(df) // 2, len(df) - 1}):
        x = pad_l + (plot_w * j / n)
        anchor = "start" if j == 0 else ("end" if j == len(df) - 1 else "middle")
        parts.append(
            f'<text x="{x:.0f}" y="{height - 8}" text-anchor="{anchor}" font-size="10" '
            f'fill="#68717f">{html.escape(str(labels.iloc[j]))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


ALERT_DB_RULES = {"connection_saturation", "cache_hit_ratio"}
ALERT_PID_RULES = {"idle_in_transaction", "long_active_query"}


def alert_target(rule_name, blocked_pid, blocker_pid, alert_id, dead_tables) -> str:
    """Rule-aware target for one alert row (same mapping as dashboard/app.py)."""
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
            return f"table {html.escape(str(relname))}"
        return f"table {blocked}" if blocked is not None else "table —"
    if rule in ALERT_DB_RULES:
        return "database"
    if blocked is not None and blocker is not None and blocker not in (0, blocked):
        return f"pid {blocked} blocked by {blocker}"
    if blocked is not None and blocked != 0:
        return f"pid {blocked}"
    return "database"


def add_alert_targets(df: pd.DataFrame, dead_tables: dict[int, str]) -> pd.DataFrame:
    if not len(df):
        return df.assign(target=pd.Series(dtype="object"))
    out = df.copy()
    out.insert(
        out.columns.get_loc("rule_name") + 1,
        "target",
        [
            alert_target(r.rule_name, r.blocked_pid, r.blocker_pid, r.alert_id, dead_tables)
            for r in df.itertuples()
        ],
    )
    return out


def main() -> int:
    env = load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("DASH_DB") or env.get("PGDATABASE", "shopdb"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "results" / "dashboard_snapshot.html"))
    ap.add_argument("--window", type=int, default=12)
    args = ap.parse_args()

    interval = int(env.get("COLLECT_INTERVAL_SECONDS") or 60)
    stale_after = 2.5 * interval

    with connect(
        "monitor_reader",
        dbname=args.db,
        connect_timeout=5,
        options="-c default_transaction_read_only=on -c statement_timeout=8000",
    ) as conn:
        def q(sql, params=None):
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])

        who = q("SELECT current_user AS role, current_database() AS db, now() AS server_now").iloc[0]
        snaps = q(
            "SELECT snapshot_id, captured_at FROM monitor.v_snapshot "
            "ORDER BY snapshot_id DESC LIMIT %s", (args.window,)
        )
        ids = [int(x) for x in snaps["snapshot_id"]] or [0]
        locks = q(
            "SELECT snapshot_id, captured_at, blocked_pid, blocker_pid, blocked_user, "
            "blocker_user, blocked_app, blocker_app, blocker_state, wait_started, waited_seconds "
            "FROM monitor.v_lock_history WHERE snapshot_id = ANY(%s) "
            "ORDER BY snapshot_id, waited_seconds DESC", (ids,)
        )
        blockers = q(
            "SELECT snapshot_id, captured_at, blocked_pid, blocker_pid, blocked_user, "
            "blocker_user, blocked_app, blocker_app, blocker_state, wait_started, waited_seconds "
            "FROM monitor.v_current_blockers ORDER BY waited_seconds DESC NULLS LAST"
        )
        sessions = q(
            "SELECT snapshot_id, captured_at, state, count(*) AS sessions "
            "FROM monitor.v_session_history WHERE snapshot_id = ANY(%s) GROUP BY 1,2,3 "
            "ORDER BY 1,3", (ids,)
        )
        stats = q(
            "SELECT snapshot_id, captured_at, datname, numbackends, deadlocks, "
            "xact_commit_delta, xact_rollback_delta, blks_read_delta, blks_hit_delta "
            "FROM monitor.v_db_stats WHERE snapshot_id = ANY(%s) ORDER BY snapshot_id", (ids,)
        )
        open_a = q(
            "SELECT alert_id, rule_name, severity, blocked_pid, blocker_pid, first_seen_at, "
            "eligible_at, fired_at, observed_seconds, notified_at FROM monitor.v_alerts "
            "WHERE status = 'open' ORDER BY fired_at DESC"
        )
        res_a = q(
            "SELECT alert_id, rule_name, severity, blocked_pid, blocker_pid, fired_at, "
            "resolved_at, observed_seconds FROM monitor.v_alerts WHERE status = 'resolved' "
            "ORDER BY resolved_at DESC NULLS LAST LIMIT 25"
        )

        # Optional history views (owned by another lane): probe with to_regclass
        # before selecting, so this fallback works on databases without them.
        available = {
            name: bool(q("SELECT to_regclass(%s) AS reg", (name,)).iloc[0]["reg"])
            for name in (
                "monitor.v_table_stats",
                "monitor.v_statement_stats",
                "monitor.v_hourly_rollup",
            )
        }

        table_latest = pd.DataFrame()
        table_history = pd.DataFrame()
        statement_stats = pd.DataFrame()
        hourly_rollup = pd.DataFrame()
        dead_names: dict[int, str] = {}

        if available["monitor.v_table_stats"]:
            table_latest = q(
                "SELECT snapshot_id, captured_at, schemaname, relname, n_live_tup, "
                "n_dead_tup, dead_ratio, seq_scan, idx_scan FROM monitor.v_table_stats "
                "WHERE snapshot_id = (SELECT max(snapshot_id) FROM monitor.v_table_stats) "
                "ORDER BY n_dead_tup DESC NULLS LAST, schemaname, relname"
            )
            table_history = q(
                "SELECT snapshot_id, captured_at, schemaname, relname, n_dead_tup "
                "FROM monitor.v_table_stats WHERE snapshot_id = ANY(%s) "
                "ORDER BY snapshot_id, schemaname, relname",
                (ids,),
            )
            try:
                refs = q(
                    "SELECT a.alert_id, ts.relname FROM monitor.v_alerts a "
                    "LEFT JOIN pg_class c ON c.oid::bigint = a.blocked_pid::bigint "
                    "LEFT JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN LATERAL ("
                    "  SELECT t.relname FROM monitor.v_table_stats t "
                    "  WHERE t.snapshot_id = (SELECT max(snapshot_id) FROM monitor.v_table_stats) "
                    "    AND t.schemaname = n.nspname AND t.relname = c.relname "
                    "  LIMIT 1"
                    ") ts ON true WHERE a.rule_name = 'dead_tuple_risk'"
                )
                dead_names = {
                    int(row.alert_id): str(row.relname)
                    for row in refs.itertuples()
                    if row.relname is not None and not pd.isna(row.relname)
                }
            except Exception:  # optional label enrichment only
                dead_names = {}

        if available["monitor.v_statement_stats"]:
            statement_stats = q(
                "SELECT snapshot_id, captured_at, queryid, calls_delta, "
                "total_exec_time_delta, mean_exec_time FROM monitor.v_statement_stats "
                "WHERE snapshot_id = (SELECT max(snapshot_id) FROM monitor.v_statement_stats) "
                "ORDER BY total_exec_time_delta DESC NULLS LAST, queryid LIMIT 20"
            )

        if available["monitor.v_hourly_rollup"]:
            hourly_rollup = q(
                "SELECT hour, snapshots, max_lock_edges, max_sessions, "
                "xact_commit_delta_sum, blks_hit_ratio, alerts_fired "
                "FROM monitor.v_hourly_rollup ORDER BY hour DESC NULLS LAST LIMIT 48"
            )

    open_a = add_alert_targets(open_a, dead_names)
    res_a = add_alert_targets(res_a, dead_names)

    latest_id = int(snaps["snapshot_id"].iloc[0]) if len(snaps) else None
    latest_ts = snaps["captured_at"].iloc[0] if len(snaps) else None
    age = age_seconds(latest_ts)
    stale = age is not None and age > stale_after

    if latest_id is None:
        banner = '<div class="banner warn">No snapshots yet — start the collector.</div>'
    elif stale:
        banner = (
            f'<div class="banner bad">STALE DATA: latest snapshot #{latest_id} captured '
            f"{ct(latest_ts)} is {age:.0f}s old (&gt; {stale_after:.0f}s = 2.5 x {interval}s).</div>"
        )
    else:
        banner = (
            f'<div class="banner ok">Collector current: latest snapshot #{latest_id} at '
            f"{ct(latest_ts)} ({age:.0f}s ago).</div>"
        )

    edges = (
        locks.groupby(["snapshot_id", "captured_at"], as_index=False).size()
        .rename(columns={"size": "lock_edges"}).sort_values("snapshot_id")
        if len(locks) else pd.DataFrame(columns=["snapshot_id", "captured_at", "lock_edges"])
    )
    if len(edges):
        edges["snapshot"] = [f"#{int(r.snapshot_id)} {ct(r.captured_at)[11:19]}" for r in edges.itertuples()]
        edge_bars = bars(edges["lock_edges"], edges["snapshot"])
    else:
        edge_bars = '<p class="empty">no lock waits in window</p>'

    if len(sessions):
        piv = sessions.pivot_table(index=["snapshot_id"], columns="state",
                                   values="sessions", aggfunc="sum", fill_value=0).reset_index()
        piv = piv.sort_values("snapshot_id")
        piv["snapshot"] = [f"#{int(v)}" for v in piv["snapshot_id"]]
        sess_html = piv.drop(columns=["snapshot_id"]).to_html(index=False, border=0, classes="tbl")
    else:
        sess_html = '<p class="empty">no session samples</p>'

    # ---- optional history panels (5-7) ----
    if not available["monitor.v_table_stats"]:
        table_health_html = (
            '<div class="banner warn">Table health: monitor.v_table_stats '
            "not available in this database.</div>"
        )
    elif len(table_latest):
        tl = table_latest.copy()
        tl["table"] = tl["schemaname"].astype(str) + "." + tl["relname"].astype(str)
        tl["dead_pct"] = tl["dead_ratio"].map(
            lambda v: f"{float(v):.2%}" if pd.notna(v) else "—"
        )
        latest_html = fmt_table(
            tl[["table", "n_live_tup", "n_dead_tup", "dead_pct", "seq_scan", "idx_scan"]]
        )
        if len(table_history):
            hist = table_history.copy()
            hist["table"] = hist["schemaname"].astype(str) + "." + hist["relname"].astype(str)
            top_tables = tl.sort_values("n_dead_tup", ascending=False)["table"].head(6).tolist()
            hist = hist[hist["table"].isin(top_tables)]
            chart_piv = (
                hist.pivot_table(
                    index=["snapshot_id", "captured_at"], columns="table",
                    values="n_dead_tup", aggfunc="max",
                )
                .reset_index()
                .sort_values("snapshot_id")
            )
            chart_cols = [c for c in top_tables if c in chart_piv.columns]
            if chart_cols:
                labels = pd.Series([
                    f"#{int(sid)} {ct(ts)[11:19]}"
                    for sid, ts in zip(chart_piv["snapshot_id"], chart_piv["captured_at"])
                ])
                chart_html = line_chart(chart_piv[chart_cols], labels)
            else:
                chart_html = '<p class="empty">no dead-tuple history</p>'
        else:
            chart_html = '<p class="empty">no dead-tuple history</p>'
        table_health_html = (
            latest_html
            + '<h3 style="font-size:13px;margin:14px 0 4px">Dead tuples over time</h3>'
            + chart_html
        )
    else:
        table_health_html = '<p class="empty">view exists but has no samples yet</p>'

    if not available["monitor.v_statement_stats"]:
        statement_html = (
            '<div class="banner warn">Top statements: monitor.v_statement_stats '
            "not available in this database.</div>"
        )
    elif len(statement_stats):
        st_show = statement_stats.copy()
        st_show["queryid"] = st_show["queryid"].map(lambda v: str(int(v)) if pd.notna(v) else "—")
        st_show["calls_delta"] = st_show["calls_delta"].map(
            lambda v: f"{int(v):,}" if pd.notna(v) else "unknown"
        )
        st_show["total_exec_time_delta"] = st_show["total_exec_time_delta"].map(
            lambda v: f"{float(v):,.1f} ms" if pd.notna(v) else "unknown"
        )
        st_show["mean_exec_time"] = st_show["mean_exec_time"].map(
            lambda v: f"{float(v):,.3f} ms" if pd.notna(v) else "—"
        )
        statement_html = fmt_table(st_show)
    else:
        statement_html = '<p class="empty">no statement samples in the latest snapshot</p>'

    if not available["monitor.v_hourly_rollup"]:
        hourly_html = (
            '<div class="banner warn">Hourly rollup: monitor.v_hourly_rollup '
            "not available in this database.</div>"
        )
    elif len(hourly_rollup):
        roll_show = hourly_rollup.copy()
        roll_show["hour"] = roll_show["hour"].map(ct)
        roll_show["blks_hit_ratio"] = roll_show["blks_hit_ratio"].map(
            lambda v: f"{float(v):.2%}" if pd.notna(v) else "unknown"
        )
        hourly_html = fmt_table(roll_show)
    else:
        hourly_html = '<p class="empty">no hourly rows</p>'

    css = """
    :root{color-scheme:light dark}
    body{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px 32px;
         background:#f6f7f9;color:#14181f}
    h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:26px 0 8px;border-bottom:1px solid #d8dce2;
         padding-bottom:4px} .sub{color:#5b6472;margin:0 0 14px}
    .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px}
    .card{background:#fff;border:1px solid #dde1e7;border-radius:8px;padding:10px 16px;min-width:150px}
    .card .k{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#68717f}
    .card .v{font-size:18px;font-weight:600}
    .banner{padding:9px 14px;border-radius:6px;margin:10px 0;font-weight:600}
    .ok{background:#e7f6ec;color:#14532d;border:1px solid #b7e2c6}
    .warn{background:#fff6e0;color:#7a4b00;border:1px solid #f0d79a}
    .bad{background:#fdeaea;color:#8a1c1c;border:1px solid #efb9b9}
    table.tbl{border-collapse:collapse;background:#fff;font-size:12.5px;width:100%;margin:6px 0 4px}
    table.tbl th{background:#eef1f5;text-align:left;padding:6px 8px;border:1px solid #dde1e7;font-weight:600}
    table.tbl td{padding:5px 8px;border:1px solid #e7eaef;vertical-align:top}
    table.tbl tr:nth-child(even) td{background:#fafbfc}
    .openbox{background:#fdeaea;border:1px solid #efb9b9;border-radius:7px;padding:8px 12px;margin:6px 0}
    .openbox table.tbl th{background:#f8d7d7}
    .empty{color:#68717f;font-style:italic}
    .hbar{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
    .hbar .lab{width:96px;color:#4a5464} .hbar .track{flex:1;background:#e9edf2;height:11px;border-radius:3px}
    .hbar .track i{display:block;height:11px;background:#3f6fb5;border-radius:3px}
    .bar{position:relative;display:flex;align-items:center;gap:6px;min-width:90px}
    .bar span{background:#3f6fb5;height:9px;border-radius:2px;display:inline-block;min-width:2px}
    footer{margin-top:26px;color:#68717f;font-size:11.5px}
    """
    stamp = ct(datetime.now(timezone.utc))
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>PostgreSQL Monitoring Dashboard (CSCE 4355)</title><style>{css}</style></head><body>
<h1>PostgreSQL Monitoring Dashboard (CSCE 4355)</h1>
<p class="sub">Static fallback render · read-only role <code>{html.escape(str(who['role']))}</code>
 · times America/Chicago (CT) · generated {stamp}</p>
<div class="cards">
  <div class="card"><div class="k">Database</div><div class="v">{html.escape(str(who['db']))}</div></div>
  <div class="card"><div class="k">Connected role</div><div class="v">{html.escape(str(who['role']))}</div></div>
  <div class="card"><div class="k">Latest snapshot</div><div class="v">{"#"+str(latest_id) if latest_id else "none"}</div></div>
  <div class="card"><div class="k">Snapshot age</div><div class="v">{f"{age:.0f} s" if age is not None else "—"}</div></div>
</div>
{banner}
<h2>1 · Alerts</h2>
<h3 style="font-size:13px;margin:8px 0 4px">Open alerts ({len(open_a)})</h3>
{('<div class="openbox">' + fmt_table(open_a, time_cols=('first_seen_at','eligible_at','fired_at','notified_at')) + '</div>') if len(open_a) else '<p class="empty">No open alerts.</p>'}
<h3 style="font-size:13px;margin:14px 0 4px">Resolved alerts ({len(res_a)})</h3>
{fmt_table(res_a, time_cols=('fired_at','resolved_at'))}
<h2>2 · Current blockers</h2>
{fmt_table(blockers, time_cols=('captured_at','wait_started')) if len(blockers) else '<div class="banner ok">No blocking at latest snapshot.</div>'}
<h2>3 · Lock history (last {len(snaps)} snapshots)</h2>
{fmt_table(locks, time_cols=('captured_at','wait_started')) if len(locks) else '<p class="empty">no lock waits</p>'}
<h3 style="font-size:13px;margin:14px 0 4px">Lock edges per snapshot</h3>
{edge_bars}
<h2>4 · Session state and database counters</h2>
<h3 style="font-size:13px;margin:8px 0 4px">Session state counts per snapshot</h3>
{sess_html}
<h3 style="font-size:13px;margin:14px 0 4px">Database counters (delta = unknown means no comparable previous sample)</h3>
{fmt_table(stats, time_cols=('captured_at',), unknown_cols=('xact_commit_delta','xact_rollback_delta','blks_read_delta','blks_hit_delta'))}
<h2>5 · Table health</h2>
{table_health_html}
<h2>6 · Top statements by total execution time</h2>
<p class="sub" style="margin:0 0 6px">Latest snapshot, ordered by total_exec_time_delta · queryid only (no SQL text is stored).</p>
{statement_html}
<h2>7 · Hourly rollup</h2>
{hourly_html}
<footer>Source: monitor.v_snapshot, v_session_history, v_lock_history, v_current_blockers,
v_db_stats, v_alerts — plus v_table_stats, v_statement_stats, v_hourly_rollup when present —
as monitor_reader (SELECT only). Streamlit replacement of dashboard/app.py.</footer>
</body></html>"""

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out} ({len(doc)} bytes) from db={args.db} role={who['role']} snapshots={len(snaps)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
