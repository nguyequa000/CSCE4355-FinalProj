#!/usr/bin/env bash
# scripts/bench_overhead.sh -- collector overhead measurement (Lane P).
#
# Six interleaved pgbench runs off,on,off,on,off,on against shopdb_bench.
# Data is re-initialised (pgbench -i -s 10 -q) before EVERY run so both arms
# see identical seeded data.  "on" runs a collector (scripts/collect.py
# --interval 10 --db shopdb_bench) for the whole run; "off" runs none.
# Snapshot rows captured inside each on-run window prove collection happened.
#
# Usage: scripts/bench_overhead.sh [dbname]      (default shopdb_bench)
# Env:   PY=/path/to/python (python that can import psycopg)
#        PGBIN=/path/to/postgres/bin
#
# Raw pgbench output and the summary table are written to
# docs/results/perf_overhead.txt.
set -euo pipefail
cd "$(dirname "$0")/.."

PGBIN="${PGBIN:-/c/Program Files/PostgreSQL/17/bin}"
PGBENCH="$PGBIN/pgbench"
PY="${PY:-python}"
DB="${1:-shopdb_bench}"
OUT="docs/results/perf_overhead.txt"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

[ -f .env ] || { echo "missing .env" >&2; exit 1; }
set -a; . ./.env; set +a
export PGPASSWORD="$PG_SUPERUSER_PASSWORD"
export PGUSER="$PG_SUPERUSER"
PSQL=("$PGBIN/psql" -h "$PGHOST" -p "$PGPORT" -U "$PG_SUPERUSER" -v ON_ERROR_STOP=1 -Atq)

mkdir -p docs/results
{
  echo "Collector overhead -- pgbench, shopdb_bench"
  echo "Started: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "Server: $("${PSQL[@]}" -d "$DB" -c "select version()")"
  echo "pgbench: $("$PGBENCH" --version)"
  echo "Design: 6 runs interleaved off,on,off,on,off,on; re-init (pgbench -i -s 10 -q)"
  echo "        before EVERY run; each run: pgbench -c 8 -j 4 -T 60 -n -P 10."
  echo "        'on' = scripts/collect.py --interval 10 --db $DB in background"
  echo "        for the whole run (10 s is more aggressive than the 60 s default)."
  echo "NOTE: other agents/tests may run on this host concurrently; runs are"
  echo "      interleaved to spread noise, and no statistical significance is claimed."
  echo
} > "$OUT"

declare -a MODES=() TPS=() SNAPS=()
off_tps=(); on_tps=()

for i in 1 2 3 4 5 6; do
  if [ $((i % 2)) -eq 1 ]; then mode=off; else mode=on; fi

  echo "== re-initialising pgbench tables (run $i, mode $mode) ==" >&2
  "$PGBENCH" -i -s 10 -q "$DB" >"$TMP/init_$i.log" 2>&1

  win_start=""; win_end=""; cpid=""; winpid=""; snaps="n/a"
  if [ "$mode" = on ]; then
    win_start="$("${PSQL[@]}" -d "$DB" -c "select clock_timestamp()")"
    # Start the collector as an MSYS background job, then map its MSYS PID to the
    # Windows PID (ps -W column 4) so Stop-Process kills exactly this process.
    "$PY" scripts/collect.py --interval 10 --db "$DB" >/dev/null 2>"$TMP/collector_$i.err" &
    cpid=$!
    sleep 3   # let the collector take its first snapshot before pgbench starts
    winpid="$(ps -W | awk -v p="$cpid" '$1 == p {print $4; exit}')"
  fi

  echo "== run $i ($mode): pgbench ==" >&2
  set +e
  "$PGBENCH" -c 8 -j 4 -T 60 -n -P 10 "$DB" >"$TMP/run_$i.out" 2>&1
  rc=$?
  set -e

  if [ "$mode" = on ]; then
    win_end="$("${PSQL[@]}" -d "$DB" -c "select clock_timestamp()")"
    if [ -n "${winpid:-}" ]; then
      powershell -NoProfile -Command "Stop-Process -Id $winpid -Force" 2>/dev/null || true
      wait "$cpid" 2>/dev/null || true
    fi
    snaps="$("${PSQL[@]}" -d "$DB" -c "select count(*) from monitor.snapshot where captured_at >= '$win_start' and captured_at <= '$win_end'")"
  fi

  tps="$(sed -n 's/^tps = \([0-9.]*\).*/\1/p' "$TMP/run_$i.out" | head -1)"
  [ -n "$tps" ] || tps="NA"
  MODES+=("$mode"); TPS+=("$tps"); SNAPS+=("$snaps")
  if [ "$mode" = off ]; then off_tps+=("$tps"); else on_tps+=("$tps"); fi

  {
    echo "------------------------------------------------------------------"
    echo "RUN $i  mode=$mode  tps=$tps  snapshots_in_window=$snaps"
    [ -n "$win_start" ] && echo "window: $win_start .. $win_end"
    [ -n "$winpid" ] && echo "collector winpid: $winpid"
    echo "--- pgbench raw output ---"
    cat "$TMP/run_$i.out"
    if [ -s "$TMP/collector_$i.err" ]; then
      echo "--- collector stderr (tail) ---"
      tail -5 "$TMP/collector_$i.err"
    fi
    echo "pgbench exit: $rc"
    echo
  } >> "$OUT"
done

mean() { awk '{s+=$1; n++} END {if (n) printf "%.2f", s/n; else print "NA"}' <<<"$*"; }
m_off="$(printf '%s\n' "${off_tps[@]}" | awk '{s+=$1;n++} END{printf "%.2f", s/n}')"
m_on="$(printf '%s\n' "${on_tps[@]}" | awk '{s+=$1;n++} END{printf "%.2f", s/n}')"
pct="$(awk -v o="$m_off" -v n="$m_on" 'BEGIN{if(o>0) printf "%+.2f", (n-o)/o*100; else print "NA"}')"

{
  echo "=================================================================="
  echo "SUMMARY (TPS excludes pgbench initial connection time)"
  echo
  printf '%-5s %-4s %12s %12s\n' run mode tps snapshots
  for i in 0 1 2 3 4 5; do
    printf '%-5s %-4s %12s %12s\n' "$((i+1))" "${MODES[$i]}" "${TPS[$i]}" "${SNAPS[$i]}"
  done
  echo
  echo "mean off TPS: $m_off  (n=3)"
  echo "mean on  TPS: $m_on  (n=3)"
  echo "difference (on vs off): $pct %"
  echo
  echo "Comparison method: plain mean of 3 collector-off vs 3 collector-on runs,"
  echo "interleaved off/on to spread background load; percentage = (mean_on -"
  echo "mean_off)/mean_off*100. This is an observed comparison only -- 3 runs per"
  echo "arm cannot support a significance claim, and other agents were running"
  echo "tests on the same host. Collector interval was 10 s (more aggressive than"
  echo "the 60 s default), so this bounds overhead conservatively for the demo."
  echo "Finished: $(date '+%Y-%m-%d %H:%M:%S %Z')"
} >> "$OUT"

echo "wrote $OUT" >&2
cat "$OUT" | tail -30
