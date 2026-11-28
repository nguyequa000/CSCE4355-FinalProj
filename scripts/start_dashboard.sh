#!/usr/bin/env bash
# scripts/start_dashboard.sh
#
# One command for the whole app:
#   * creates .env with random passwords on first run (never committed)
#   * builds and starts postgres + dashboard + notifier
#   * waits until the dashboard answers, then prints its URL
#
# Prerequisites: Docker Engine/Desktop with Compose v2 and a running daemon,
# curl, and Python 3 (Python only when this script must create .env). The
# docker CLI must be on PATH; on Windows, if Docker runs inside WSL (as on the
# machine used for the recorded run), run this script from WSL:
#   wsl.exe -e bash scripts/start_dashboard.sh
#
# Usage:  bash scripts/start_dashboard.sh
# Stop:   bash scripts/stop_dashboard.sh          (keep history)
#         bash scripts/stop_dashboard.sh --reset  (delete the data volume)
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed or not on PATH." >&2
  if command -v wsl.exe >/dev/null 2>&1 && wsl.exe -e docker --version >/dev/null 2>&1; then
    echo "       Docker is available inside WSL, not in this shell. From this directory run:" >&2
    echo "         wsl.exe -e bash scripts/start_dashboard.sh" >&2
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
if ! command -v curl >/dev/null 2>&1; then
  echo "error: curl is required to wait for the dashboard health endpoint." >&2
  echo "       Install curl (Git Bash and WSL include it), then re-run." >&2
  exit 1
fi

if [ ! -f .env ]; then
  PYTHON=""
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
       && "$candidate" -c 'import secrets' >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
  if [ -z "$PYTHON" ]; then
    echo "error: Python 3 is required on the first run to write .env with random passwords." >&2
    echo "       Install Python 3, or copy .env.example to .env, replace every change-me" >&2
    echo "       value, and re-run." >&2
    exit 1
  fi

  echo "No .env found — generating one with random role passwords."
  (
    umask 077
    "$PYTHON" - <<'PY'
import os
import secrets


def pw() -> str:
    # URL/psql-safe: no quotes, spaces or shell metacharacters
    return secrets.token_urlsafe(24)


env = f"""PGHOST=127.0.0.1
PGPORT=55432
PGDATABASE=shopdb
PG_SUPERUSER=postgres
PG_SUPERUSER_PASSWORD={pw()}
SHOP_APP_PASSWORD={pw()}
MONITOR_COLLECTOR_PASSWORD={pw()}
MONITOR_READER_PASSWORD={pw()}
COLLECT_INTERVAL_SECONDS=60
DASHBOARD_PORT=8501
GRAFANA_ADMIN_PASSWORD={pw()}
"""
# O_EXCL: never overwrite an .env that appeared after the check above.
# newline="\n" keeps the file LF-only even when generated on Windows.
fd = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(env)
PY
  )
fi

# Credential file: try owner-only POSIX mode, but do not pretend it worked.
# Git Bash and WSL /mnt/c do not enforce chmod modes, so report the effective
# mode and leave Windows users to restrict the NTFS ACL (README §13).
chmod 600 .env 2>/dev/null || true
MODE="$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env 2>/dev/null || true)"
case "$MODE" in
  ""|400|600) ;;
  *)
    echo "warning: .env has POSIX mode $MODE here; this filesystem does not enforce owner-only modes." >&2
    echo "         On Windows, restrict its NTFS permissions to your account (README §13)." >&2
    ;;
esac

echo "Building and starting the stack..."
docker compose up -d --build

# Ask Compose for the mapping it actually created instead of re-reading
# DASHBOARD_PORT from .env: shell environment values override .env during
# compose interpolation, and the .env value may be quoted or absent.
MAPPING="$(docker compose port dashboard 8501 2>/dev/null | tr -d '\r' | head -n 1 || true)"
PORT="${MAPPING##*:}"
case "$PORT" in
  ''|*[!0-9]*)
    echo "error: could not determine the dashboard's published port (got '${MAPPING:-no mapping}')." >&2
    echo "       The dashboard container may not have started. Check:" >&2
    echo "         docker compose ps" >&2
    echo "         docker compose logs dashboard --tail 100" >&2
    exit 1
    ;;
esac

echo -n "Waiting for the dashboard"
for _ in $(seq 1 90); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/_stcore/health" >/dev/null 2>&1; then
    echo
    echo "Dashboard ready:  http://localhost:${PORT}"
    echo "Demo traffic:     docker compose run --rm demo"
    echo "Logs:             docker compose logs -f dashboard"
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo
echo "The containers were started but the dashboard has not answered on 127.0.0.1:${PORT} after 90 attempts." >&2
echo "Check: docker compose ps" >&2
echo "       docker compose logs dashboard --tail 100" >&2
exit 1
