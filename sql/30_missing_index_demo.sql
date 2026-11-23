-- sql/30_missing_index_demo.sql -- missing-index before/after demonstration.
--
-- Target: shop.orders has only its primary key and orders_customer_id_idx
-- (sql/01_shop.sql), so a status+date-range aggregate must scan the table.
-- Three identical EXPLAIN (ANALYZE, BUFFERS) runs before the index, then the
-- index is created (and the table ANALYZEd), three identical runs after, then
-- the index is dropped.  Run against shopdb_bench:
--
--   psql -h "$PGHOST" -p "$PGPORT" -U "$PG_SUPERUSER" -d shopdb_bench \
--        -f sql/30_missing_index_demo.sql
--
-- The demonstration is data-only; it changes no seeded rows.

\set ON_ERROR_STOP on
\pset pager off
\timing off

\echo '=== indexes on shop.orders BEFORE ==='
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = 'shop' AND tablename = 'orders' ORDER BY indexname;

\echo ''
\echo '=== query under test (no supporting index) ==='
SELECT count(*), sum(total_amount) AS total_amount
FROM shop.orders
WHERE status = 'paid'
  AND ordered_at >= '2026-02-03' AND ordered_at < '2026-02-04';

\echo ''
\echo '=== BEFORE 1/3 ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_amount) FROM shop.orders
WHERE status = 'paid'
  AND ordered_at >= '2026-02-03' AND ordered_at < '2026-02-04';

\echo ''
\echo '=== BEFORE 2/3 ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_amount) FROM shop.orders
WHERE status = 'paid'
  AND ordered_at >= '2026-02-03' AND ordered_at < '2026-02-04';

\echo ''
\echo '=== BEFORE 3/3 ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_amount) FROM shop.orders
WHERE status = 'paid'
  AND ordered_at >= '2026-02-03' AND ordered_at < '2026-02-04';

\echo ''
\echo '=== CREATE INDEX + ANALYZE ==='
DROP INDEX IF EXISTS shop.orders_status_ordered_at_idx;
CREATE INDEX orders_status_ordered_at_idx
    ON shop.orders (status, ordered_at);
ANALYZE shop.orders;

\echo ''
\echo '=== indexes on shop.orders AFTER CREATE ==='
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = 'shop' AND tablename = 'orders' ORDER BY indexname;

\echo ''
\echo '=== AFTER 1/3 ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_amount) FROM shop.orders
WHERE status = 'paid'
  AND ordered_at >= '2026-02-03' AND ordered_at < '2026-02-04';

\echo ''
\echo '=== AFTER 2/3 ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_amount) FROM shop.orders
WHERE status = 'paid'
  AND ordered_at >= '2026-02-03' AND ordered_at < '2026-02-04';

\echo ''
\echo '=== AFTER 3/3 ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_amount) FROM shop.orders
WHERE status = 'paid'
  AND ordered_at >= '2026-02-03' AND ordered_at < '2026-02-04';

\echo ''
\echo '=== DROP INDEX (leave schema as seeded) ==='
DROP INDEX IF EXISTS shop.orders_status_ordered_at_idx;
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = 'shop' AND tablename = 'orders' ORDER BY indexname;
