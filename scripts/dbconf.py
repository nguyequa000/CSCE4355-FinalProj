"""Shared connection helper. Reads ./.env (gitignored) — no credentials in code.

    from scripts.dbconf import connect
    conn = connect("monitor_reader")   # or shop_app / monitor_collector / postgres
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
_PW_KEYS = {
    "postgres": "PG_SUPERUSER_PASSWORD",
    "shop_app": "SHOP_APP_PASSWORD",
    "monitor_collector": "MONITOR_COLLECTOR_PASSWORD",
    "monitor_reader": "MONITOR_READER_PASSWORD",
}


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k in _PW_KEYS.values() or k.startswith("PG")})
    return env


def connect(role: str, dbname: str | None = None, **kw) -> psycopg.Connection:
    env = load_env()
    return psycopg.connect(
        host=env.get("PGHOST", "127.0.0.1"),
        port=int(env.get("PGPORT", "55432")),
        dbname=dbname or env.get("PGDATABASE", "shopdb"),
        user=role,
        password=env[_PW_KEYS[role]],
        application_name=kw.pop("application_name", role),
        **kw,
    )
