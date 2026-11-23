#!/usr/bin/env bash
# scripts/setup_db.sh — create (or recreate) the project database on the
# disposable local cluster started by scripts/start_cluster.sh.
# Usage: scripts/setup_db.sh [dbname]     (default: $PGDATABASE from .env, else shopdb)
# Generates .env with random role passwords on first run (never committed).
set -euo pipefail
cd "$(dirname "$0")/.."
PGBIN="${PGBIN:-/c/Program Files/PostgreSQL/17/bin}"
export PATH="$PGBIN:$PATH"

if [ ! -f .env ]; then
  rnd() { python - <<'PY'
import secrets; print(secrets.token_urlsafe(18))
PY
  }
  cat > .env <<EOF
PGHOST=127.0.0.1
PGPORT=${PGPORT:-55432}
PGDATABASE=shopdb
PG_SUPERUSER=postgres
PG_SUPERUSER_PASSWORD=$(rnd)
SHOP_APP_PASSWORD=$(rnd)
MONITOR_COLLECTOR_PASSWORD=$(rnd)
MONITOR_READER_PASSWORD=$(rnd)
COLLECT_INTERVAL_SECONDS=60
EOF
  echo "wrote .env (gitignored)"
fi
set -a; . ./.env; set +a
DB="${1:-$PGDATABASE}"
export PGPASSWORD="$PG_SUPERUSER_PASSWORD"
PSQL=(psql -h "$PGHOST" -p "$PGPORT" -U "$PG_SUPERUSER" -v ON_ERROR_STOP=1 -q)

"${PSQL[@]}" -d postgres -v dbname="$DB" -v app_pw="$SHOP_APP_PASSWORD" \
  -v collector_pw="$MONITOR_COLLECTOR_PASSWORD" -v reader_pw="$MONITOR_READER_PASSWORD" -f sql/00_roles.sql
# every numbered script after 00_roles.sql, in name order (01 shop, 10-14 monitor
# schema/collector/purge, 20-21 alerts, 90 pg_cron jobs) -- the same order the
# Docker init script (docker/postgres/initdb/10_project_init.sh) uses
for f in sql/[0-9][0-9]_*.sql; do
  case "$(basename "$f")" in 00_*) continue ;; esac
  "${PSQL[@]}" -d "$DB" -f "$f" && echo "applied $f to $DB"
done
"${PSQL[@]}" -d "$DB" -Atc "select 'server='||current_setting('server_version'),
  'pg_stat_statements='||(select extversion from pg_extension where extname='pg_stat_statements'),
  'customers='||(select count(*) from shop.customers), 'orders='||(select count(*) from shop.orders),
  'products='||(select count(*) from shop.products), 'order_items='||(select count(*) from shop.order_items),
  'payments='||(select count(*) from shop.payments), 'inventory='||(select count(*) from shop.inventory)"
