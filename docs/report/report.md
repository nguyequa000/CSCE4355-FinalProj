<!-- titlepage:start -->
# PostgreSQL Database Monitoring Dashboard
## Historical Metrics, Threshold Alerting, and a Read-Only Dashboard

CSCE 4355 — Database Administration

Final Project Report

Quan Nguyen · Dustin Dowell · Zach Riggle

November 22, 2026
<!-- titlepage:end -->

<!-- pagebreak -->

# Executive Summary

PostgreSQL exposes a rich set of runtime statistics, but almost all of them describe the present
moment or a cumulative total since the last reset. A database administrator who is called after a
slowdown has already lost the evidence: which session blocked which, when the wait began, and
whether the condition cleared. This project closes that gap for a small simulated online store by
storing periodic observations of the server in a dedicated `monitor` schema, evaluating a
threshold rule against that stored history, recording the resulting alert, reporting it to a
console notifier, and exposing the history to a read-only dashboard role.

The delivered system is a disposable PostgreSQL 17.11 cluster on `127.0.0.1:55432` holding a
`shop` schema (`customers`, `orders`, 1,000 and 10,000 deterministic rows) and a `monitor` schema
with snapshot, session, lock-wait and database-counter history. A Python collector using psycopg 3
takes a snapshot every 60 seconds and calls `monitor.evaluate_alerts()`; a `blocked_session` rule
with a 30-second threshold and `critical` severity opens at most one alert per blocked backend and
resolves it when a later snapshot no longer observes the wait. A Streamlit dashboard connects as
`monitor_reader`, which can read only the sanitized `monitor.v_*` views.

The scope is deliberately one workflow rather than the six rules and full stack in the approved
proposal. The substitution of PostgreSQL 17, a Python scheduler and Streamlit for PostgreSQL 16,
`pg_cron` and Grafana, the reduced seed, and the unimplemented rules are disclosed in Section 8.

The measured run is presented in Sections 5–7. On 2026-11-22 the collector opened alert 1:
`blocked_pid` 35164 (`lock_demo_waiter`) had waited on `blocker_pid` 42028 (`lock_demo_blocker`,
state `idle in transaction`) since 21:42:33.388 CST. The alert became eligible at 21:43:03.388,
fired at 21:43:25.214 — 51.8 s after the lock was injected, against the proposal's 120 s
(two-interval) target — was printed by the notifier 54 ms later, and resolved at 21:45:25.208,
21.8 s after the lock was released at 21:45:03.434. No duplicate alert was opened while the wait
persisted, and no alert remained open afterwards. The automated suite passes all 29 tests on freshly
dropped and recreated test databases: `29 passed in 22.18s` (`docs/results/final_pytest.txt`).

The two items left open in the previous draft are now closed with captured evidence. A fresh-clone
reproduction run by the team's automated tooling — **self-tested by team tooling, not independently
reproduced by a teammate** — rebuilt the cluster from the README and reproduced the full T1–T7
sequence, including the fire/resolve pair (PIDs 23896 / 19700) and the dashboard health check. It
passed only after three README gaps were worked around: `start_cluster.sh` could not initialize from
Git Bash's `/tmp` password file and fixed `PGPORT` before sourcing `.env`, and Section 6 did not name
the test databases. The script now creates the password file inside the project directory and sources
`.env` before computing the port, and Section 6 lists `shopdb_t2` / `shopdb_t3` / `shopdb_t5`
(`docs/results/repro_smoke.txt`). Two further rules, `idle_in_transaction` and `long_active_query`,
were each verified live on `shopdb_t5`: fired 60.605 s and 60.562 s after injection (0.604 s and
0.561 s after eligibility, at a 30 s collector cadence) and resolved 20.602 s and 20.555 s after the
sessions ended (`docs/results/t5_extra_rules_live.txt`). Three of the six proposed rules are
therefore verified live. No result is stated in this report that was not read from a captured file
in `docs/results/`.

# 1. Introduction & Problem

## 1.1 The historical monitoring gap

PostgreSQL's cumulative statistics system answers "what is happening now" and "what has happened in
total". `pg_stat_activity` shows the current state of every backend, `pg_locks` and
`pg_blocking_pids()` show current lock waits, `pg_stat_database` accumulates counters since the last
statistics reset, and `pg_stat_statements` aggregates statement execution across the instance. None
of these views retains a timeline. A DBA who looks at them after users complain sees the aftermath,
not the incident: the blocking session has usually finished, the counters have moved on, and the
wait that lasted two minutes is invisible.

The practical consequences are familiar: connection exhaustion, long-running or blocked queries,
sessions left idle inside open transactions, a falling buffer-cache hit ratio, unused or missing
indexes, and growing dead-tuple counts are discovered late, when they have already affected users.
Without stored history there is no baseline to compare against, and without threshold evaluation
there is no early warning.

## 1.2 Objective

The project builds a working, reproducible monitoring loop for a single PostgreSQL instance running
a simulated online store:

1. Capture session, lock and database-level observations on a fixed schedule and store them as
   history rather than exposing only the live views.
2. Detect a blocked-session condition from the stored history using an explicit, configurable
   threshold, and record when the condition became eligible, when the alert fired, and when it
   resolved.
3. Preserve *who blocked whom* — the `(blocked_pid, blocker_pid)` relationship — so the incident can
   be reconstructed after the fact.
4. Report new and resolved alerts through a notifier and display the history through a read-only
   dashboard role.
5. Enforce least privilege with three roles, so that neither the application nor the dashboard can
   read or write more than it needs.
6. Make the whole demonstration reproducible from the README on a disposable, non-administrator
   cluster.

## 1.3 Final scope

The final scope is one complete DBA workflow, demonstrated for three rules: a simulated shop develops
lock contention, the collector preserves the blocking edge, the rule fires once the wait crosses 30
seconds, the notifier reports it, the dashboard shows the same event, and the alert resolves after the
lock clears; two further rules (`idle_in_transaction` and `long_active_query`, both at a 60 s
threshold) were exercised against their own sessions with the same fire/resolve lifecycle. The
approved proposal described six shop tables, roughly 100,000 customers and 1,000,000 orders, six
alert rules, `pg_cron`, Grafana and a Docker Compose environment. Those parts are not all present,
and Section 8 states precisely which are missing. The reduction is a deliberate trade: an
end-to-end, evidence-backed demonstration of three rules was preferred over six partially configured
rules with no observed detection.

# 2. System / Database Architecture

![Figure 1: Actual system architecture and data flow. The workload drives PostgreSQL statistics views; the collector turns them into history; alerts flow to the notifier, and history flows to the read-only dashboard.](figures/architecture.png)

Figure 1 shows the components actually implemented. The workload generator issues SQL as `shop_app`
against the `shop` schema. PostgreSQL maintains its statistics views. The Python collector polls
those views every 60 seconds and writes one snapshot plus its per-session, per-lock-edge and
per-database rows into the `monitor` schema, then evaluates the alert rules. `monitor_reader` reads
only the reporting views that the collector role exposes; the Streamlit dashboard uses that role.
The console notifier polls `monitor.alert_log` and prints new and resolved alerts.

## 2.1 Components

Table 1: Implemented components and their responsibilities.

| Component | Technology | Responsibility |
|---|---|---|
| Database | PostgreSQL 17.11, disposable local cluster in `./.pgdata`, `127.0.0.1:55432` | Hosts the `shop` and `monitor` schemas |
| Extension | `pg_stat_statements` 1.11, preloaded via `shared_preload_libraries` | Instance-wide statement statistics available to a superuser or granted role |
| Workload | `workload/lock_demo.py` (psycopg 3) | Opens two `shop_app` sessions that contend on one `shop.orders` row and holds the lock |
| Collector | `scripts/collect.py` (Python 3.12, psycopg 3) | 60-second snapshot loop and alert evaluation |
| History | `monitor` schema, owned by `monitor_collector` | Snapshot, session, lock-wait, database-counter and alert tables |
| Alert logic | `monitor.evaluate_alerts()` in `sql/20_alerts.sql`, extended by `sql/21_extra_rules.sql` | Applies `blocked_session` (30 s, critical), `idle_in_transaction` (60 s, warning) and `long_active_query` (60 s, warning); opens and resolves alert rows |
| Notifier | `notifier/notify.py` | Polls `monitor.alert_log` every 15 seconds and prints changes |
| Dashboard | `dashboard/app.py`, Streamlit on port 8501 | Read-only history, current blockers and alert list as `monitor_reader` |
| Configuration | `.env` (gitignored) read by `scripts/dbconf.py` | Host, port, database and the four role passwords |

## 2.2 Deployment and testing environment

The system runs on Windows 11 under Git Bash, without administrator rights and without Docker. The
cluster is created by `scripts/start_cluster.sh` with `initdb` into `./.pgdata`; it is not registered
as a Windows service and does not touch the pre-existing PostgreSQL service on port 5432. Table 2
records the versions in use.

Table 2: Software versions in the tested environment.

| Software | Version | How it was determined |
|---|---|---|
| PostgreSQL server | 17.11 | `current_setting('server_version')` captured in `docs/results/t1_setup.txt` |
| `pg_stat_statements` | 1.11 | `pg_extension.extversion`, same capture |
| Python | 3.12 | Build environment (`python --version`) |
| psycopg | 3.3.9 | `psycopg.__version__` in the project venv |
| Streamlit | 1.67.0 | `streamlit.__version__` in the project venv |
| python-docx | 1.2.0 | Report builder dependency |
| matplotlib | 3.11.2 | Architecture figure generation |

## 2.3 Data flow and privilege boundaries

Each collection cycle is a single logical pass over the live statistics:

1. Insert a `monitor.snapshot` row; its `snapshot_id` and `captured_at` identify every row written in
   that cycle.
2. Copy one row per client backend from `pg_stat_activity` into `monitor.session_sample`, including
   state, wait event, transaction and query start times, the `pg_blocking_pids()` array, and a
   60-character statement excerpt.
3. Expand each non-empty blocking array into one `monitor.lock_wait` row per `(blocked, blocker)`
   pair, with both roles and application names, the blocker's state, and the observed wait in
   seconds.
4. Read `pg_stat_database` for the project database into `monitor.db_stat_sample`, computing deltas
   against the previous sample and storing `NULL` when no valid previous sample exists.
5. Call `monitor.evaluate_alerts()` so that alert state is derived from the rows just stored.

The privilege boundary is part of the architecture, not an afterthought: `shop_app` has no rights in
`monitor`; `monitor_collector` owns `monitor` and is a member of `pg_read_all_stats` so it can see
other sessions; `monitor_reader` has `USAGE` on the schema and `SELECT` on the six `v_*` views only.
The dashboard therefore cannot read base tables, cannot read `shop`, and cannot write anything.

# 3. Database Design

## 3.1 The `shop` schema

The application schema is intentionally small: two tables with primary keys, a foreign key, check
constraints, a unique constraint and one secondary index. `sql/01_shop.sql` drops and recreates both
tables, so the seed is repeatable and deterministic.

Table 3: Columns and constraints of the `shop` schema.

| Table / column | Type | Constraints |
|---|---|---|
| `shop.customers.customer_id` | `integer` | Primary key |
| `shop.customers.email` | `text` | `NOT NULL`, `UNIQUE` |
| `shop.customers.full_name` | `text` | `NOT NULL` |
| `shop.customers.region` | `text` | `NOT NULL` |
| `shop.customers.created_at` | `timestamptz` | `NOT NULL` |
| `shop.customers.balance` | `numeric(12,2)` | `NOT NULL`, default `0` |
| `shop.orders.order_id` | `integer` | Primary key |
| `shop.orders.customer_id` | `integer` | `NOT NULL`, references `shop.customers(customer_id)` |
| `shop.orders.ordered_at` | `timestamptz` | `NOT NULL` |
| `shop.orders.status` | `text` | `NOT NULL`, `CHECK (status IN ('new','paid','shipped','cancelled'))` |
| `shop.orders.total_amount` | `numeric(12,2)` | `NOT NULL`, `CHECK (total_amount >= 0)` |

`orders_customer_id_idx` on `shop.orders(customer_id)` supports the foreign-key join direction. The
seed is a pure function of the row number — no `random()` and no external data — so every run
produces identical rows: 1,000 customers with `customerN@example.test` addresses and four rotating
regions, and 10,000 orders whose `customer_id` is `1 + (g * 7) % 1000`, spread over four statuses
and a bounded amount. `customer_id = 1` therefore has ten orders and is the row the lock
demonstration targets. `ANALYZE` runs at the end so the planner has statistics immediately.

The seed is smaller than the proposal's target of ~100,000 customers and ~1,000,000 orders, and the
proposal's `products`, `order_items`, `inventory` and `payments` tables are absent. The reason is
time: two tables are sufficient to produce and hold a row-level lock, and the reduced seed keeps
cluster creation and reproduction fast. The reduction is disclosed again in Section 8.

## 3.2 The `monitor` schema

`sql/10_monitor.sql` creates the schema with `AUTHORIZATION monitor_collector`, revokes all rights
from `PUBLIC`, and switches to the collector role before creating objects, so ownership is correct
without superuser-owned tables. Table 4 summarises the objects.

Table 4: History and alerting tables in the `monitor` schema.

| Object | Grain | Purpose |
|---|---|---|
| `monitor.snapshot` | one row per collection cycle | `snapshot_id` (`bigserial`), `captured_at` (`clock_timestamp()`), `collector_pid` |
| `monitor.session_sample` | one row per client backend per snapshot | `pid`, `usename`, `application_name`, `state`, `wait_event_type`, `wait_event`, `xact_start`, `query_start`, `state_change`, `blocked_by integer[]`, 60-character `query_excerpt`; primary key `(snapshot_id, pid)` |
| `monitor.lock_wait` | one row per blocked/blocker edge per snapshot | PIDs, both users and application names, blocker state, `wait_started`, `waited_seconds`; primary key `(snapshot_id, blocked_pid, blocker_pid)` |
| `monitor.db_stat_sample` | one row per database per snapshot | `numbackends`, cumulative counters, `stats_reset`, and non-negative deltas; primary key `(snapshot_id, datname)` |
| `monitor.alert_rule` | one row per rule name | `threshold_seconds`, `severity` (`info`/`warning`/`critical`), `enabled`, `description` |
| `monitor.alert_log` | one row per alert | rule, severity, both PIDs, `first_seen_at`, `eligible_at`, `fired_at`, `fired_snapshot_id`, `observed_seconds`, `last_seen_snapshot_id`, `resolved_at`, `resolved_snapshot_id`, `notified_at`, `resolve_notified_at` |

Two design decisions matter for correctness. First, every history table references
`monitor.snapshot(snapshot_id)` with `ON DELETE CASCADE`, so a snapshot and all of its detail rows
are one unit; deleting history cannot leave orphaned observations. Second, deltas are stored, not
rates, and are explicitly nullable: the first sample after cluster creation, and the first sample
after `pg_stat_reset`, have no valid predecessor. The collector writes `NULL` there rather than a
negative or a fabricated value, and the dashboard must display "unknown" for that interval.

## 3.3 Keys, indexes and sequences

`monitor.snapshot` and `monitor.alert_log` use `bigserial` primary keys, which also gives the
"latest snapshot" lookup a cheap index. The partial unique index `alert_log_one_open` on
`(rule_name, blocked_pid) WHERE resolved_at IS NULL` enforces the rule that a given blocked backend
can have at most one open alert: repeated samples extend the existing row instead of inserting a
duplicate. The composite primary keys of the detail tables make re-running a cycle for the same
snapshot impossible to duplicate, and the `(snapshot_id, ...)` prefix serves the per-snapshot joins
used by the views.

## 3.4 Reporting views

Six views form the reader-facing contract. They are created by `monitor_collector` and granted to
`monitor_reader`; base tables are never granted.

Table 5: Reporting views exposed to `monitor_reader`.

| View | Contents |
|---|---|
| `monitor.v_snapshot` | `snapshot_id`, `captured_at` — the timeline |
| `monitor.v_session_history` | Session samples joined to snapshot time, without the statement excerpt |
| `monitor.v_lock_history` | Lock-wait edges joined to snapshot time |
| `monitor.v_current_blockers` | `v_lock_history` restricted to the maximum `snapshot_id` |
| `monitor.v_db_stats` | Database counters and deltas joined to snapshot time |
| `monitor.v_alerts` | Alert rows plus a derived `status` of `open` or `resolved` |

The views are also a sanitization layer: `v_session_history` deliberately omits `query_excerpt`, so
the dashboard role sees session identity and state but not SQL text. `v_alerts` derives status from
`resolved_at IS NULL` rather than storing a redundant status column that could disagree with the
timestamps.

## 3.5 Roles and privileges

Table 6: Login roles and their effective privileges.

| Role | Membership / attributes | Privileges | Denied |
|---|---|---|---|
| `shop_app` | `LOGIN` | `USAGE` on `shop`; `SELECT`, `INSERT`, `UPDATE` on `shop.customers` and `shop.orders` | No access to `monitor`; no `DELETE`; no DDL |
| `monitor_collector` | `LOGIN`, member of `pg_read_all_stats` | Owns `monitor`; full DDL/DML there; can read all sessions' statistics | No rights in `shop` |
| `monitor_reader` | `LOGIN` | `USAGE` on `monitor`; `SELECT` on the six `v_*` views | No base-table access, no writes, no `shop` access |

`sql/00_roles.sql` creates the roles and database, revokes `ALL` on the database from `PUBLIC`, and
grants `CONNECT` only to the three project roles. Passwords arrive as `psql` variables (`-v app_pw=...`)
and are formatted with `%L` inside `\gexec`, so no password literal is stored in the repository and
role creation is idempotent.

# 4. DBA Implementation

## 4.1 Provisioning and configuration

`scripts/start_cluster.sh` performs `initdb` only when `./.pgdata/PG_VERSION` is absent, using
`--auth=scram-sha-256`, a temporary password file that is deleted immediately, `UTF8` encoding and
the `C` locale. It then appends project settings to `postgresql.conf`:

Table 7: Server settings written to `postgresql.conf` by `scripts/start_cluster.sh`.

| Setting | Value | Reason |
|---|---|---|
| `listen_addresses` | `'127.0.0.1'` | Loopback only; no network exposure |
| `port` | `55432` | Avoids the existing service on 5432 |
| `shared_preload_libraries` | `'pg_stat_statements'` | The extension can only load at startup |
| `pg_stat_statements.track` | `all` | Nested statements included |
| `track_activity_query_size` | `2048` | Enough text for a useful 60-character excerpt |
| `log_lock_waits` | `on` | Server log corroborates the sampled waits |
| `deadlock_timeout` | `1s` | Short enough that real deadlocks surface during a demo |

The script distinguishes start from stop (`scripts/start_cluster.sh stop` runs `pg_ctl -m fast stop`)
and uses `pg_ctl status` before starting, so a second invocation does not fail. It ends with
`pg_isready`, which gives an unambiguous success signal for scripts and for the reader.

## 4.2 Snapshot collection

`scripts/collect.py` is a scheduler loop: connect as `monitor_collector`, capture, sleep until the
next interval, repeat. Each capture inserts the snapshot row first and then writes the detail rows
using the returned `snapshot_id`, so every observation in a cycle shares one timestamp. Session
sampling filters to client backends and records the blocking PID array as PostgreSQL reports it,
rather than re-deriving blockers from `pg_locks`.

Three details protect the integrity of the history. Statement text is truncated to 60 characters
and credentials are never read; the collector stores session metadata, not query results. Database
counters are compared only with the immediately preceding sample of the same database, and a change
in `stats_reset` invalidates the comparison, producing `NULL` deltas. And the loop is interval-based
with a fresh connection per cycle, so a dropped connection delays one sample instead of ending the
history.

## 4.3 Blocking detection

Blocking is derived from `pg_blocking_pids(pid)`, which returns the PIDs a backend is directly
waiting on, including the indirect case where a prepared transaction or a parallel worker holds the
lock. For each non-empty array, one `monitor.lock_wait` row is written with the blocked and blocker
PIDs, both user names, both application names, the blocker's state, and `wait_started` taken from the
waiting backend's `state_change`. `waited_seconds` is `captured_at - wait_started` at sample time.

Storing one edge per pair, rather than a single "blocked" flag, is what makes the incident
reconstructable: after the fact, the history answers who waited on whom, in which order, for how
long, and whether the blocker was itself waiting. The independent `blocked_by` array on
`monitor.session_sample` preserves the raw server answer even if the edge expansion changes.

## 4.4 Alert evaluation and lifecycle

`monitor.alert_rule` holds the rule configuration. The build seeds three rows:

Table 8: Seeded alert rules.

| Rule | Threshold | Severity | Meaning |
|---|---|---|---|
| `blocked_session` | 30 seconds | `critical` | A backend has waited on a lock held by another backend for at least the threshold at sample time |
| `idle_in_transaction` | 60 seconds | `warning` | A backend has been `idle in transaction` for at least the threshold since its last state change |
| `long_active_query` | 60 seconds | `warning` | A backend has been `active` on a non-lock wait for at least the threshold since `query_start` (lock-blocked sessions are excluded so they are not counted twice) |

`monitor.evaluate_alerts()` (in `sql/20_alerts.sql`) applies the enabled rules to the newest
snapshot. It calls `monitor.evaluate_extra_rules(snapshot_id)` from `sql/21_extra_rules.sql` when
that function exists, which is how the two extra rules share the collector's single call site. The
lifecycle is expressed entirely in `monitor.alert_log` timestamps: `first_seen_at` is
the earliest observed wait start for the pair, `eligible_at` is `first_seen_at` plus the threshold,
`fired_at` is the snapshot time at which the alert was opened, `observed_seconds` is the latest
observed wait, and `resolved_at` plus `resolved_snapshot_id` record the first later snapshot in which
the wait was no longer present. The partial unique index guarantees that repeated samples of the
same ongoing wait extend `last_seen_snapshot_id` and `observed_seconds` instead of opening a second
alert.

This design makes two properties testable without trusting the notifier: an alert cannot exist
without a snapshot that observed the wait, and an alert cannot be both open and resolved for the
same pair. The notification timestamps (`notified_at`, `resolve_notified_at`) are written by the
notifier, so the delay between firing and notification is measurable rather than assumed.

Table 9 states the status of every rule in the approved proposal. A rule is called *verified* only
when a captured run shows the alert opening and resolving; a row in `monitor.alert_rule` is not
detection.

Table 9: Rule status. Three of the six proposed rules are verified live.

| Rule (proposal §8.2) | Threshold / severity | Implementation | Status |
|---|---|---|---|
| `blocked_session` | 30 s, `critical` | `sql/20_alerts.sql`, seeded in `monitor.alert_rule` | **Verified (live)** — alert 1 opened and resolved in `docs/results/live/alert_sql.txt` |
| `idle_in_transaction` | 60 s, `warning` | `sql/21_extra_rules.sql` (`monitor.evaluate_extra_rules`), seeded in `monitor.alert_rule` | **Verified (live, 30 s cadence)** — alert 1 fired 21:55:05.198, 60.605 s after injection (0.604 s after eligibility), resolved 21:56:05.192, 20.602 s after the session ended (`docs/results/t5_extra_rules_live.txt`) |
| `long_active_query` | 60 s, `warning` | `sql/21_extra_rules.sql` (`monitor.evaluate_extra_rules`), seeded in `monitor.alert_rule` | **Verified (live, 30 s cadence)** — alert 2 fired 21:57:44.958, 60.562 s after the query started (0.561 s after eligibility), resolved 21:58:44.953, 20.555 s after the session ended (same file) |
| Connection saturation | > 80 % of `max_connections`, `critical` | None | Not implemented |
| Low cache hit ratio | < 95 % over 5 minutes, `warning` | None | Not implemented |
| Table bloat risk | dead tuples > 20 % of live tuples, `info` | None | Not implemented |

Both extra rules are sample-based and were measured with the collector at `--interval 30` rather than
the 60 s default, so the 100 s demonstrations fit inside the evidence window; the threshold itself
stays 60 s. The fire delay after eligibility (0.604 s and 0.561 s) reflects where eligibility fell
relative to the sampling grid, not rule latency: with a 60 s cadence the same alert can fire up to
one extra interval later. `blocker_pid` repeats `blocked_pid` for both rules because
`alert_log.blocker_pid` is `NOT NULL` and neither condition has a blocker; this is a schema
accommodation, documented in `sql/21_extra_rules.sql`.

## 4.5 Access control

Access control is implemented in SQL, not in application code. `sql/00_roles.sql` creates the three
login roles and the database and revokes `PUBLIC` connect rights; `sql/01_shop.sql` grants the
application role only the schema usage and DML it needs; `sql/10_monitor.sql` creates `monitor` as
the collector, revokes `PUBLIC`, creates the views, and grants the reader only `SELECT` on those
views. The collector's ability to see other sessions comes from membership in the predefined role
`pg_read_all_stats`, which is narrower than `pg_monitor` and much narrower than superuser.

The consequence is that a compromised dashboard credential can read aggregate monitoring history
and nothing else: no base tables, no `shop` data, no writes, no DDL. The application credential
cannot see monitoring history at all. Verification of both the allow and the deny direction is a
required test (Section 6).

## 4.6 Dashboard

`dashboard/app.py` is a Streamlit application that connects with `scripts/dbconf.py` as
`monitor_reader` and reads only the `v_*` views. It presents the snapshot timeline, the lock-wait
history, the current blockers from the latest snapshot, and the alert list with derived status.
Because every query goes through the views, the dashboard cannot accidentally depend on a base table
that the reader role is not allowed to see; a permission error at development time is the intended
feedback.

## 4.7 Notifier

`notifier/notify.py` polls `monitor.alert_log` every 15 seconds and prints newly fired and newly
resolved alerts with rule, severity, PIDs, first-seen time and observed duration. It is read-only
except for stamping `notified_at` and `resolve_notified_at`, which makes notification delay a
recorded fact. Console output is deliberate: the proposal allowed email as optional, and the build
avoids storing SMTP credentials.

# 5. Implementation Evidence

Figure 2 and Table 10 are read together: both describe alert 1 from the same run. The screenshot was
taken while the alert was open; the SQL in Table 10 was queried as `monitor_reader` at 21:46:10 CST,
after resolution, so it shows the final `resolved` row (`docs/results/live/alert_sql.txt`).

![Figure 2: Streamlit dashboard as role monitor_reader at 21:43:59 CST, 34 s after snapshot 2, cropped from the full-page capture `docs/results/lock-dashboard.png` to keep the header, alert, blocker and lock-history panels. The header cards show database shopdb and connected role monitor_reader; the alert band shows alert_id 1, rule blocked_session, severity critical, blocked_pid 35164 and blocker_pid 42028 with first_seen 21:42:33, eligible 21:43:03, fired 21:43:25 and observed_seconds 51.8; the Current blockers table shows the same pair with blocker_state "idle in transaction". Streamlit renders these grids on a canvas, so the values are legible at full resolution but small in print; Table 10 transcribes the same row from SQL.](figures/dashboard.png)

The dashboard is the Streamlit application itself, connecting as `monitor_reader` and
reading `monitor.v_alerts`, `monitor.v_current_blockers` and `monitor.v_snapshot`. Its own footer
records the render at 21:44:01 CST, the latest snapshot as `#2` (captured 21:43:25, age 36 s), one
open alert and zero resolved alerts. The same page re-rendered at 21:44:43 CST shows snapshot `#3`
with `observed_seconds` 111.8 and the alert still open (`docs/results/lock-dashboard-open-2.png`),
and at 21:45:47 CST shows snapshot `#4` with zero open alerts, one resolved alert and "No blocking at
latest snapshot" (`docs/results/lock-dashboard-resolved.png`). In the resolved capture the alert grid
cells did not paint text, so the resolved row is evidenced by `docs/results/live/dashboard_resolved.html`
and by the SQL, not by that image.

Table 10: Alert 1 read as `monitor_reader`, plus the collector and notifier records for the same event.

| Field | Source | Observed value |
|---|---|---|
| `alert_id`, `rule_name`, `severity` | `monitor.v_alerts` | `1`, `blocked_session`, `critical` |
| Blocked PID / blocker PID and applications | `monitor.v_alerts`, `monitor.v_lock_history` | 35164 (`lock_demo_waiter`) waiting on 42028 (`lock_demo_blocker`), blocker state `idle in transaction` |
| `first_seen_at` (wait start) | `monitor.alert_log` | 2026-11-22 21:42:33.388 CST |
| `eligible_at` (wait start + 30 s) | `monitor.alert_log` | 2026-11-22 21:43:03.388 CST |
| `fired_at` / `observed_seconds` at firing | `monitor.alert_log` / `monitor.v_alerts` | 2026-11-22 21:43:25.214 CST / 51.8 s |
| Latest observed while open | `monitor.v_lock_history` snapshot 3 | 111.8 s at 21:44:25.220 CST |
| `resolved_at` and `status` | `monitor.alert_log` / `monitor.v_alerts` | 2026-11-22 21:45:25.208 CST / `resolved` |
| `notified_at` / `resolve_notified_at` | `monitor.alert_log` | 21:43:25.268 CST / 21:45:25.275 CST |
| Open alerts after recovery | `monitor.v_alerts` | 0 |

The notifier stdout for the same two transitions is short enough to quote in full
(`docs/results/live/notifier_console.log`):

```
[ALERT FIRED] id=1 rule=blocked_session severity=critical blocked_pid=35164 blocker_pid=42028 fired_at=2026-11-22T21:43:25.213833-06:00 observed=51.8s
[ALERT RESOLVED] id=1 resolved_at=2026-11-22T21:45:25.208184-06:00
```

Four moments are therefore distinguishable and each has its own source. The lock was injected at
21:42:33.440 CST (`docs/results/live/lock_demo.log`, recorded in UTC as 03:42:33.440+00:00); the
server's `state_change` for the waiting backend is 21:42:33.388, 52 ms earlier, because
`first_seen_at` is taken from the backend's own wait start rather than from the demo's log line. The
wait became eligible at 21:43:03.388, exactly 30 s later. It fired at 21:43:25.214, the first sample
after eligibility — 21.8 s after the threshold was crossed and 51.8 s after injection. It resolved at
21:45:25.208, the first clean sample after the lock was released at 21:45:03.434. The collector's own
lines record the same progression, including the absence of a duplicate: snapshot 2
`lock_edges=1 alerts_opened=1`, snapshot 3 `lock_edges=1 alerts_opened=0`, snapshot 4
`lock_edges=0 alerts_resolved=1` (`docs/results/live/collector.log`).

The screenshots alone would not be sufficient evidence: they show a page, not the stored row. They
are evidence here because they can be tied to the stored data by alert ID, by the PID pair and by
the snapshot timestamp, and because the SQL was executed under the same read-only role the dashboard
uses.

# 6. Testing & Verification

Table 11 lists the tests the demonstration requires. The expected column is fixed by design; the
actual column is read from a captured artifact, and every row below names the artifact it came from.

Table 11: Verification matrix. Every "actual" value is read from the named artifact.

| ID | Test | Expected behavior | Actual / artifact | Result |
|---|---|---|---|---|
| T1 | Cluster and seed | Server 17.11, `pg_stat_statements` 1.11, 1,000 customers, 10,000 orders, `shop_app` can log in | 17.11 / 1.11 / 1000 / 10000 (`docs/results/t1_setup.txt`) | PASS (as captured) |
| T2 | Repeated snapshots | Two unattended snapshots with distinct timestamps, each with session and database rows | Four snapshots 21:42:25.256 → 21:45:25.208, gaps 59.958 / 60.007 / 59.988 s, sessions 1/3/3/1, lock edges 0/1/1/0 (`docs/results/live/alert_sql.txt`, `docs/results/live/collector.log`); separate `--count 2` loop 60 s apart on `shopdb_t2` (`docs/results/t2_collector_run.txt`) | PASS |
| T3 | Delta correctness | First sample has `NULL` deltas; later deltas are non-negative; a stats reset produces `NULL`, never a negative value | Snapshot 1 deltas blank (`NULL`) then 1/32, 29/6723, 3/1466 (`docs/results/t2_collector_run.txt`); dashboard prints "unknown" for the first interval (`docs/results/live/dashboard_open_alert.html`); `test_db_stat_deltas_null_first_then_non_negative` passed (`docs/results/t2_pytest.txt`) | PASS (reset branch implemented, not exercised by a captured test) |
| T4 | Blocking capture | The injected wait appears as one `(blocked_pid, blocker_pid)` edge with a plausible `waited_seconds` | Edge 35164 → 42028, `lock_demo_waiter` behind `lock_demo_blocker` (`idle in transaction`), 51.8 s at snapshot 2 and 111.8 s at snapshot 3 (`docs/results/live/alert_sql.txt`) | PASS |
| T5 | Alert firing | Exactly one `blocked_session` alert opens once the observed wait reaches 30 s, severity `critical` | alert_id 1 opened at 21:43:25.214 with `observed_seconds` 51.8 ≥ 30 s; `eligible_at` 21:43:03.388 (`docs/results/live/alert_sql.txt`) | PASS |
| T6 | No duplicate open alert | Sampling the same wait in later cycles extends the open alert instead of inserting another | Snapshot 3 (21:44:25.220) extended alert 1 to 111.8 s; collector logged `alerts_opened=0`; `v_alerts` holds one row (`docs/results/live/collector.log`, `docs/results/live/alert_sql.txt`) | PASS |
| T7 | Resolution | After the lock is released, a later snapshot sets `resolved_at` and status becomes `resolved` | Snapshot 4 (21:45:25.208) `lock_edges=0`, `alerts_resolved=1`, `resolved_at` 21:45:25.208, status `resolved`, `open_alerts_now` 0 (`docs/results/live/alert_sql.txt`) | PASS |
| T8 | Privilege: reader allowed | `monitor_reader` can select every `v_*` view | `test_reader_can_select_every_monitor_view` passed (`docs/results/t2_pytest.txt`); `alert_sql.txt` was itself executed as `monitor_reader` | PASS |
| T9 | Privilege: reader denied | `monitor_reader` cannot select `monitor.alert_log` or `shop.customers`, and cannot insert into `monitor.alert_log` | Three tests passed with SQLSTATE 42501: INSERT `monitor.alert_log`, SELECT `monitor.snapshot`, SELECT `shop.customers` (`docs/results/t2_pytest.txt`) | PASS |
| T10 | Privilege: app denied | `shop_app` cannot read or write anything in `monitor` | Two tests passed: `shop_app` SELECT `monitor.v_alerts` and `monitor.snapshot` both 42501 (`docs/results/t2_pytest.txt`) | PASS |
| T11 | Dashboard agreement | Dashboard shows the same alert ID/PIDs/timestamp as the SQL output | Figure 2 shows alert_id 1, 35164 / 42028, fired 21:43:25, observed 51.8, identical to `docs/results/live/alert_sql.txt` and `docs/results/live/dashboard_open_alert.html` | PASS |
| T12 | Reproducibility | A fresh cluster built from the README reproduces T1–T7 | Fresh clone at `8a0b226`: `start_cluster.sh` as written failed on an MSYS `/tmp` password path and a `PGPORT` fixed before `.env` was sourced; `pytest tests -v` as written failed until `shopdb_t2` / `shopdb_t3` existed. After those fixes the full sequence passed: 18/18 tests, alert 1 fired for 23896 blocked by 19700 at 32.2 s and resolved, notifier printed both transitions, Streamlit health 200, cleanup clean (`docs/results/repro_smoke.txt`). Self-tested by team tooling, not a teammate | PASS after documented fixes |
| T13 | Automated tests | `pytest tests -v` passes | `29 passed in 22.18s` on freshly dropped and recreated test databases (`docs/results/final_pytest.txt`, 22:06:53 CST); per-lane: `11 passed in 18.41s` (`tests/test_extra_rules.py`) and `9 passed in 3.47s` (`tests/test_alert.py`) in `docs/results/t5_extra_rules_pytest.txt`, `9 passed in 0.68s` (`docs/results/t2_pytest.txt`, capture + privileges), `9 passed in 3.28s` (`docs/results/t3_pytest.txt`, alert) | PASS |
| T14 | Collector overhead | Three collector-off and three collector-on `pgbench` runs, identical workload | Not measured | Not measured |
| T15 | Additional rules | `idle_in_transaction` and `long_active_query` each open and resolve against their own test session | Alert 1 (`idle_in_transaction`, pid 22992) fired 21:55:05.198, 60.605 s after injection (0.604 s after eligibility), resolved 21:56:05.192, 20.602 s after session end. Alert 2 (`long_active_query`, pid 31668) fired 21:57:44.958, 60.562 s after query start (0.561 s after eligibility), resolved 21:58:44.953, 20.555 s after session end. Both at a 30 s collector cadence, threshold 60 s, one alert per rule/PID (`docs/results/t5_extra_rules_live.txt`); 11 tests pass (`docs/results/t5_extra_rules_pytest.txt`) | PASS |

The distinction between "implemented" and "verified" is deliberate, and so is the distinction between
the build's own tests and a teammate's independent reproduction. The 29-test full run in
`docs/results/final_pytest.txt` was executed by the build orchestrator on freshly dropped and
recreated test databases after all lanes were merged; it re-runs the team's own suite and is not a
teammate's review. The fresh-clone reproduction (T12) was likewise driven by the team's automated
tooling — self-tested by team tooling, not independently reproduced by a teammate — so it is a
stronger check than the same-checkout suite but is not third-party validation.

# 7. Results & Discussion

## 7.1 Measured outcomes

Table 12: Measurements from the live run. Every value is computed from `docs/results/live/alert_sql.txt`
unless another artifact is named.

| Measurement | Target / definition | Observed | Artifact |
|---|---|---|---|
| Detection delay (injection → alert fired) | Within two collection intervals (≤ 120 s) | 51.77 s (`00:00:51.773833`), 68.2 s inside the target | `docs/results/live/alert_sql.txt` |
| Eligibility → firing | One collection interval at most | 21.83 s (`00:00:21.825649`) — the first sample after `eligible_at` | `docs/results/live/alert_sql.txt` |
| Resolution delay (lock released → resolved) | One collection interval plus evaluation | 21.77 s (`00:00:21.774184`) | `docs/results/live/alert_sql.txt` |
| Notification delay (fired → `notified_at`) | Within one notifier poll (≤ 15 s) | 53.9 ms | `docs/results/live/alert_sql.txt` |
| Resolution notification delay | Within one notifier poll (≤ 15 s) | 66.5 ms | `docs/results/live/alert_sql.txt` |
| Snapshot interval stability | Consecutive `captured_at` values approximately 60 s apart | 59.958 / 60.007 / 59.988 s | `docs/results/live/alert_sql.txt` |
| Observed wait at firing / last open sample | Grows while the wait persists | 51.8 s at snapshot 2, 111.8 s at snapshot 3 | `docs/results/live/alert_sql.txt` |
| Duplicate open alerts while the wait persisted | Zero | 0 (`alerts_opened=0` at snapshot 3; one row in `v_alerts`) | `docs/results/live/collector.log` |
| Idle-in-transaction detection (injection → fired) | 60 s threshold, first sample at or above it | 60.605 s; 0.604 s after `eligible_at`, at a 30 s cadence | `docs/results/t5_extra_rules_live.txt` |
| Idle-in-transaction resolution (session end → resolved) | First clean sample | 20.602 s at a 30 s cadence | `docs/results/t5_extra_rules_live.txt` |
| Long-query detection (query start → fired) | 60 s threshold, first sample at or above it | 60.562 s; 0.561 s after `eligible_at`, at a 30 s cadence | `docs/results/t5_extra_rules_live.txt` |
| Long-query resolution (session end → resolved) | First clean sample | 20.555 s at a 30 s cadence | `docs/results/t5_extra_rules_live.txt` |
| Collector overhead | `< 5 %` `pgbench` throughput impact, three runs each | Not measured | None |
| Reader privilege boundary | Allowed on views, denied on base tables and `shop` | Allowed: 1 view test; denied: 5 tests with SQLSTATE 42501 | `docs/results/t2_pytest.txt` |

## 7.2 Discussion

The timing chain is bounded by the sampling design, and the measurements show where the bounds come
from. The collector sampled at 21:42:25, 21:43:25, 21:44:25 and 21:45:25. The wait began at
21:42:33.388, 8.1 s after the first sample, so the first sample that could observe a wait of at
least 30 s was 21:43:25; the alert could not have fired earlier without a shorter interval or an
event trigger. The 21.8 s between eligibility and firing is therefore one sampling period, not rule
latency. The same logic applies at the other end: the lock was released at 21:45:03.434 and the first
clean sample was 21:45:25.208, so `resolved_at` is the first clean observation rather than the release
instant. Both quantizations are under 60 s and both are disclosed in Section 8.

Against the proposal's target of detection within two 60 s intervals (120 s), the observed 51.8 s
leaves 68.2 s of margin. The operator saw the alert 54 ms after it fired because the notifier polls
every 15 s and picked the new row up on its next pass.

What the design guarantees, independent of any single run: every stored alert is traceable to a
snapshot that observed the wait, the blocking relationship is preserved per sample, duplicate open
alerts for the same pair are impossible by index, and the dashboard role cannot read or write
anything beyond the reporting views. Those are properties of the schema and grants in this
repository, and they can be checked by reading the SQL.

The two extra rules extend the same lifecycle to non-lock conditions and confirm that the sampler is
the system's clock. At the 30 s cadence used for their demonstrations, `idle_in_transaction` fired
60.605 s after the session entered `idle in transaction` and `long_active_query` fired 60.562 s after
`query_start`; in both runs eligibility fell about half a second before a scheduled sample, so the
alert waited one sample. Resolution took 20.602 s and 20.555 s after the sessions ended, again one
sample. These delays describe the cadence, not the rules: at the default 60 s cadence the worst case
is one further interval. Both rules take `first_seen_at` from `state_change` or `query_start`, so a
new transaction or statement by the same PID is a new episode rather than a continuation.

The fresh-clone reproduction adds a different kind of evidence: it rebuilt the cluster from the
README on a clean checkout and reproduced the whole sequence, including the alert pair 23896 / 19700
firing at 32.2 s and resolving, the notifier output and the dashboard health check. It also exposed
three documentation defects that the same-checkout runs had hidden — the `/tmp` password path, the
port fixed before `.env`, and the missing test databases — which are now fixed in the scripts and
documented in the README. That is the practical value of a fresh-clone check even when it is run by
the team's own tooling rather than by a teammate.

The honest reading of the delivered scope is that the system demonstrates the *method* on three
rules. It does not demonstrate that six rules work, that the dashboard is a full DBA console, or that
collection overhead is negligible. Section 8 records each gap.

# 8. Challenges & Limitations

## 8.1 Promised vs delivered

Table 13: Approved proposal scope against the delivered build, with the reason for each gap.

| Area | Proposed | Delivered | Reason / disclosure |
|---|---|---|---|
| DBMS version | PostgreSQL 16 | PostgreSQL 17.11 | 17 was the installed server; the approved 16 image was not available |
| Provisioning | Docker Compose, one command | `initdb` disposable cluster in `./.pgdata` on port 55432 | Docker was not installed on the build machine; no admin rights |
| Scheduler | `pg_cron` | Python scheduler loop in `scripts/collect.py` | `pg_cron` is not packaged with the Windows build; Python keeps one scheduler |
| Dashboard | Provisioned Grafana | Streamlit on port 8501 | Grafana requires the same missing container stack |
| Shop schema | 6 tables (`customers`, `products`, `orders`, `order_items`, `inventory`, `payments`) | 2 tables (`customers`, `orders`) | Two tables are sufficient to hold a row lock; time-boxed build |
| Seed size | ~100,000 customers, ~1,000,000 orders | 1,000 customers, 10,000 orders | Fast, deterministic setup; disclosed |
| History tables | Session, statement, table, index, lock, database | Session, lock, database counters | Statement/table/index history not implemented |
| Analysis layer | Multiple views, materialized view, hourly rollup | Six reader views, no materialized view, no rollup | Not implemented |
| Retention | `purge_old_snapshots(interval)` | None | Not implemented; history grows until the cluster is deleted |
| Alert rules | 6 (blocked, long-running, idle-in-transaction, saturation, cache ratio, bloat) | 3 of 6 verified live: `blocked_session`, `idle_in_transaction`, `long_active_query`; saturation, cache ratio and bloat not implemented | Three rules demonstrated end to end with captured fire/resolve evidence; the remaining three are unimplemented |
| Overhead target | `< 5 %` `pgbench` impact | Not measured | No matched `pgbench` runs were executed |
| Recording | Required video demonstration | Outstanding | Deferred by the team; submission incomplete without it |

## 8.2 Design limitations

**Sampling misses short events.** The collector observes the server every 60 seconds. A lock wait
that begins and ends between two samples leaves no trace in `monitor.lock_wait`, although the server
log may still record it because `log_lock_waits` is on. The demonstration holds the lock for 150
seconds precisely to span multiple samples; that is a property of the demonstration, not of the
collector. The two extra rules were measured at a 30 s cadence for the same reason — to keep their
100 s sessions inside the evidence window — while their thresholds remain 60 s.

**`resolved_at` is a sampled boundary.** Resolution is recorded by the first snapshot that no longer
sees the wait, so the stored time can lag the real release by up to one collection interval. In the
captured run the lock was released at 21:45:03.434 and `resolved_at` is 21:45:25.208, a 21.8 s
quantization. The same applies to firing: the alert fires at the first sample where the observed wait
is at least the threshold, not at the instant the threshold is crossed (21.8 s later in this run).
The timestamps are honest observations; they are not event times.

**Alert identity across PID reuse.** An open alert is keyed on `(rule_name, blocked_pid)`. The rule
distinguishes a new wait episode by the wait start time, so a later wait by the same PID closes the
old alert and opens a new one (`test_pid_reuse_after_previous_observation_starts_new_alert`). If a
different backend reused the PID and began waiting within the same wait-start window, it would be
attributed to the existing alert. The heuristic is visible in the schema and disclosed here rather
than hidden.

**One blocker per alert.** When a backend waits on several blockers, the alert records the lowest
blocker PID and the longest observed wait (`test_two_blockers_picks_lowest_blocker_pid`). The other
edges remain in `monitor.lock_wait` and are visible in the lock history, but the alert row itself
names only one blocker.

**Counters, not rates.** `monitor.db_stat_sample` stores cumulative counters and their deltas. Any
"per second" figure is a derived value that depends on the actual gap between samples, which varies
with scheduler jitter. The design therefore avoids storing rates that would look precise while
being wrong after a pause.

**Single instance, single node.** There is no replication, no failover, no multi-server aggregation
and no OS-level metric collection (CPU, memory, disk I/O). The proposal listed those as out of scope
and they remain out of scope.

**No retention or archival.** Nothing purges old snapshots. On a long-running instance the
`monitor` schema would grow without bound; the correct next step is a purge function driven by an
interval parameter, which the proposal specified and this build omits.

**Local-only security posture.** `scram-sha-256` over loopback with passwords in a gitignored `.env`
is appropriate for a disposable teaching cluster, not for production. There is no TLS, no audit
logging, no password rotation and no secret manager.

**Alerting is console-only.** There is no email, paging or escalation, and no deduplication across
process restarts beyond the database-level uniqueness of an open alert. If the notifier is not
running, alerts are still recorded but nobody is told.

**Dashboard scope.** The dashboard is a Streamlit application, not a provisioned Grafana dashboard,
and its panels reflect the views that exist: timeline, lock history, current blockers, alerts. There
are no statement, index-usage, table-health or cache-ratio panels because the underlying history
does not exist.

# 9. Team Contributions

Table 14: Module-based contributions. Work that has not produced a concrete artifact is marked pending rather than credited.

| Member | Implementation modules | Report contributions | Video / demonstration |
|---|---|---|---|
| Quan Nguyen | Disposable cluster scripts, roles and grants, `shop` seed, `monitor` schema and views, collector integration, alert integration, Streamlit dashboard, notifier integration, live evidence run, report builder | Architecture, database design, DBA implementation, evidence, verification and limitations sections; README | Planned: architecture, collector and dashboard walkthrough — pending recording |
| Dustin Dowell | Proposed: independent reproducibility smoke test on a fresh cluster — **pending, fill in actual work products** | Proposed: environment and reproduction paragraph — **pending** | Pending — not yet recorded |
| Zach Riggle | Proposed: independent privilege and alert-resolution test — **pending, fill in actual work products** | Proposed: expected-vs-actual verification write-up — **pending** | Pending — not yet recorded |

The proposal assigned modules differently (Dustin: environment and collector; Quan: workload, views
and dashboard; Zach: alerting, notifier and reproducibility). The table above records what was
actually built for this submission, not the original plan. No contribution is claimed for a teammate
without a concrete work product; where a task was proposed but not completed, it is marked pending.

Quan's implementation and this report were produced with AI agent assistance (Hermes Agent, Nous
Research), which drafted code and SQL, ran the build and test commands, captured the evidence files
and assembled this document under his direction. The agent is tooling, not a team member, and is
disclosed here rather than presented as independent human work. The 29-test full suite in
`docs/results/final_pytest.txt` was re-run by the build orchestrator on freshly dropped and recreated
databases, and the fresh-clone reproduction in `docs/results/repro_smoke.txt` was executed by the
team's automated tooling. Both are the team testing its own build, not independent teammate work, so
the Dustin and Zach rows above remain pending and no third party has yet reproduced the build.

# 10. Conclusion & Future Work

The build delivers a complete monitoring loop on a disposable PostgreSQL 17.11 instance: scheduled
collection into a `monitor` schema, historical preservation of blocking relationships, a
threshold-based `blocked_session` rule with an explicit eligibility and resolution lifecycle, a
console notifier, and a read-only dashboard reached through six sanitized views under a dedicated
role. The design is verifiable by inspection: the grants are in SQL, the alert uniqueness is an
index, and the lifecycle is a set of timestamps rather than a status flag that can drift.

What the stored history proves is concrete: for alert 1 the database retains the pair (35164 waiting
on 42028), the fact that the blocker sat `idle in transaction`, the wait start, the threshold
crossing, the firing time, the 111.8 s the wait was observed, the release, the resolution and the two
notification timestamps. The dashboard and the SQL read the same rows under the same read-only role.
Reconstructing a contention incident after it happened is the gap the project set out to close, and
it is demonstrated end to end for three rules.

Future work, in the order that would add the most value:

1. Record the required demonstration with all three members narrating a module; the video is the only
   required deliverable still outstanding.
2. Have a teammate independently repeat the fresh-clone reproduction, so the T12 check becomes
   third-party validation rather than the team's own tooling testing itself.
3. Implement and verify the three remaining rules — connection saturation, five-minute cache hit
   ratio and dead-tuple ratio. Each needs its own test scenario and captured artifact; the three
   implemented rules show the pattern.
4. Add retention: `monitor.purge_old_snapshots(interval)` plus a scheduled call, so history does not
   grow without bound.
5. Extend history to statement, table and index statistics, then add the analysis views and hourly
   rollup the proposal described.
6. Measure collector overhead with matched `pgbench` runs (three off, three on, identical workload)
   instead of asserting a target.
7. Restore the approved stack where available — PostgreSQL 16, `pg_cron`, Grafana provisioned from
   JSON — or document the Python/Streamlit substitutions as the supported deployment.
8. Restore the full six-table shop schema and the larger seed once the demonstration path is stable.

# References

1. PostgreSQL Global Development Group. *The Statistics Collector* (PostgreSQL 17 documentation).
   https://www.postgresql.org/docs/17/monitoring-stats.html
2. PostgreSQL Global Development Group. *pg_stat_activity* (PostgreSQL 17 documentation).
   https://www.postgresql.org/docs/17/monitoring-stats.html#MONITORING-PG-STAT-ACTIVITY-VIEW
3. PostgreSQL Global Development Group. *pg_stat_database* (PostgreSQL 17 documentation).
   https://www.postgresql.org/docs/17/monitoring-stats.html#MONITORING-PG-STAT-DATABASE-VIEW
4. PostgreSQL Global Development Group. *System Information Functions and Operators* —
   `pg_blocking_pids()` (PostgreSQL 17 documentation).
   https://www.postgresql.org/docs/17/functions-info.html
5. PostgreSQL Global Development Group. *pg_locks* (PostgreSQL 17 documentation).
   https://www.postgresql.org/docs/17/view-pg-locks.html
6. PostgreSQL Global Development Group. *pg_stat_statements* (PostgreSQL 17 documentation).
   https://www.postgresql.org/docs/17/pgstatstatements.html
7. PostgreSQL Global Development Group. *Predefined Roles* (PostgreSQL 17 documentation).
   https://www.postgresql.org/docs/17/predefined-roles.html
8. PostgreSQL Global Development Group. *Explicit Locking* (PostgreSQL 17 documentation).
   https://www.postgresql.org/docs/17/explicit-locking.html
9. Psycopg Development Team. *Psycopg 3 documentation*.
   https://www.psycopg.org/psycopg3/docs/
10. Streamlit. *Streamlit documentation*.
    https://docs.streamlit.io/
11. python-docx. *python-docx documentation*.
    https://python-docx.readthedocs.io/
12. Citus Data. *pg_cron* (referenced as the proposed scheduler, not used in this build).
    https://github.com/citusdata/pg_cron
13. Grafana Labs. *Grafana documentation* (referenced as the proposed dashboard, not used in this build).
    https://grafana.com/docs/
14. Project proposal. *PostgreSQL Database Monitoring Dashboard* (CSCE 4355, Fall 2026). Repository
    file `DB_Monitoring_Dashboard_Project_Proposal.pdf`; the scope it promised is compared with the
    delivered build in Section 8.
