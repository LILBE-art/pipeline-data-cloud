"""Schema validation and low-level parsers."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.validation import (
    REQUIRED_COLUMNS,
    SchemaError,
    evaluate_rows,
    is_missing,
    parse_date_column,
    parse_number_column,
    validate_schema,
)
from tests.conftest import make_raw


def test_validate_schema_accepts_complete_columns_and_reports_extras() -> None:
    extras = validate_schema([*REQUIRED_COLUMNS, "_row_number", "zzz"])
    assert extras == ["_row_number", "zzz"]


def test_validate_schema_lists_every_missing_column() -> None:
    columns = [c for c in REQUIRED_COLUMNS if c not in ("unit_price", "region")]
    with pytest.raises(SchemaError, match="unit_price, region"):
        validate_schema(columns)


@pytest.mark.parametrize("value", ["", " ", "N/A", "n/a", "NULL", "None", "nan", "-"])
def test_is_missing_detects_null_tokens(value: str) -> None:
    assert is_missing(pd.Series([value])).tolist() == [True]


def test_is_missing_keeps_real_values() -> None:
    assert is_missing(pd.Series(["0", "Paris", "NAnterre"])).tolist() == [False, False, False]


def test_parse_dates_iso_and_french_format() -> None:
    dates, used_french = parse_date_column(pd.Series(["2025-03-05", "05/03/2025", "03/15/2025"]))
    assert dates.iloc[0] == pd.Timestamp("2025-03-05")
    assert dates.iloc[1] == pd.Timestamp("2025-03-05")  # DD/MM/YYYY -> 5 March
    assert pd.isna(dates.iloc[2])  # MM/DD/YYYY is NOT guessed
    assert used_french.tolist() == [False, True, False]


@pytest.mark.parametrize("value", ["2025-02-30", "31-02-2025", "yesterday", "2025/13/01", ""])
def test_parse_dates_rejects_garbage(value: str) -> None:
    dates, _ = parse_date_column(pd.Series([value]))
    assert pd.isna(dates.iloc[0])


def test_parse_numbers_accepts_comma_and_flags_it() -> None:
    values, ok, comma = parse_number_column(pd.Series(["12.50", "12,50", "-3", " 7 "]))
    assert values.tolist() == [12.5, 12.5, -3.0, 7.0]
    assert ok.tolist() == [True] * 4
    assert comma.tolist() == [False, True, False, False]


@pytest.mark.parametrize("value", ["abc", "1,234.50", "12.5.0", "€12", "", "1e3"])
def test_parse_numbers_rejects_ambiguous_or_invalid(value: str) -> None:
    values, ok, _ = parse_number_column(pd.Series([value]))
    assert not ok.iloc[0]
    assert pd.isna(values.iloc[0])


def test_evaluate_rows_marks_valid_row_with_empty_reason() -> None:
    result = evaluate_rows(make_raw({}), date(2026, 1, 1))
    assert result["reject_reason"].tolist() == [""]
    assert result.at[0, "quantity"] == 2.0


def test_evaluate_rows_joins_multiple_reasons() -> None:
    result = evaluate_rows(make_raw({"quantity": "0", "unit_price": "abc"}), date(2026, 1, 1))
    assert result.at[0, "reject_reason"] == "QUANTITY_OUT_OF_RANGE;INVALID_UNIT_PRICE"


def test_evaluate_rows_handles_empty_table() -> None:
    result = evaluate_rows(make_raw().iloc[0:0], date(2026, 1, 1))
    assert len(result) == 0
