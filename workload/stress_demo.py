#!/usr/bin/env python3
"""Write workload for the dead_tuple_risk and connection_saturation demos.

Updates a slice of shop.orders (creating dead tuple versions), and can hold
extra idle shop_app connections open so a collector snapshot sees a higher
backend count.  Runs as shop_app; no credentials are printed.

    python workload/stress_demo.py --database shopdb_r2 --rows 3000
    python workload/stress_demo.py --database shopdb_r2 --connections 3 --hold 15

The caller is responsible for making the new statistics visible to the next
snapshot (SELECT pg_stat_force_next_flush(); or wait ~1 s).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=None,
                        help="database name (default: PGDATABASE from .env)")
    parser.add_argument("--rows", type=int, default=0,
                        help="update shop.orders rows with order_id <= N (0 = none)")
    parser.add_argument("--connections", type=int, default=0,
                        help="extra idle shop_app connections to hold open")
    parser.add_argument("--hold", type=float, default=0.0,
                        help="seconds to keep the extra connections open")
    parser.add_argument("--log", default=None,
                        help="optional JSONL event log path")
    args = parser.parse_args(argv)

    events: list[dict] = []
    if args.rows > 0:
        with connect("shop_app", dbname=args.database,
                     application_name="r2_stress_update") as conn:
            updated = conn.execute(
                "UPDATE shop.orders SET total_amount = total_amount + 1 "
                "WHERE order_id <= %s",
                (args.rows,),
            ).rowcount
            conn.commit()
        event = {"action": "update", "table": "shop.orders", "rows": updated}
        events.append(event)
        print(json.dumps(event), flush=True)

    extra = []
    for i in range(args.connections):
        extra.append(connect("shop_app", dbname=args.database,
                             application_name=f"r2_stress_conn_{i}"))
    if extra:
        event = {"action": "connections_open", "count": len(extra),
                 "pids": [c.info.backend_pid for c in extra],
                 "hold_seconds": args.hold}
        events.append(event)
        print(json.dumps(event), flush=True)
        if args.hold > 0:
            time.sleep(args.hold)
        for c in extra:
            c.close()
        closed = {"action": "connections_closed", "count": len(extra)}
        events.append(closed)
        print(json.dumps(closed), flush=True)

    if args.log:
        path = Path(args.log)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
