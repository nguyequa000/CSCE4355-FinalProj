# Dashboard notes (T4)

Read-only Streamlit console over the monitoring history. Everything here is in
`dashboard/`; the app never writes to the database.

## Files

| file | purpose |
|---|---|
| `app.py` | Streamlit dashboard (alerts, current blockers, lock history, session/db counters, table health, top statements, hourly rollup) |
| `static_report.py` | fallback: renders the same views to a single static HTML file |
| `dev_fixtures.sql` | **DEV / SYNTHETIC** rows for local testing only — never load into the demo DB |
| `dev_fixtures_v2.sql` | **DEV / SYNTHETIC** history-view fixtures: contract base tables + stand-in views when missing + extra-rule alerts |
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
  `v_current_blockers`, `v_db_stats`, `v_alerts` and nothing else. When the
  optional history views exist, it also has `SELECT` on `v_table_stats`,
  `v_statement_stats` and `v_hourly_rollup`.
* Each optional view is probed with `SELECT to_regclass('monitor.v_*')` before it
  is queried; a database without them renders **"not available in this database"**
  in that panel instead of failing the page.
* Every session also sets `default_transaction_read_only=on` and
  `statement_timeout=8000`, and an in-app `_guard()` rejects any statement that is
  not `SELECT`/`WITH` or that contains a write keyword.
* No credentials are displayed; the sidebar prints only database, role and host:port.

## What it shows

1. **Alerts** — open alerts highlighted in red (alert_id, rule, severity, PIDs,
   first_seen/eligible/fired, notified_at) plus the resolved alerts table with
   `resolved_at` and `observed_seconds`. Both tables carry a rule-aware
   **`target`** column and can be filtered by `rule_name`:
   `blocked_session` → `pid X blocked by Y`; `idle_in_transaction` /
   `long_active_query` → `pid X`; `dead_tuple_risk` → `table <relname>`
   (relname looked up in the latest `v_table_stats`, falling back to the relid
   number); `connection_saturation` / `cache_hit_ratio` → `database`.
2. **Current blockers** — `monitor.v_current_blockers`, i.e. only the latest
   snapshot; shows "No blocking at latest snapshot." when empty.
3. **Lock history** — last *N* snapshots (sidebar slider, default 12): waiter →
   blocker rows with `waited_seconds`, plus a line chart of lock edges per snapshot.
4. **Session state and database counters** — session counts per state per snapshot
   (line chart) and `v_db_stats` rows. `NULL` deltas render as `unknown`, never 0.
5. **Table health** — latest snapshot per table from `v_table_stats` (live/dead
   tuples, `dead_ratio`, sequential vs index scans) plus a dead-tuples-over-time
   line chart for up to six tables.
6. **Top statements** — latest snapshot from `v_statement_stats`, ordered by
   `total_exec_time_delta`, `queryid` only (the contract stores no SQL text).
7. **Hourly rollup** — `v_hourly_rollup` rows (snapshots, peak lock edges and
   sessions, commit delta sum, block cache hit ratio, alerts fired).

Panels 5–7 are optional: each probes `to_regclass` first and shows
**"not available in this database"** when the view is absent.

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
bash scripts/setup_db.sh shopdb_d2
set -a; . ./.env; set +a
PGPASSWORD="$PG_SUPERUSER_PASSWORD" psql -h 127.0.0.1 -p 55432 -U postgres \
    -d shopdb_d2 -v ON_ERROR_STOP=1 \
    -f dashboard/dev_fixtures.sql -f dashboard/dev_fixtures_v2.sql
```

It starts with `TRUNCATE monitor.alert_log, ...` — it destroys history in the target
database, so only ever point it at `shopdb_d2`. Screenshots taken from these rows are
labelled DEV/SYNTHETIC; the real evidence screenshot must be captured against the live
demo database.

`dashboard/dev_fixtures_v2.sql` runs after it and adds the history-view fixtures:

* creates the contract base tables `monitor.table_stat_sample` and
  `monitor.statement_sample` with `IF NOT EXISTS`;
* creates **simple stand-in** versions of `monitor.v_table_stats`,
  `monitor.v_statement_stats` and `monitor.v_hourly_rollup` only when
  `to_regclass` says the view is missing (the production views are owned by
  another lane and are never replaced);
* grants `SELECT` on those three views to `monitor_reader`;
* inserts DEV/SYNTHETIC table/statement samples and alerts for
  `dead_tuple_risk`, `connection_saturation`, `cache_hit_ratio`,
  `idle_in_transaction` and `long_active_query`.

To exercise the missing-view path, build a second database and load **only**
`dev_fixtures.sql` (or nothing at all):

```bash
bash scripts/setup_db.sh shopdb_d2b   # fresh DB without the new views
```

Verification evidence for both cases is in `docs/results/d2_verify.txt`; the
headless-Edge screenshots are `docs/results/d2_dev_dashboard_SYNTHETIC.png`
(1500x3600 viewport) and `docs/results/d2_dev_dashboard_full_SYNTHETIC.png`
(1500x5000, all panels).
