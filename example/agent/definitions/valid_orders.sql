-- Completed orders (status COMPLETED). Cancelled and refunded orders are excluded.
-- The period is filtered in the query, not here.
SELECT * FROM shop.orders WHERE status = 'COMPLETED'