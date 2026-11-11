"""Gold layer: analytical indicators computed from the validated Silver table.

KPI definitions (all based on validated orders only):

* **Total revenue** = sum of ``total_amount``.
* **Orders count** = number of distinct ``order_id``.
* **Average order value (AOV)** = total revenue / orders count.
* **Monthly revenue** = revenue grouped by calendar month of ``order_date``.
* **Share** = revenue (or orders) of the group / overall total x 100.
* **MoM %** = (month - previous month) / previous month x 100.

The same definitions are implemented as SQL views in ``sql/analytics.sql``;
``src/load.py`` reconciles both implementations after each database load.
"""

from __future__ import annotations

import pandas as pd

TOP_PRODUCTS_LIMIT = 10

GOLD_TABLES: tuple[str, ...] = (
    "kpi_summary",
    "monthly_sales",
    "orders_trend",
    "sales_by_region",
    "sales_by_category",
    "top_products",
    "payment_methods",
)


def _with_month(orders: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with ``month`` = first day of the order month."""
    result = orders.copy()
    result["month"] = result["order_date"].dt.to_period("M").dt.to_timestamp().dt.date
    return result


def _pct(part: pd.Series, total: float) -> pd.Series:
    """Percentage of ``total`` rounded to two decimals (0 if total is 0)."""
    if total == 0:
        return part * 0.0
    return (part / total * 100).round(2)


def kpi_summary(orders: pd.DataFrame) -> pd.DataFrame:
    """Compute the headline KPIs as a single-row table."""
    revenue = round(float(orders["total_amount"].sum()), 2)
    count = int(orders["order_id"].nunique())
    return pd.DataFrame(
        [
            {
                "total_revenue": revenue,
                "orders_count": count,
                "average_order_value": round(revenue / count, 2) if count else 0.0,
                "unique_customers": int(orders["customer_id"].nunique()),
                "units_sold": int(orders["quantity"].sum()),
                "first_order_date": orders["order_date"].min().date() if count else None,
                "last_order_date": orders["order_date"].max().date() if count else None,
            }
        ]
    )


def monthly_sales(orders: pd.DataFrame) -> pd.DataFrame:
    """Revenue, orders, AOV and revenue month-over-month growth per month."""
    grouped = (
        _with_month(orders)
        .groupby("month", as_index=False)
        .agg(revenue=("total_amount", "sum"), orders_count=("order_id", "nunique"))
        .sort_values("month")
        .reset_index(drop=True)
    )
    grouped["revenue"] = grouped["revenue"].round(2)
    grouped["average_order_value"] = (grouped["revenue"] / grouped["orders_count"]).round(2)
    grouped["revenue_mom_pct"] = (grouped["revenue"].pct_change() * 100).round(2)
    return grouped


def orders_trend(orders: pd.DataFrame) -> pd.DataFrame:
    """Orders per month with MoM change, 3-month moving average and cumulative total."""
    grouped = (
        _with_month(orders)
        .groupby("month", as_index=False)
        .agg(orders_count=("order_id", "nunique"))
        .sort_values("month")
        .reset_index(drop=True)
    )
    grouped["orders_mom_pct"] = (grouped["orders_count"].pct_change() * 100).round(2)
    grouped["orders_3m_moving_avg"] = (
        grouped["orders_count"].rolling(window=3, min_periods=1).mean().round(2)
    )
    grouped["cumulative_orders"] = grouped["orders_count"].cumsum()
    return grouped


def _sales_by(orders: pd.DataFrame, column: str) -> pd.DataFrame:
    """Revenue, orders, AOV and revenue share for one dimension."""
    grouped = orders.groupby(column, as_index=False).agg(
        revenue=("total_amount", "sum"), orders_count=("order_id", "nunique")
    )
    grouped["revenue"] = grouped["revenue"].round(2)
    grouped["average_order_value"] = (grouped["revenue"] / grouped["orders_count"]).round(2)
    grouped["revenue_share_pct"] = _pct(grouped["revenue"], float(grouped["revenue"].sum()))
    return grouped.sort_values(["revenue", column], ascending=[False, True]).reset_index(drop=True)


def sales_by_region(orders: pd.DataFrame) -> pd.DataFrame:
    """Sales indicators per region, sorted by revenue."""
    return _sales_by(orders, "region")


def sales_by_category(orders: pd.DataFrame) -> pd.DataFrame:
    """Sales indicators per product category, sorted by revenue."""
    return _sales_by(orders, "category")


def top_products(orders: pd.DataFrame, limit: int = TOP_PRODUCTS_LIMIT) -> pd.DataFrame:
    """Top products by revenue (ties broken alphabetically)."""
    grouped = orders.groupby(["product_name", "category"], as_index=False).agg(
        units_sold=("quantity", "sum"),
        orders_count=("order_id", "nunique"),
        revenue=("total_amount", "sum"),
    )
    grouped["revenue"] = grouped["revenue"].round(2)
    grouped = grouped.sort_values(["revenue", "product_name"], ascending=[False, True])
    grouped = grouped.head(limit).reset_index(drop=True)
    grouped.insert(0, "rank", range(1, len(grouped) + 1))
    return grouped[["rank", "product_name", "category", "units_sold", "orders_count", "revenue"]]


def payment_methods(orders: pd.DataFrame) -> pd.DataFrame:
    """Orders, revenue and order share per payment method."""
    grouped = orders.groupby("payment_method", as_index=False).agg(
        orders_count=("order_id", "nunique"), revenue=("total_amount", "sum")
    )
    grouped["revenue"] = grouped["revenue"].round(2)
    grouped["orders_share_pct"] = _pct(
        grouped["orders_count"], float(grouped["orders_count"].sum())
    )
    grouped = grouped.sort_values(["orders_count", "payment_method"], ascending=[False, True])
    grouped = grouped.reset_index(drop=True)
    return grouped[["payment_method", "orders_count", "orders_share_pct", "revenue"]]


def compute_gold_tables(orders: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Compute every Gold table from the Silver orders.

    Args:
        orders: Silver table (typed, validated).

    Returns:
        Mapping ``table name -> DataFrame``, keys listed in ``GOLD_TABLES``.

    Raises:
        ValueError: If ``orders`` is empty (no indicator can be computed).
    """
    if orders.empty:
        raise ValueError("Cannot compute Gold tables from an empty Silver table")
    return {
        "kpi_summary": kpi_summary(orders),
        "monthly_sales": monthly_sales(orders),
        "orders_trend": orders_trend(orders),
        "sales_by_region": sales_by_region(orders),
        "sales_by_category": sales_by_category(orders),
        "top_products": top_products(orders),
        "payment_methods": payment_methods(orders),
    }
