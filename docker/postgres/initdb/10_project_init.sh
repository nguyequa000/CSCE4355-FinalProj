#!/usr/bin/env bash
# docker/postgres/initdb/10_project_init.sh  (M1 - Dustin Dowell)
#
# Runs once, on the first start of an empty data volume, from the official
# image's /docker-entrypoint-initdb.d hook. Applies the numbered scripts in
# /project/sql (bind-mounted read-only from ./sql) in order:
#
#   00_roles.sql            against "postgres" (roles + project database)
#   [01-99]_*.sql           against the project database, sorted by name
#
# The same script builds an extra database (for example a test database) on a
# running container:
#
#   docker compose exec postgres /docker-entrypoint-initdb.d/10_project_init.sh shopdb_t2
#
# Passwords come from the container environment (docker-compose.yml maps them
# from .env); they are passed to psql as variables and never written to disk.
set -euo pipefail

DB="${1:-${APP_DB:-shopdb}}"
SQL_DIR="${SQL_DIR:-/project/sql}"
: "${SHOP_APP_PASSWORD:?SHOP_APP_PASSWORD is not set}"
: "${MONITOR_COLLECTOR_PASSWORD:?MONITOR_COLLECTOR_PASSWORD is not set}"
: "${MONITOR_READER_PASSWORD:?MONITOR_READER_PASSWORD is not set}"

PSQL=(psql -v ON_ERROR_STOP=1 -q --no-psqlrc --username "${POSTGRES_USER:-postgres}")

"${PSQL[@]}" --dbname postgres \
  -v dbname="$DB" \
  -v app_pw="$SHOP_APP_PASSWORD" \
  -v collector_pw="$MONITOR_COLLECTOR_PASSWORD" \
  -v reader_pw="$MONITOR_READER_PASSWORD" \
  -f "$SQL_DIR/00_roles.sql"
echo "project-init: applied 00_roles.sql (database $DB)"

for f in "$SQL_DIR"/[0-9][0-9]_*.sql; do
  case "$(basename "$f")" in 00_*) continue ;; esac
  "${PSQL[@]}" --dbname "$DB" -f "$f"
  echo "project-init: applied $(basename "$f") to $DB"
done

"${PSQL[@]}" --dbname "$DB" -Atc "
  SELECT 'server=' || current_setting('server_version')
  UNION ALL SELECT 'pg_stat_statements=' || coalesce((SELECT extversion FROM pg_extension WHERE extname = 'pg_stat_statements'), 'absent')
  UNION ALL SELECT 'pg_cron=' || coalesce((SELECT extversion FROM pg_extension WHERE extname = 'pg_cron'), 'absent (not the cron database)')"
