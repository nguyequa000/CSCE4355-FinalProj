#!/usr/bin/env bash
# workload/run_pgbench.sh — baseline shop load with the custom pgbench scripts.
#
# Usage: workload/run_pgbench.sh [--profile read|write|mixed] [--clients N]
#                                [--seconds S] [--database DB]
#   read   workload/pgbench/read_heavy.sql
#   write  workload/pgbench/write_heavy.sql
#   mixed  both, weighted 8:2 read:write (default)
# Runs as shop_app with the password from .env; nothing is printed or stored.
set -euo pipefail
cd "$(dirname "$0")/.."
PGBIN="${PGBIN:-/c/Program Files/PostgreSQL/17/bin}"
PROFILE=mixed; CLIENTS=4; SECONDS_RUN=30; DB=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)  PROFILE="$2"; shift 2 ;;
    --clients)  CLIENTS="$2"; shift 2 ;;
    --seconds)  SECONDS_RUN="$2"; shift 2 ;;
    --database) DB="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -f .env ] || { echo "missing .env (run scripts/setup_db.sh first)" >&2; exit 1; }
set -a; . ./.env; set +a
DB="${DB:-$PGDATABASE}"
case "$PROFILE" in
  read)  FILES=(-f workload/pgbench/read_heavy.sql) ;;
  write) FILES=(-f workload/pgbench/write_heavy.sql) ;;
  mixed) FILES=(-f workload/pgbench/read_heavy.sql@8 -f workload/pgbench/write_heavy.sql@2) ;;
  *) echo "profile must be read, write or mixed" >&2; exit 2 ;;
esac
export PGPASSWORD="$SHOP_APP_PASSWORD"
exec "$PGBIN/pgbench" -n -h "$PGHOST" -p "$PGPORT" -U shop_app \
  -c "$CLIENTS" -j "$CLIENTS" -T "$SECONDS_RUN" "${FILES[@]}" "$DB"
