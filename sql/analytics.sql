-- =============================================================================
-- Analytical views (Gold layer served by PostgreSQL).
-- All indicators are computed from ecommerce.orders, which contains validated
-- orders only (rejected rows live in ecommerce.rejected_orders).
-- The same definitions are implemented in src/metrics.py and reconciled by
-- src/load.py after each load.
-- =============================================================================

-- Total revenue, number of orders, average order value (AOV).
-- AOV = total revenue / number of orders.
CREATE OR REPLACE VIEW ecommerce.v_kpi_summary AS
SELECT
    COALESCE(SUM(total_amount), 0)::NUMERIC(14,2)  AS total_revenue,
    COUNT(*)                                       AS orders_count,
    COALESCE(ROUND(AVG(total_amount), 2), 0)       AS average_order_value,
    COUNT(DISTINCT customer_id)                    AS unique_customers,
    COALESCE(SUM(quantity), 0)                     AS units_sold,
    MIN(order_date)                                AS first_order_date,
    MAX(order_date)                                AS last_order_date
FROM ecommerce.orders;

-- Monthly revenue, orders, AOV and revenue growth vs previous month.
CREATE OR REPLACE VIEW ecommerce.v_monthly_sales AS
WITH monthly AS (
    SELECT
        date_trunc('month', order_date)::DATE AS month,
        SUM(total_amount)                     AS revenue,
        COUNT(*)                              AS orders_count
    FROM ecommerce.orders
    GROUP BY 1
)
SELECT
    month,
    revenue,
    orders_count,
    ROUND(revenue / orders_count, 2) AS average_order_value,
    ROUND(
        (revenue - LAG(revenue) OVER w) / NULLIF(LAG(revenue) OVER w, 0) * 100, 2
    ) AS revenue_mom_pct
FROM monthly
WINDOW w AS (ORDER BY month)
ORDER BY month;

-- Evolution of the number of orders.
CREATE OR REPLACE VIEW ecommerce.v_orders_trend AS
WITH monthly AS (
    SELECT date_trunc('month', order_date)::DATE AS month, COUNT(*) AS orders_count
    FROM ecommerce.orders
    GROUP BY 1
)
SELECT
    month,
    orders_count,
    ROUND(
        (orders_count - LAG(orders_count) OVER w)::NUMERIC
        / NULLIF(LAG(orders_count) OVER w, 0) * 100, 2
    ) AS orders_mom_pct,
    ROUND(AVG(orders_count) OVER (ORDER BY month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 2)
        AS orders_3m_moving_avg,
    SUM(orders_count) OVER (ORDER BY month) AS cumulative_orders
FROM monthly
WINDOW w AS (ORDER BY month)
ORDER BY month;

-- Sales by region (revenue share = region revenue / total revenue x 100).
CREATE OR REPLACE VIEW ecommerce.v_sales_by_region AS
SELECT
    region,
    SUM(total_amount)                                              AS revenue,
    COUNT(*)                                                       AS orders_count,
    ROUND(SUM(total_amount) / COUNT(*), 2)                         AS average_order_value,
    ROUND(SUM(total_amount) / SUM(SUM(total_amount)) OVER () * 100, 2) AS revenue_share_pct
FROM ecommerce.orders
GROUP BY region
ORDER BY revenue DESC, region;

-- Sales by product category.
CREATE OR REPLACE VIEW ecommerce.v_sales_by_category AS
SELECT
    category,
    SUM(total_amount)                                              AS revenue,
    COUNT(*)                                                       AS orders_count,
    ROUND(SUM(total_amount) / COUNT(*), 2)                         AS average_order_value,
    ROUND(SUM(total_amount) / SUM(SUM(total_amount)) OVER () * 100, 2) AS revenue_share_pct
FROM ecommerce.orders
GROUP BY category
ORDER BY revenue DESC, category;

-- Top 10 products by revenue (ties broken alphabetically).
CREATE OR REPLACE VIEW ecommerce.v_top_products AS
SELECT rank, product_name, category, units_sold, orders_count, revenue
FROM (
    SELECT
        ROW_NUMBER() OVER (ORDER BY SUM(total_amount) DESC, product_name) AS rank,
        product_name,
        category,
        SUM(quantity)     AS units_sold,
        COUNT(*)          AS orders_count,
        SUM(total_amount) AS revenue
    FROM ecommerce.orders
    GROUP BY product_name, category
) ranked
WHERE rank <= 10
ORDER BY rank;

-- Payment method distribution (share of orders).
CREATE OR REPLACE VIEW ecommerce.v_payment_methods AS
SELECT
    payment_method,
    COUNT(*)                                                 AS orders_count,
    ROUND(COUNT(*)::NUMERIC / SUM(COUNT(*)) OVER () * 100, 2) AS orders_share_pct,
    SUM(total_amount)                                        AS revenue
FROM ecommerce.orders
GROUP BY payment_method
ORDER BY orders_count DESC, payment_method;

-- Data quality: why rows were rejected (a row may carry several reason codes,
-- separated by ';'; each code is counted once per row).
CREATE OR REPLACE VIEW ecommerce.v_rejections_by_reason AS
SELECT code AS reject_reason, COUNT(*) AS rows_count
FROM ecommerce.rejected_orders,
     LATERAL unnest(string_to_array(reject_reason, ';')) AS code
GROUP BY code
ORDER BY rows_count DESC, code;
