"""Gold layer: KPI calculations on a small hand-computed dataset."""

from __future__ import annotations

import pandas as pd
import pytest

from src.metrics import GOLD_TABLES, compute_gold_tables, top_products


@pytest.fixture
def orders() -> pd.DataFrame:
    """Four orders over two months with known totals (revenue = 400.00)."""
    rows = [
        ("O1", "C1", "2025-01-10", "Books", "Book A", 2, 10.00, 20.00, "PayPal", "Paris", "IDF"),
        ("O2", "C1", "2025-01-20", "Books", "Book A", 1, 10.00, 10.00, "PayPal", "Paris", "IDF"),
        ("O3", "C2", "2025-02-05", "Sports", "Mat", 1, 70.00, 70.00, "Credit Card", "Lyon", "ARA"),
        ("O4", "C3", "2025-02-15", "Sports", "Bike", 1, 300.00, 300.00, "PayPal", "Lyon", "ARA"),
    ]
    frame = pd.DataFrame(
        rows,
        columns=[
            "order_id",
            "customer_id",
            "order_date",
            "category",
            "product_name",
            "quantity",
            "unit_price",
            "total_amount",
            "payment_method",
            "city",
            "region",
        ],
    )
    frame["order_date"] = pd.to_datetime(frame["order_date"])
    return frame


def test_kpi_summary(orders: pd.DataFrame) -> None:
    kpi = compute_gold_tables(orders)["kpi_summary"].iloc[0]
    assert kpi["total_revenue"] == 400.00
    assert kpi["orders_count"] == 4
    assert kpi["average_order_value"] == 100.00  # 400 / 4
    assert kpi["unique_customers"] == 3
    assert kpi["units_sold"] == 5
    assert str(kpi["first_order_date"]) == "2025-01-10"
    assert str(kpi["last_order_date"]) == "2025-02-15"


def test_monthly_sales_and_growth(orders: pd.DataFrame) -> None:
    monthly = compute_gold_tables(orders)["monthly_sales"]
    assert [str(m) for m in monthly["month"]] == ["2025-01-01", "2025-02-01"]
    assert monthly["revenue"].tolist() == [30.00, 370.00]
    assert monthly["orders_count"].tolist() == [2, 2]
    assert monthly["average_order_value"].tolist() == [15.00, 185.00]
    assert pd.isna(monthly.at[0, "revenue_mom_pct"])
    assert monthly.at[1, "revenue_mom_pct"] == pytest.approx(1133.33)  # (370-30)/30


def test_orders_trend(orders: pd.DataFrame) -> None:
    trend = compute_gold_tables(orders)["orders_trend"]
    assert trend["orders_count"].tolist() == [2, 2]
    assert trend["orders_mom_pct"].tolist()[1] == 0.0
    assert trend["orders_3m_moving_avg"].tolist() == [2.0, 2.0]
    assert trend["cumulative_orders"].tolist() == [2, 4]


def test_sales_by_region_and_share(orders: pd.DataFrame) -> None:
    region = compute_gold_tables(orders)["sales_by_region"]
    assert region["region"].tolist() == ["ARA", "IDF"]  # sorted by revenue desc
    assert region["revenue"].tolist() == [370.00, 30.00]
    assert region["revenue_share_pct"].tolist() == [92.5, 7.5]
    assert region["revenue_share_pct"].sum() == pytest.approx(100.0)


def test_sales_by_category(orders: pd.DataFrame) -> None:
    category = compute_gold_tables(orders)["sales_by_category"]
    assert category["category"].tolist() == ["Sports", "Books"]
    assert category["orders_count"].tolist() == [2, 2]


def test_top_products_rank_and_alphabetical_tie_break(orders: pd.DataFrame) -> None:
    ranked = top_products(
        orders.assign(
            total_amount=lambda d: d["total_amount"].where(d["product_name"] != "Mat", 30.0)
        )
    )
    assert ranked["product_name"].tolist() == [
        "Bike",
        "Book A",
        "Mat",
    ]  # Book A and Mat tie at 30.00
    assert ranked["rank"].tolist() == [1, 2, 3]


def test_top_products_is_limited_to_ten() -> None:
    many = pd.DataFrame(
        {
            "order_id": [f"O{i}" for i in range(15)],
            "product_name": [f"P{i:02d}" for i in range(15)],
            "category": "X",
            "quantity": 1,
            "total_amount": [float(i + 1) for i in range(15)],
        }
    )
    ranked = top_products(many)
    assert len(ranked) == 10 and ranked["product_name"].iloc[0] == "P14"


def test_payment_methods_distribution(orders: pd.DataFrame) -> None:
    payments = compute_gold_tables(orders)["payment_methods"]
    assert payments["payment_method"].tolist() == ["PayPal", "Credit Card"]
    assert payments["orders_count"].tolist() == [3, 1]
    assert payments["orders_share_pct"].tolist() == [75.0, 25.0]
    assert payments["revenue"].tolist() == [330.00, 70.00]


def test_all_gold_tables_are_produced(orders: pd.DataFrame) -> None:
    assert tuple(compute_gold_tables(orders)) == GOLD_TABLES


def test_empty_input_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        compute_gold_tables(pd.DataFrame(columns=["order_id"]))
