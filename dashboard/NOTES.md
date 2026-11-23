# Dashboard notes (T4)

Read-only Streamlit console over the monitoring history. Everything here is in
`dashboard/`; the app never writes to the database.

## Files

| file | purpose |
|---|---|
| `app.py` | Streamlit dashboard (alerts, current blockers, lock history, session/db counters) |
| `static_report.py` | fallback: renders the same views to a single static HTML file |
| `dev_fixtures.sql` | **DEV / SYNTHETIC** rows for local testing only — never load into the demo DB |
| `NOTES.md` | this file |

## Run

```bash
# from the repository root, with .env present (PGHOST/PGPORT/PGDATABASE and role passwords)
.venv/Scripts/python.exe -m streamlit run dashboard/app.py \
    --server.port 8501 --server.headless true
# then open http://127.0.0.1:8501/

# a different database than PGDATABASE (default shopdb):
DASH_DB=shopdb_t4 .venv/Scripts/python.exe -m streamlit run dashboard/app.py --server.port 8501
```

Fallback if Streamlit will not start during the demo:

```bash
.venv/Scripts/python.exe dashboard/static_report.py --db shopdb \
    --out docs/results/dashboard_snapshot.html
# open the HTML file; same views, same CT timestamps, no server process
```

## Access model

* Connects only as **`monitor_reader`** via `scripts.dbconf.connect`; that role has
  `SELECT` on `monitor.v_snapshot`, `v_session_history`, `v_lock_history`,
  `v_current_blockers`, `v_db_stats`, `v_alerts` and nothing else (verified:
  `INSERT INTO monitor.snapshot` → `permission denied for table snapshot`;
  `SELECT FROM shop.orders` → `permission denied for schema shop`).
* Every session also sets `default_transaction_read_only=on` and
  `statement_timeout=8000`, and an in-app `_guard()` rejects any statement that is
  not `SELECT`/`WITH` or that contains a write keyword.
* No credentials are displayed; the sidebar prints only database, role and host:port.

## What it shows

1. **Alerts** — open alerts highlighted in red (alert_id, rule, severity, PIDs,
   first_seen/eligible/fired, notified_at) plus the resolved alerts table with
   `resolved_at` and `observed_seconds`.
2. **Current blockers** — `monitor.v_current_blockers`, i.e. only the latest
   snapshot; shows "No blocking at latest snapshot." when empty.
3. **Lock history** — last *N* snapshots (sidebar slider, default 12): waiter →
   blocker rows with `waited_seconds`, plus a line chart of lock edges per snapshot.
4. **Session state and database counters** — session counts per state per snapshot
   (line chart) and `v_db_stats` rows. `NULL` deltas render as `unknown`, never 0.

Header shows database, `current_user` (must be `monitor_reader`), latest snapshot id
and `captured_at`, and its age. Age > 2.5 × `COLLECT_INTERVAL_SECONDS` (default 60 s,
so 150 s) raises a red **STALE DATA** banner naming the snapshot so a stopped
collector cannot be mistaken for a healthy system.

All timestamps are converted to **America/Chicago** and printed with the `CST`/`CDT`
label from the tz database.

## Refresh

`st.fragment(run_every="15s")` (Streamlit >= 1.37; this environment has 1.67).
Auto-refresh is a sidebar checkbox; when unchecked the fragment does not
self-schedule. Queries are cached for 5 s so a fragment tick always re-reads.
If `st.fragment` is unavailable the app falls back to a `time.sleep` + `st.rerun`
loop, which is the same data path.

## Development fixtures

`dashboard/dev_fixtures.sql` loads six synthetic snapshots with two lock incidents
(one resolved, one open), two alerts and two session-start-time edge cases into a
throwaway database:

```bash
bash scripts/setup_db.sh shopdb_t4
set -a; . ./.env; set +a
PGPASSWORD="$PG_SUPERUSER_PASSWORD" psql -h 127.0.0.1 -p 55432 -U postgres \
    -d shopdb_t4 -v ON_ERROR_STOP=1 -f dashboard/dev_fixtures.sql
```

It starts with `TRUNCATE monitor.alert_log, ...` — it destroys history in the target
database, so only ever point it at `shopdb_t4`. Screenshots taken from these rows are
labelled DEV/SYNTHETIC; the real evidence screenshot must be captured against the live
demo database.
