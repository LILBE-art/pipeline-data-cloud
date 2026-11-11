"""Schema validation and row-level business rules.

Every function here is pure (no I/O): it receives pandas objects and returns
pandas objects, which keeps the business rules easy to unit-test.

Rejection reason codes produced by :func:`evaluate_rows`:

====================================  =========================================
Code                                  Meaning
====================================  =========================================
``MISSING_ORDER_ID``                  Mandatory ``order_id`` is empty
``MISSING_CUSTOMER_ID``               Mandatory ``customer_id`` is empty
``MISSING_PRODUCT_NAME``              Mandatory ``product_name`` is empty
``MISSING_ORDER_DATE``                Mandatory ``order_date`` is empty
``INVALID_ORDER_DATE``                Date cannot be parsed (ISO or DD/MM/YYYY)
``ORDER_DATE_OUT_OF_RANGE``           Date before 2020-01-01 or in the future
``MISSING_QUANTITY``                  Mandatory ``quantity`` is empty
``INVALID_QUANTITY``                  Quantity is not a whole number
``QUANTITY_OUT_OF_RANGE``             Quantity outside [1, 100]
``MISSING_UNIT_PRICE``                Mandatory ``unit_price`` is empty
``INVALID_UNIT_PRICE``                Price is not a number
``UNIT_PRICE_OUT_OF_RANGE``           Price outside [0.01, 10000.00]
====================================  =========================================
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import numpy as np
import pandas as pd

REQUIRED_COLUMNS: tuple[str, ...] = (
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
)

# A row is rejected when one of these values is missing or invalid.
MANDATORY_COLUMNS: tuple[str, ...] = (
    "order_id",
    "customer_id",
    "order_date",
    "product_name",
    "quantity",
    "unit_price",
)

# Descriptive attributes: when missing, the value becomes ``UNKNOWN`` (counted
# in the quality report) because revenue remains computable without them.
OPTIONAL_DIMENSIONS: tuple[str, ...] = ("category", "payment_method", "city", "region")

UNKNOWN = "Unknown"

MIN_QUANTITY = 1
MAX_QUANTITY = 100
MIN_UNIT_PRICE = 0.01
MAX_UNIT_PRICE = 10_000.00
MIN_ORDER_DATE = date(2020, 1, 1)

MISSING_TOKENS = frozenset({"", "na", "n/a", "null", "none", "nan", "-"})

CANONICAL_PAYMENT_METHODS: dict[str, str] = {
    "credit card": "Credit Card",
    "debit card": "Debit Card",
    "paypal": "PayPal",
    "bank transfer": "Bank Transfer",
    "apple pay": "Apple Pay",
}

_NUMBER_PATTERN = r"-?\d+(?:[.,]\d+)?"


class SchemaError(ValueError):
    """Raised when mandatory columns are absent from the input."""


def validate_schema(columns: Iterable[str]) -> list[str]:
    """Check that every required column is present.

    Args:
        columns: Column names of the input table.

    Returns:
        The list of extra (ignored) columns.

    Raises:
        SchemaError: If at least one required column is missing.
    """
    present = set(columns)
    missing = [name for name in REQUIRED_COLUMNS if name not in present]
    if missing:
        raise SchemaError(f"Missing required columns: {', '.join(missing)}")
    return sorted(present - set(REQUIRED_COLUMNS))


def is_missing(series: pd.Series) -> pd.Series:
    """Return a boolean mask of empty values and common null tokens."""
    normalised = series.fillna("").astype(str).str.strip().str.lower()
    return normalised.isin(MISSING_TOKENS)


def parse_date_column(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Parse dates written as ``YYYY-MM-DD`` or ``DD/MM/YYYY``.

    ``DD/MM/YYYY`` is the documented French convention of the source system.
    Any other format (including ``MM/DD/YYYY`` or ``DD-MM-YYYY``) is left
    unparsed (``NaT``) so that the row is rejected instead of guessed.

    Returns:
        A tuple ``(dates, used_dd_mm_yyyy)``: the parsed datetimes and a mask of
        the values that needed the ``DD/MM/YYYY`` fallback.
    """
    text = series.fillna("").astype(str).str.strip()
    iso = pd.to_datetime(text, format="%Y-%m-%d", errors="coerce")
    french = pd.to_datetime(text, format="%d/%m/%Y", errors="coerce")
    used_french = iso.isna() & french.notna()
    return iso.fillna(french), used_french


def parse_number_column(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Parse numbers using ``.`` or ``,`` as the decimal separator.

    Thousands separators, currency symbols and multiple separators are *not*
    accepted, so ``"1,234.50"`` and ``"12.5.0"`` remain invalid.

    Returns:
        A tuple ``(values, well_formed, used_comma)``.
    """
    text = series.fillna("").astype(str).str.strip()
    well_formed = text.str.fullmatch(_NUMBER_PATTERN).fillna(False).astype(bool)
    used_comma = well_formed & text.str.contains(",", regex=False)
    cleaned = text.where(well_formed, "").str.replace(",", ".", regex=False)
    values = pd.to_numeric(cleaned, errors="coerce").astype(float)
    return values, well_formed, used_comma


def evaluate_rows(df: pd.DataFrame, max_order_date: date) -> pd.DataFrame:
    """Parse the typed columns of every row and apply the business rules.

    Args:
        df: Table of *stripped string* values containing ``REQUIRED_COLUMNS``.
        max_order_date: Latest acceptable order date (usually the run date).

    Returns:
        A table indexed like ``df`` with the parsed values (``order_date``,
        ``quantity``, ``unit_price``, ``total_raw``), the normalisation flags
        and ``reject_reason`` (``""`` when the row is valid; otherwise reason
        codes separated by ``;``).
    """
    missing = {column: is_missing(df[column]) for column in REQUIRED_COLUMNS}

    dates, date_dmy = parse_date_column(df["order_date"])
    quantity, quantity_ok, _ = parse_number_column(df["quantity"])
    price, price_ok, price_comma = parse_number_column(df["unit_price"])
    total, total_ok, _ = parse_number_column(df["total_amount"])

    quantity_whole = quantity_ok & (quantity == np.floor(quantity))
    quantity_decimal_notation = quantity_whole & df["quantity"].str.contains(r"[.,]", regex=True)

    masks: dict[str, pd.Series] = {
        "MISSING_ORDER_ID": missing["order_id"],
        "MISSING_CUSTOMER_ID": missing["customer_id"],
        "MISSING_PRODUCT_NAME": missing["product_name"],
        "MISSING_ORDER_DATE": missing["order_date"],
        "INVALID_ORDER_DATE": ~missing["order_date"] & dates.isna(),
        "ORDER_DATE_OUT_OF_RANGE": dates.notna()
        & ((dates < pd.Timestamp(MIN_ORDER_DATE)) | (dates > pd.Timestamp(max_order_date))),
        "MISSING_QUANTITY": missing["quantity"],
        "INVALID_QUANTITY": ~missing["quantity"] & ~quantity_whole,
        "QUANTITY_OUT_OF_RANGE": quantity_whole
        & ((quantity < MIN_QUANTITY) | (quantity > MAX_QUANTITY)),
        "MISSING_UNIT_PRICE": missing["unit_price"],
        "INVALID_UNIT_PRICE": ~missing["unit_price"] & ~price_ok,
        "UNIT_PRICE_OUT_OF_RANGE": price_ok & ((price < MIN_UNIT_PRICE) | (price > MAX_UNIT_PRICE)),
    }
    mask_frame = pd.DataFrame(masks, index=df.index).fillna(False).astype(bool)
    reasons = mask_frame.apply(lambda row: ";".join(row.index[row.to_numpy()]), axis=1)
    if reasons.empty:
        reasons = pd.Series(dtype=object, index=df.index)

    return pd.DataFrame(
        {
            "order_date": dates,
            "quantity": quantity,
            "unit_price": price,
            "total_raw": total.where(total_ok),
            "total_missing": missing["total_amount"],
            "flag_date_dd_mm_yyyy": date_dmy,
            "flag_price_decimal_comma": price_comma,
            "flag_quantity_decimal_notation": quantity_decimal_notation,
            "reject_reason": reasons.astype(object),
        },
        index=df.index,
    )
