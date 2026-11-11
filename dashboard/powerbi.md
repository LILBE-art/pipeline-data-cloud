# Power BI dashboard guide

> **Honest status:** no `.pbix` file and no screenshot is provided. Power BI Desktop does not run natively on
> Linux and no tool here can generate a working `.pbix`. This guide explains how to build the report from the
> data the pipeline produces. Add your own screenshots to `dashboard/screenshots/` once it exists.

## Realistic options from Debian/Linux

| Option | How | Trade-offs |
|--------|-----|-----------|
| **Windows virtual machine** (recommended for authoring) | VirtualBox / KVM + Windows (evaluation ISO), install Power BI Desktop | Needs RAM/disk; Windows licensing |
| **Power BI Service in the browser** | Sign in at app.powerbi.com with a work/school account, *Create → Upload a file* with the Gold CSVs | Report authoring in the browser is more limited than Desktop; a personal Microsoft account is generally not accepted, availability depends on your tenant licence |
| **Complementary local web dashboard** | Streamlit/Plotly reading `data/gold/*.csv` | Works on Linux, but it is **not** Power BI and must not be presented as such. Not implemented in this repo (see roadmap) |

Whatever the option, the data contract is identical: the 7 Gold CSV files, or the PostgreSQL views.

## Data sources

### A. Gold CSV files (simplest)

Files are in `data/gold/` (UTF-8 **with BOM**). In Power BI Desktop: *Get data → Text/CSV*, pick each file and
check *File origin = 65001: Unicode (UTF-8)*. Copy the files to the Windows VM (shared folder or `scp`).

### B. PostgreSQL views (live connection)

1. Make PostgreSQL reachable from the Windows machine. Docker publishes it on `127.0.0.1` only; for a VM use an
   SSH tunnel instead of opening the port to the network:
   ```bash
   ssh -N -L 5432:127.0.0.1:5432 user@debian-host      # run on the Windows side
   ```
2. *Get data → PostgreSQL database* → Server `localhost:5432`, Database `ecommerce`, **Database** credentials
   (user `pipeline` and the password from your `.env`), Import mode.
3. Select the views of schema `ecommerce`: `v_kpi_summary`, `v_monthly_sales`, `v_orders_trend`,
   `v_sales_by_region`, `v_sales_by_category`, `v_top_products`, `v_payment_methods`
   (optionally `v_rejections_by_reason` for a data-quality page).

Power BI's PostgreSQL connector may require the Npgsql provider (installed with Desktop on recent versions;
otherwise follow the prompt). Never commit the password or the `.pbix` (it embeds the imported data).

## Model

The tables are pre-aggregated and independent, so **no relationships are required**. Set data types:
`month` / dates → Date, amounts → Fixed decimal number, percentages → Decimal number. Sort the month column
chronologically and `payment_method` by `orders_count`.

## Measures (DAX)

Replace the table names if you imported from PostgreSQL (`v_kpi_summary`, …). Names below match the CSV files.

```dax
Total Revenue = SUM ( kpi_summary[total_revenue] )
Orders        = SUM ( kpi_summary[orders_count] )
Average Basket = DIVIDE ( [Total Revenue], [Orders] )
Unique Customers = SUM ( kpi_summary[unique_customers] )

Monthly Revenue = SUM ( monthly_sales[revenue] )
Monthly Orders  = SUM ( orders_trend[orders_count] )
Revenue MoM %   = AVERAGE ( monthly_sales[revenue_mom_pct] ) / 100
Orders 3M Avg   = AVERAGE ( orders_trend[orders_3m_moving_avg] )
```

Format `Revenue MoM %` as percentage. Because the Gold tables are already aggregated, use these measures in
visuals with the month (or region/category/product) field on the axis.

## Report layout (suggested)

| Visual | Fields |
|--------|--------|
| 4 KPI cards | `Total Revenue`, `Orders`, `Average Basket`, `Unique Customers` |
| Line chart – monthly revenue | X: `monthly_sales[month]`, Y: `Monthly Revenue` |
| Column + line – orders evolution | X: `orders_trend[month]`, columns: `Monthly Orders`, line: `Orders 3M Avg` |
| Bar chart – sales by region | `sales_by_region[region]`, `revenue` |
| Donut / bar – sales by category | `sales_by_category[category]`, `revenue` |
| Table / bar – top 10 products | `top_products[rank]`, `product_name`, `revenue` (sorted by rank) |
| Donut – payment methods | `payment_methods[payment_method]`, `orders_count` |

Good practice: a title stating the period (2024-01 → 2025-06), a footnote “synthetic data”, consistent
number formats, and `Unknown` kept visible (it is the share of orders with a missing dimension).

## KPI definitions

Identical to the README: revenue = sum of `total_amount` of validated orders; orders = distinct `order_id`;
basket = revenue ÷ orders; MoM % = (month − previous month) ÷ previous month. Reference values for the
seed-42 dataset: **618,065.97** revenue, **4,884** orders, **126.55** average basket.
Use them to check your report (`Total Revenue` card must show 618,065.97).

## Refreshing

CSV source: rerun `make run`, then *Refresh*. PostgreSQL source: rerun `make run-db` (or `make docker-run`),
then *Refresh*. Scheduled refresh in the Service needs a data gateway for a local database: out of scope.

## Publishing note

Do not publish the report publicly with real data. The synthetic data here carry no confidentiality
constraint, but keep the habit of checking sharing settings.
