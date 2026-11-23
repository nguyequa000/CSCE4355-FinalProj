#!/usr/bin/env python3
"""Proposal-scale synthetic data for the shop schema (M1 - Dustin Dowell).

sql/01_shop.sql builds the six shop tables with a small deterministic seed
(1,000 customers / 10,000 orders) so test databases build in seconds.  This
script REPLACES that data with the proposal's target volume:

    customers 100,000 · products 2,000 · inventory 2,000
    orders 1,000,000 · order_items ~2,000,000 · payments ~750,000

    python workload/seed_data.py --yes                       # shopdb, proposal scale
    python workload/seed_data.py --yes --db shopdb --customers 20000 --orders 200000

Realistic but synthetic: names/cities from Faker (falls back to generated names
when Faker is not installed), skewed distributions (a minority of customers
place most orders, a minority of products sell most units), order totals equal
to the sum of their lines, and one payment per order that left 'new'.

Reproducible: every order is generated from its own Random(seed, order_id), so
the same --seed always produces the same rows, and orders, order_items and
payments are streamed with COPY in three passes without holding millions of
rows in memory.

Runs as the cluster superuser (table owner) from .env.  Refuses to touch a
database whose name ends in _t2/_t3/_t4/_t5 (pytest databases) and requires
--yes because it TRUNCATEs every shop table.  The deliberately missing index on
shop.order_items(product_id) is left missing.  Takes a few minutes at full scale.
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dbconf import connect, load_env  # noqa: E402

try:  # optional: realistic names; plain generated names otherwise
    from faker import Faker
except ImportError:  # pragma: no cover - depends on the local venv
    Faker = None

REGIONS = (["north"] * 3 + ["south"] * 3 + ["east"] * 2 + ["west"] * 2)
CATEGORIES = ["books", "electronics", "home", "toys", "grocery", "garden", "sports", "beauty"]
ORDER_STATUS = (["shipped"] * 55 + ["paid"] * 20 + ["new"] * 15 + ["cancelled"] * 10)
PAY_METHODS = (["card"] * 70 + ["paypal"] * 20 + ["gift_card"] * 5 + ["bank_transfer"] * 5)
EPOCH = datetime(2025, 1, 1, tzinfo=timezone.utc)
SPAN_SECONDS = int(timedelta(days=700).total_seconds())  # orders spread over ~2 years
CENT = Decimal("0.01")


def skewed_id(rng: random.Random, n: int, power: float) -> int:
    """1..n, biased toward low ids (power > 1 = more skew)."""
    return 1 + int(n * rng.random() ** power) if n > 1 else 1


def order_rng(seed: int, order_id: int) -> random.Random:
    return random.Random(seed * 1_000_003 + order_id)


def make_order(seed: int, order_id: int, n_customers: int, prices: list[Decimal]):
    """Everything about one order, as a pure function of (seed, order_id)."""
    rng = order_rng(seed, order_id)
    customer_id = skewed_id(rng, n_customers, 2.2)
    ordered_at = EPOCH + timedelta(seconds=rng.randrange(SPAN_SECONDS))
    status = rng.choice(ORDER_STATUS)
    n_lines = rng.choices((1, 2, 3, 4, 5), weights=(35, 30, 18, 10, 7))[0]
    lines = []
    for line_no in range(1, n_lines + 1):
        product_id = skewed_id(rng, len(prices), 1.8)
        quantity = rng.choices((1, 2, 3, 4), weights=(70, 18, 8, 4))[0]
        lines.append((line_no, product_id, quantity, prices[product_id - 1]))
    total = sum((q * p for _, _, q, p in lines), Decimal("0")).quantize(CENT)
    method = rng.choice(PAY_METHODS)
    return customer_id, ordered_at, status, lines, total, method


def main(argv: list[str] | None = None) -> int:
    env = load_env()
    ap = argparse.ArgumentParser(description="Load proposal-scale synthetic shop data.")
    ap.add_argument("--db", default=env.get("PGDATABASE", "shopdb"))
    ap.add_argument("--customers", type=int, default=100_000)
    ap.add_argument("--orders", type=int, default=1_000_000)
    ap.add_argument("--products", type=int, default=2_000)
    ap.add_argument("--seed", type=int, default=4355)
    ap.add_argument("--yes", action="store_true",
                    help="confirm: TRUNCATE every shop table in --db and reload it")
    args = ap.parse_args(argv)

    if min(args.customers, args.orders, args.products) < 1:
        ap.error("--customers, --orders and --products must be >= 1")
    if re.search(r"_t\d+$", args.db):
        ap.error(f"refusing to load bulk data into test database {args.db!r}")
    if not args.yes:
        print(f"This TRUNCATEs every shop table in {args.db!r} and loads "
              f"{args.customers:,} customers / {args.orders:,} orders. Re-run with --yes.")
        return 2

    rng = random.Random(args.seed)
    fake = None
    if Faker is not None:
        Faker.seed(args.seed)
        fake = Faker("en_US")
    else:
        print("seed_data: Faker not installed; using generated names "
              "(uv pip install faker for realistic ones)", file=sys.stderr)

    started = time.monotonic()

    def step(msg: str) -> None:
        print(f"[{time.monotonic() - started:7.1f}s] {msg}", flush=True)

    with connect("postgres", dbname=args.db, application_name="seed_data") as conn:
        conn.execute("SET synchronous_commit = off")
        step(f"truncating shop tables in {args.db}")
        conn.execute("TRUNCATE shop.payments, shop.order_items, shop.inventory, "
                     "shop.orders, shop.products, shop.customers RESTART IDENTITY")

        # --- customers ---------------------------------------------------
        step(f"customers: {args.customers:,}")
        with conn.cursor().copy(
            "COPY shop.customers (customer_id, email, full_name, region, created_at, balance) "
            "FROM STDIN"
        ) as cp:
            for cid in range(1, args.customers + 1):
                if fake is not None:
                    first, last = fake.first_name(), fake.last_name()
                else:
                    first, last = f"Customer{cid}", "Shopper"
                email = f"{first}.{last}.{cid}@example.test".lower()
                created = EPOCH - timedelta(days=rng.randrange(1, 1500),
                                            seconds=rng.randrange(86_400))
                balance = Decimal(rng.randrange(0, 50_000)) / 100
                cp.write_row((cid, email, f"{first} {last}", rng.choice(REGIONS),
                              created, balance))

        # --- products + inventory ------------------------------------------
        step(f"products + inventory: {args.products:,}")
        prices: list[Decimal] = []
        with conn.cursor().copy(
            "COPY shop.products (product_id, sku, name, category, unit_price, created_at) "
            "FROM STDIN"
        ) as cp:
            for pid in range(1, args.products + 1):
                # log-uniform-ish prices: many cheap items, a few expensive ones
                price = Decimal(str(round(2 + 498 * rng.random() ** 3, 2))).quantize(CENT)
                prices.append(price)
                if fake is not None:
                    name = f"{fake.color_name()} {fake.word().title()} {rng.choice(CATEGORIES[:5]).rstrip('s').title()}"
                else:
                    name = f"Product {pid}"
                cp.write_row((pid, f"SKU-{pid:06d}", name, rng.choice(CATEGORIES), price,
                              EPOCH - timedelta(days=rng.randrange(1, 900))))
        with conn.cursor().copy(
            "COPY shop.inventory (product_id, quantity_on_hand, reorder_level, updated_at) "
            "FROM STDIN"
        ) as cp:
            now = datetime.now(timezone.utc)
            for pid in range(1, args.products + 1):
                cp.write_row((pid, rng.randrange(0, 2_000), rng.randrange(10, 100), now))

        # --- orders, order_items, payments: three streamed passes -----------
        # (separate COPYs because of the foreign keys; each order is regenerated
        # from its own RNG, so the passes agree without buffering anything)
        step(f"orders: {args.orders:,}")
        with conn.cursor().copy(
            "COPY shop.orders (order_id, customer_id, ordered_at, status, total_amount) "
            "FROM STDIN"
        ) as cp:
            for oid in range(1, args.orders + 1):
                cust, at, status, _lines, total, _m = make_order(args.seed, oid, args.customers, prices)
                cp.write_row((oid, cust, at, status, total))

        step("order_items")
        n_items = 0
        with conn.cursor().copy(
            "COPY shop.order_items (order_id, line_no, product_id, quantity, unit_price) "
            "FROM STDIN"
        ) as cp:
            for oid in range(1, args.orders + 1):
                _c, _a, _s, lines, _t, _m = make_order(args.seed, oid, args.customers, prices)
                for line_no, pid, qty, price in lines:
                    cp.write_row((oid, line_no, pid, qty, price))
                    n_items += 1

        step("payments")
        n_pay = 0
        with conn.cursor().copy(
            "COPY shop.payments (payment_id, order_id, paid_at, amount, method, status) "
            "FROM STDIN"
        ) as cp:
            for oid in range(1, args.orders + 1):
                _c, at, status, _l, total, method = make_order(args.seed, oid, args.customers, prices)
                if status == "new":
                    continue
                n_pay += 1
                pay_status = "refunded" if status == "cancelled" else "captured"
                cp.write_row((n_pay, oid, at + timedelta(minutes=5), total, method, pay_status))

        # identity columns were loaded with explicit values
        conn.execute("SELECT setval(pg_get_serial_sequence('shop.products', 'product_id'), %s)",
                     (args.products,))
        conn.execute("SELECT setval(pg_get_serial_sequence('shop.payments', 'payment_id'), %s)",
                     (max(n_pay, 1),))
        conn.commit()

        step("ANALYZE")
        conn.autocommit = True
        for table in ("customers", "products", "inventory", "orders", "order_items", "payments"):
            conn.execute(f"ANALYZE shop.{table}")

        counts = conn.execute(
            "SELECT (SELECT count(*) FROM shop.customers), (SELECT count(*) FROM shop.products), "
            "(SELECT count(*) FROM shop.orders), (SELECT count(*) FROM shop.order_items), "
            "(SELECT count(*) FROM shop.payments), "
            "pg_size_pretty(sum(pg_total_relation_size(c.oid))) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'shop' AND c.relkind = 'r'"
        ).fetchone()

    step(f"done: customers={counts[0]:,} products={counts[1]:,} orders={counts[2]:,} "
         f"order_items={counts[3]:,} payments={counts[4]:,} shop_size={counts[5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
