#!/usr/bin/env bash
# scripts/stop_dashboard.sh
#
# Stop the Compose stack. History in the pgdata volume is kept.
#   bash scripts/stop_dashboard.sh            # stop containers, keep data
#   bash scripts/stop_dashboard.sh --reset    # stop and delete the data volume
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
  echo "Usage: bash scripts/stop_dashboard.sh [--reset]" >&2
  echo "  (no option)  stop the containers and keep the pgdata volume" >&2
  echo "  --reset      stop the containers and delete the pgdata volume" >&2
}

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed or not on PATH." >&2
  if command -v wsl.exe >/dev/null 2>&1 && wsl.exe -e docker --version >/dev/null 2>&1; then
    echo "       Docker is available inside WSL, not in this shell. From this directory run:" >&2
    echo "         wsl.exe -e bash scripts/stop_dashboard.sh" >&2
  else
    echo "       Install Docker Engine or Docker Desktop with Compose v2, then re-run." >&2
  fi
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "error: docker compose v2 is required (the 'docker compose' plugin, not docker-compose)." >&2
  echo "       Install or enable the Compose v2 plugin, then re-run." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "error: the Docker daemon is not running or not reachable by this user." >&2
  echo "       Start Docker Desktop or the Docker service (and its WSL distro), then re-run." >&2
  exit 1
fi

if [ "$#" -gt 1 ]; then
  echo "error: too many arguments: $*" >&2
  usage
  exit 2
fi

# docker compose down interpolates the compose file, so a deleted .env would
# otherwise stop the command with "required variable ... is missing". down
# does not use these values; they only satisfy interpolation.
down() {
  if [ -f .env ]; then
    docker compose down "$@"
  else
    echo "warning: .env not found; stopping with throwaway values (stopping needs no credentials)." >&2
    PG_SUPERUSER_PASSWORD=throwaway \
    SHOP_APP_PASSWORD=throwaway \
    MONITOR_COLLECTOR_PASSWORD=throwaway \
    MONITOR_READER_PASSWORD=throwaway \
      docker compose down "$@"
  fi
}

case "${1:-}" in
  "")
    echo "Stopping the stack (history kept)..."
    down
    ;;
  --reset)
    echo "Stopping the stack and deleting the database volume..."
    down -v
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    echo "error: unknown option: $1" >&2
    usage
    exit 2
    ;;
esac
