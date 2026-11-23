-- read_heavy.sql: read-only shop traffic (customer order history, category
-- browsing, and the product_id lookup on shop.order_items that has no index —
-- the intentional missing-index scenario from sql/01_shop.sql).
\set cid random(1, 1000)
\set pid random(1, 200)
SELECT order_id, status, total_amount FROM shop.orders
 WHERE customer_id = :cid ORDER BY ordered_at DESC LIMIT 10;
SELECT product_id, name, unit_price FROM shop.products
 WHERE category = 'books' LIMIT 20;
SELECT count(*) FROM shop.order_items WHERE product_id = :pid;
