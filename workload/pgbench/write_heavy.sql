-- write_heavy.sql: short write transactions (stock adjustment + order amount
-- touch-up). Each UPDATE leaves a dead tuple version behind, so sustained
-- runs also feed the dead_tuple_risk rule.
\set pid random(1, 200)
\set oid random(1, 10000)
BEGIN;
UPDATE shop.inventory
   SET quantity_on_hand = quantity_on_hand + 1, updated_at = now()
 WHERE product_id = :pid;
UPDATE shop.orders SET total_amount = total_amount + 0.01 WHERE order_id = :oid;
END;
