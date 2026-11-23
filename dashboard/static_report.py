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
<footer>Source: monitor.v_snapshot, v_session_history, v_lock_history, v_current_blockers,
v_db_stats, v_alerts as monitor_reader (SELECT only). Streamlit replacement of dashboard/app.py.</footer>
</body></html>"""

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out} ({len(doc)} bytes) from db={args.db} role={who['role']} snapshots={len(snaps)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
