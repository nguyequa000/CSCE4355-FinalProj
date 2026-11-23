-- 01_shop.sql  (run as superuser, connected to the project database)
-- Small deterministic shop seed. Reduced from the proposal's ~100k customers /
-- ~1M orders: 1,000 customers and 10,000 orders (disclosed in the report).
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS shop;

DROP TABLE IF EXISTS shop.orders;
DROP TABLE IF EXISTS shop.customers;

CREATE TABLE shop.customers (
    customer_id  integer PRIMARY KEY,
    email        text    NOT NULL UNIQUE,
    full_name    text    NOT NULL,
    region       text    NOT NULL,
    created_at   timestamptz NOT NULL,
    balance      numeric(12,2) NOT NULL DEFAULT 0
);

CREATE TABLE shop.orders (
    order_id     integer PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES shop.customers(customer_id),
    ordered_at   timestamptz NOT NULL,
    status       text    NOT NULL CHECK (status IN ('new','paid','shipped','cancelled')),
    total_amount numeric(12,2) NOT NULL CHECK (total_amount >= 0)
);
CREATE INDEX orders_customer_id_idx ON shop.orders(customer_id);

-- deterministic: no random(); every value is a pure function of the row number
INSERT INTO shop.customers (customer_id, email, full_name, region, created_at, balance)
SELECT g,
       'customer' || g || '@example.test',
       'Customer ' || g,
       (ARRAY['north','south','east','west'])[1 + g % 4],
       timestamptz '2026-01-01 00:00:00+00' + (g || ' hours')::interval,
       (g % 500)::numeric
FROM generate_series(1, 1000) AS g;

INSERT INTO shop.orders (order_id, customer_id, ordered_at, status, total_amount)
SELECT g,
       1 + (g * 7) % 1000,
       timestamptz '2026-02-01 00:00:00+00' + (g || ' minutes')::interval,
       (ARRAY['new','paid','shipped','cancelled'])[1 + g % 4],
       round(((g * 37) % 50000) / 100.0, 2)
FROM generate_series(1, 10000) AS g;

ANALYZE shop.customers;
ANALYZE shop.orders;

-- least privilege: the application role can use shop tables only
GRANT USAGE ON SCHEMA shop TO shop_app;
GRANT SELECT, INSERT, UPDATE ON shop.customers, shop.orders TO shop_app;
