-- Products with standardized category (Coffee, Accessories, Equipment, Maintenance).
SELECT product_id, name,
       upper(left(trim(category), 1)) || lower(substr(trim(category), 2)) AS category,
       price, active = 1 AS active
FROM inventory.products