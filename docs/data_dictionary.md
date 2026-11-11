# Data dictionary

The dataset is **synthetic**. Its generation is documented in `src/extract.py`, and the number of injected
anomalies is stored in `data/raw/orders_raw.manifest.json`.

## Raw / Silver columns

| Column | Silver type | Required | Validation and treatment |
|--------|-------------|:--------:|--------------------------|
| `order_id` | text (PK) | yes | Non-empty. Unique after deduplication. |
| `customer_id` | text | yes | Non-empty. |
| `order_date` | date | yes | `YYYY-MM-DD` or `DD/MM/YYYY`; between 2020-01-01 and the run date. Other formats are rejected. |
| `category` | text | no | Missing → `Unknown`. Not checked against a list. |
| `product_name` | text | yes | Non-empty; whitespace trimmed. |
| `quantity` | integer | yes | Whole number in [1, 100]; `3.0` accepted as 3 (counted). |
| `unit_price` | decimal(10,2) | yes | Number in [0.01, 10 000.00]; `.` or `,` as decimal separator; thousands separators rejected. |
| `total_amount` | decimal(12,2) | no (derived) | **Always recomputed** as `round(quantity × unit_price, 2)`. A missing or different raw value is counted in the report. |
| `payment_method` | text | no | Trimmed, case-insensitive match against `Credit Card`, `Debit Card`, `PayPal`, `Bank Transfer`, `Apple Pay`. Missing or unrecognised → `Unknown`. |
| `city` | text | no | Missing → `Unknown`. |
| `region` | text | no | Missing → `Unknown` (never inferred from `city`, to avoid hidden assumptions). |

Missing-value tokens (case-insensitive): empty string, `N/A`, `NA`, `NULL`, `NONE`, `NAN`, `-`.

## Bronze metadata columns (`orders_bronze.csv`)

| Column | Meaning |
|--------|---------|
| `_source_file` | Name of the raw file. |
| `_run_id` | Pipeline run identifier, e.g. `20261017T220854Z-bb6f36`. |
| `_ingested_at` | UTC ingestion timestamp (ISO 8601). |
| `_row_number` | 1-based position of the row in the source file (header excluded). |

## Rejected rows (`rejected_rows.csv`, `ecommerce.rejected_orders`)

`run_id`, `source_file`, `source_row`, `reject_reason`, then the 11 raw columns **as received** (not trimmed).
Several reason codes may be joined with `;`. In PostgreSQL, empty raw values are stored as `NULL`.

| Reason code | Rule |
|-------------|------|
| `MISSING_ORDER_ID`, `MISSING_CUSTOMER_ID`, `MISSING_PRODUCT_NAME`, `MISSING_ORDER_DATE`, `MISSING_QUANTITY`, `MISSING_UNIT_PRICE` | Mandatory value missing |
| `INVALID_ORDER_DATE` | Not parseable as `YYYY-MM-DD` / `DD/MM/YYYY` (includes impossible dates such as 2024-02-30) |
| `ORDER_DATE_OUT_OF_RANGE` | Before 2020-01-01 or after the run date |
| `INVALID_QUANTITY` | Not a whole number (`two`, `2.5`, `1x`) |
| `QUANTITY_OUT_OF_RANGE` | < 1 or > 100 |
| `INVALID_UNIT_PRICE` | Not a number (`abc`, `12.5.0`, `€12`, `1,234.50`) |
| `UNIT_PRICE_OUT_OF_RANGE` | < 0.01 or > 10 000.00 |
| `DUPLICATE_EXACT` | Same normalised content as an earlier valid row |
| `DUPLICATE_ORDER_ID_CONFLICT` | Same `order_id`, different content, as an earlier valid row |

Deduplication runs **after** row-level validation, on valid rows only, so an invalid first occurrence never
hides a valid later one. The first valid occurrence in file order is kept.

## Data quality report (`data_quality_report.json`)

| Key | Meaning |
|-----|---------|
| `rows_raw`, `rows_clean`, `rows_rejected`, `rejection_rate_pct` | Row accounting |
| `rows_rejected_as_duplicates` | Rows rejected as duplicates |
| `rejection_reasons` | Occurrences per reason code |
| `normalisations_applied_to_clean_rows` | Whitespace, `DD/MM/YYYY` dates, decimal commas, `3.0` quantities, payment case, missing/inconsistent totals — counted on **kept** rows |
| `values_replaced_by_unknown` | Missing category / payment / city / region, unrecognised payment methods |
| `business_rules` | The rules applied |

## PostgreSQL objects (schema `ecommerce`, version 1.0.0)

| Object | Purpose |
|--------|---------|
| `orders` | Validated orders; PK `order_id`; `CHECK` constraints; indexes on date, category, region, product, payment method, customer; `run_id`, `loaded_at` |
| `rejected_orders` | Audit snapshot per source file; PK `(source_file, source_row)` |
| `pipeline_runs` | One row per successful execution with row counts |
| `schema_version` | Applied schema versions |
| `v_kpi_summary`, `v_monthly_sales`, `v_orders_trend`, `v_sales_by_region`, `v_sales_by_category`, `v_top_products`, `v_payment_methods` | Analytical views (BI layer) |
| `v_rejections_by_reason` | Rejections per reason code |

## Gold files (`data/gold/`, UTF-8 with BOM)

| File | Columns |
|------|---------|
| `kpi_summary.csv` | `total_revenue, orders_count, average_order_value, unique_customers, units_sold, first_order_date, last_order_date` |
| `monthly_sales.csv` | `month, revenue, orders_count, average_order_value, revenue_mom_pct` |
| `orders_trend.csv` | `month, orders_count, orders_mom_pct, orders_3m_moving_avg, cumulative_orders` |
| `sales_by_region.csv` | `region, revenue, orders_count, average_order_value, revenue_share_pct` |
| `sales_by_category.csv` | `category, revenue, orders_count, average_order_value, revenue_share_pct` |
| `top_products.csv` | `rank, product_name, category, units_sold, orders_count, revenue` |
| `payment_methods.csv` | `payment_method, orders_count, orders_share_pct, revenue` |

`month` is the first day of the month (`YYYY-MM-01`). `*_mom_pct` is empty for the first month.
