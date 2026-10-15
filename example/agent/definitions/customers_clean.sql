-- Customers with standardized region names (whitespace and upper/lower case fixed).
SELECT customer_id, name, email, created_date,
       CASE lower(trim(region))
         WHEN 'london' THEN 'London'
         WHEN 'south east' THEN 'South East'
         WHEN 'south west' THEN 'South West'
         WHEN 'midlands' THEN 'Midlands'
         WHEN 'north west' THEN 'North West'
         WHEN 'north east' THEN 'North East'
         WHEN 'scotland' THEN 'Scotland'
         WHEN 'wales' THEN 'Wales'
         WHEN 'international' THEN 'International'
         ELSE trim(region)
       END AS region
FROM crm.customers