-- Order lines on valid orders, with customer, order date and amount (quantity × unit_price).
-- Negative quantities are credit notes and are included.
SELECT l.line_id, l.order_id, o.customer_id, o.order_date, l.product_id,
       l.quantity, l.unit_price, l.quantity * l.unit_price AS amount
FROM shop.order_lines l
JOIN def.valid_orders o ON o.order_id = l.order_id