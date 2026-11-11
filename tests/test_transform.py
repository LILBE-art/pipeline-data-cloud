"""Silver layer: cleaning, deduplication, rejection rules and reporting."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.extract import build_bronze, generate_raw_dataset
from src.transform import clean_orders
from src.validation import REQUIRED_COLUMNS, SchemaError
from tests.conftest import make_raw

RUN_DATE = date(2026, 6, 1)


def clean(*rows: dict[str, str]):
    return clean_orders(make_raw(*rows), run_date=RUN_DATE, run_id="r", source_file="f.csv")


def reasons(result) -> list[str]:
    return result.rejected["reject_reason"].tolist()


# --- types and amounts --------------------------------------------------------
def test_valid_row_is_kept_with_expected_types() -> None:
    result = clean({})
    assert len(result.clean) == 1 and result.rejected.empty
    row = result.clean.iloc[0]
    assert pd.api.types.is_integer_dtype(result.clean["quantity"])
    assert pd.api.types.is_datetime64_any_dtype(result.clean["order_date"])
    assert row["order_date"] == pd.Timestamp("2025-01-15")
    assert row["total_amount"] == 21.00


def test_type_conversions_for_alternative_notations() -> None:
    result = clean({"order_date": "05/03/2025", "unit_price": "10,50", "quantity": "3.0"})
    row = result.clean.iloc[0]
    assert row["order_date"] == pd.Timestamp("2025-03-05")
    assert row["unit_price"] == 10.50 and row["quantity"] == 3
    notes = result.report["normalisations_applied_to_clean_rows"]
    assert notes["dates_converted_from_dd_mm_yyyy"] == 1
    assert notes["unit_prices_with_decimal_comma"] == 1
    assert notes["quantities_written_with_decimals"] == 1


def test_total_amount_is_recomputed_not_trusted() -> None:
    result = clean({"total_amount": "999.99"}, {"order_id": "ORD-2", "total_amount": ""})
    assert result.clean["total_amount"].tolist() == [21.00, 21.00]
    notes = result.report["normalisations_applied_to_clean_rows"]
    assert notes["total_amount_inconsistent_then_recomputed"] == 1
    assert notes["total_amount_missing_then_computed"] == 1


def test_total_amount_uses_exact_cents() -> None:
    result = clean({"quantity": "3", "unit_price": "19.99", "total_amount": "59.97"})
    assert result.clean.at[0, "total_amount"] == 59.97
    assert (
        result.report["normalisations_applied_to_clean_rows"][
            "total_amount_inconsistent_then_recomputed"
        ]
        == 0
    )


# --- missing values -----------------------------------------------------------
@pytest.mark.parametrize("token", ["", "  ", "N/A", "NULL"])
@pytest.mark.parametrize(
    ("column", "code"),
    [
        ("order_id", "MISSING_ORDER_ID"),
        ("customer_id", "MISSING_CUSTOMER_ID"),
        ("order_date", "MISSING_ORDER_DATE"),
        ("product_name", "MISSING_PRODUCT_NAME"),
        ("quantity", "MISSING_QUANTITY"),
        ("unit_price", "MISSING_UNIT_PRICE"),
    ],
)
def test_missing_mandatory_value_rejects_the_row(column: str, code: str, token: str) -> None:
    result = clean({column: token})
    assert result.clean.empty
    assert code in reasons(result)[0].split(";")


def test_missing_descriptive_values_become_unknown_and_are_counted() -> None:
    result = clean({"category": "", "city": "N/A", "region": "NULL", "payment_method": " "})
    row = result.clean.iloc[0]
    assert (row["category"], row["city"], row["region"], row["payment_method"]) == ("Unknown",) * 4
    counts = result.report["values_replaced_by_unknown"]
    assert counts["category"] == counts["city"] == counts["region"] == counts["payment_method"] == 1


def test_payment_method_is_standardised_or_marked_unknown() -> None:
    result = clean(
        {"order_id": "1", "payment_method": " paypal "},
        {"order_id": "2", "payment_method": "CREDIT CARD"},
        {"order_id": "3", "payment_method": "Bitcoin"},
    )
    assert result.clean["payment_method"].tolist() == ["PayPal", "Credit Card", "Unknown"]
    assert (
        result.report["normalisations_applied_to_clean_rows"]["payment_method_case_standardised"]
        == 2
    )
    assert result.report["values_replaced_by_unknown"]["payment_method_unrecognised"] == 1


def test_whitespace_is_trimmed_and_counted() -> None:
    result = clean({"product_name": "  SQL Cookbook\t", "city": "Paris "})
    assert result.clean.at[0, "product_name"] == "SQL Cookbook"
    assert (
        result.report["normalisations_applied_to_clean_rows"]["rows_with_whitespace_trimmed"] == 1
    )


# --- rejection rules ----------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("03/15/2025", "INVALID_ORDER_DATE"),  # MM/DD/YYYY is never guessed
        ("2025-02-30", "INVALID_ORDER_DATE"),
        ("31-02-2025", "INVALID_ORDER_DATE"),
        ("yesterday", "INVALID_ORDER_DATE"),
        ("2019-12-31", "ORDER_DATE_OUT_OF_RANGE"),
        ("2026-06-02", "ORDER_DATE_OUT_OF_RANGE"),  # after the run date
    ],
)
def test_invalid_dates_are_rejected(value: str, code: str) -> None:
    result = clean({"order_date": value})
    assert result.clean.empty and reasons(result) == [code]


def test_run_date_itself_is_accepted() -> None:
    assert len(clean({"order_date": "2026-06-01"}).clean) == 1


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("0", "QUANTITY_OUT_OF_RANGE"),
        ("-1", "QUANTITY_OUT_OF_RANGE"),
        ("101", "QUANTITY_OUT_OF_RANGE"),
        ("2.5", "INVALID_QUANTITY"),
        ("two", "INVALID_QUANTITY"),
    ],
)
def test_invalid_quantities_are_rejected(value: str, code: str) -> None:
    assert reasons(clean({"quantity": value})) == [code]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("0", "UNIT_PRICE_OUT_OF_RANGE"),
        ("-3", "UNIT_PRICE_OUT_OF_RANGE"),
        ("10000.01", "UNIT_PRICE_OUT_OF_RANGE"),
        ("abc", "INVALID_UNIT_PRICE"),
        ("1,234.50", "INVALID_UNIT_PRICE"),  # ambiguous thousands separator
    ],
)
def test_invalid_prices_are_rejected(value: str, code: str) -> None:
    assert reasons(clean({"unit_price": value})) == [code]


@pytest.mark.parametrize(("quantity", "price"), [("1", "0.01"), ("100", "10000.00")])
def test_boundary_values_are_accepted(quantity: str, price: str) -> None:
    assert len(clean({"quantity": quantity, "unit_price": price}).clean) == 1


def test_multiple_problems_are_all_reported() -> None:
    assert reasons(clean({"customer_id": "", "quantity": "0"})) == [
        "MISSING_CUSTOMER_ID;QUANTITY_OUT_OF_RANGE"
    ]


# --- duplicates ---------------------------------------------------------------
def test_exact_duplicates_keep_first_and_reject_the_rest() -> None:
    result = clean({}, {}, {})
    assert len(result.clean) == 1
    assert reasons(result) == ["DUPLICATE_EXACT", "DUPLICATE_EXACT"]
    assert result.rejected["source_row"].tolist() == [2, 3]


def test_duplicates_are_detected_after_normalisation() -> None:
    result = clean(
        {}, {"order_date": "15/01/2025", "unit_price": "10,50", "payment_method": "paypal"}
    )
    assert len(result.clean) == 1 and reasons(result) == ["DUPLICATE_EXACT"]


def test_conflicting_duplicates_keep_first_occurrence() -> None:
    result = clean({"quantity": "2"}, {"quantity": "3"})
    assert result.clean["quantity"].tolist() == [2]
    assert reasons(result) == ["DUPLICATE_ORDER_ID_CONFLICT"]


def test_invalid_first_occurrence_does_not_hide_a_valid_duplicate() -> None:
    result = clean({"quantity": "0"}, {"quantity": "2"})
    assert result.clean["quantity"].tolist() == [2]
    assert reasons(result) == ["QUANTITY_OUT_OF_RANGE"]


# --- traceability and structure -----------------------------------------------
def test_no_row_disappears_silently() -> None:
    raw = make_raw(
        {},
        {},
        {"order_id": "2", "quantity": "0"},
        {"order_id": "3", "order_date": "x"},
        {"order_id": "4"},
    )
    result = clean_orders(raw, run_date=RUN_DATE)
    assert len(result.clean) + len(result.rejected) == len(raw)
    assert result.report["rows_raw"] == 5


def test_rejected_rows_keep_original_raw_values_and_trace() -> None:
    raw = make_raw({}, {"order_id": "ORD-9", "product_name": " Bad ", "quantity": "-4"})
    raw["_row_number"] = [10, 11]
    result = clean_orders(raw, run_date=RUN_DATE, run_id="run-7", source_file="orders.csv")
    rejected = result.rejected.iloc[0]
    assert rejected["quantity"] == "-4" and rejected["product_name"] == " Bad "  # raw, unstripped
    assert (rejected["source_row"], rejected["source_file"], rejected["run_id"]) == (
        11,
        "orders.csv",
        "run-7",
    )


def test_missing_required_column_raises_schema_error() -> None:
    with pytest.raises(SchemaError, match="order_date"):
        clean_orders(make_raw({}).drop(columns=["order_date"]), run_date=RUN_DATE)


def test_extra_columns_are_ignored_and_reported() -> None:
    raw = make_raw({})
    raw["comment"] = "hello"
    raw["_run_id"] = "r"  # Bronze metadata: expected, never reported as extra
    result = clean_orders(raw, run_date=RUN_DATE)
    assert list(result.clean.columns) == list(REQUIRED_COLUMNS)
    assert result.report["extra_columns_ignored"] == ["comment"]


def test_empty_input_returns_empty_outputs() -> None:
    result = clean_orders(make_raw().iloc[0:0], run_date=RUN_DATE)
    assert result.clean.empty and result.rejected.empty
    assert result.report["rows_raw"] == 0 and result.report["rejection_rate_pct"] == 0.0


def test_report_counts_match_rows() -> None:
    result = clean({}, {"order_id": "2", "quantity": "0"})
    report = result.report
    assert (report["rows_raw"], report["rows_clean"], report["rows_rejected"]) == (2, 1, 1)
    assert report["rejection_rate_pct"] == 50.0
    assert report["rejection_reasons"] == {"QUANTITY_OUT_OF_RANGE": 1}


# --- ground truth from the synthetic generator --------------------------------
def test_pipeline_detects_exactly_the_injected_anomalies() -> None:
    raw, manifest = generate_raw_dataset(1000, seed=42)
    bronze = build_bronze(
        raw, "orders_raw.csv", "r", pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime()
    )
    result = clean_orders(
        bronze, run_date=date(2026, 6, 1), run_id="r", source_file="orders_raw.csv"
    )
    expected = manifest["expected_pipeline_outcome"]
    injected = manifest["anomalies_injected"]
    report = result.report

    assert report["rows_clean"] == expected["rows_clean"]
    assert report["rows_rejected"] == expected["rows_rejected"]
    assert report["rows_rejected_as_duplicates"] == expected["rows_rejected_as_duplicates"]
    assert result.clean["order_id"].is_unique

    reasons_found = report["rejection_reasons"]
    assert reasons_found["DUPLICATE_EXACT"] == injected["exact_duplicates"]
    assert reasons_found["DUPLICATE_ORDER_ID_CONFLICT"] == injected["conflicting_duplicates"]
    assert reasons_found["QUANTITY_OUT_OF_RANGE"] == (
        injected["quantity_non_positive"] + injected["quantity_above_max"]
    )
    assert reasons_found["UNIT_PRICE_OUT_OF_RANGE"] == (
        injected["unit_price_non_positive"] + injected["unit_price_above_max"]
    )

    notes = report["normalisations_applied_to_clean_rows"]
    assert notes["dates_converted_from_dd_mm_yyyy"] == injected["order_date_dd_mm_yyyy"]
    assert notes["unit_prices_with_decimal_comma"] == injected["unit_price_decimal_comma"]
    assert (
        notes["total_amount_inconsistent_then_recomputed"] == injected["total_amount_inconsistent"]
    )
    assert notes["total_amount_missing_then_computed"] == injected["total_amount_missing"]
    unknown = report["values_replaced_by_unknown"]
    assert unknown["category"] == injected["missing_category"]
    assert unknown["payment_method_unrecognised"] == injected["unrecognised_payment_method"]

    recomputed = (result.clean["quantity"] * result.clean["unit_price"]).round(2)
    assert (result.clean["total_amount"] - recomputed).abs().max() < 1e-9
