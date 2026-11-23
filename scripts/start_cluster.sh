#!/usr/bin/env bash
# scripts/start_cluster.sh — create (first run) and start a disposable, non-admin
# PostgreSQL 17 cluster in ./.pgdata on 127.0.0.1:55432. Never touches the
# Windows PostgreSQL service on :5432.
#   scripts/start_cluster.sh          init (if needed) + start
#   scripts/start_cluster.sh stop     stop the cluster
set -euo pipefail
cd "$(dirname "$0")/.."
PGBIN="${PGBIN:-/c/Program Files/PostgreSQL/17/bin}"
export PATH="$PGBIN:$PATH"
PGDATA_DIR="${PGDATA_DIR:-.pgdata}"
[ -f .env ] && { set -a; . ./.env; set +a; }   # .env (if present) sets PGPORT
PORT="${PGPORT:-55432}"

if [ "${1:-start}" = "stop" ]; then pg_ctl -D "$PGDATA_DIR" -m fast stop; exit 0; fi

if [ ! -f "$PGDATA_DIR/PG_VERSION" ]; then
  [ -f .env ] || { echo "run scripts/setup_db.sh once? no: create .env first (cp .env.example .env and set PG_SUPERUSER_PASSWORD)"; exit 1; }
  # pwfile inside the project dir: native initdb.exe cannot read MSYS /tmp paths
  pwfile="$(mktemp ./.pgpw.XXXXXX)"; printf '%s\n' "$PG_SUPERUSER_PASSWORD" > "$pwfile"
  initdb -D "$PGDATA_DIR" -U postgres --auth=scram-sha-256 --pwfile="$pwfile" -E UTF8 --locale=C >/dev/null
  rm -f "$pwfile"
  cat >> "$PGDATA_DIR/postgresql.conf" <<EOF
# --- CSCE4355 monitoring project ---
listen_addresses = '127.0.0.1'
port = $PORT
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.track = all
track_activity_query_size = 2048
log_lock_waits = on
deadlock_timeout = 1s
EOF
fi
pg_ctl -D "$PGDATA_DIR" -l "$PGDATA_DIR/server.log" -w status >/dev/null 2>&1 || \
  pg_ctl -D "$PGDATA_DIR" -l "$PGDATA_DIR/server.log" -w start
pg_isready -h 127.0.0.1 -p "$PORT"
