"""Silver layer: cleaning, deduplication, rejection and quality report.

Processing order (each step is documented in ``docs/data_dictionary.md``):

1. Check the schema (mandatory columns).
2. Trim whitespace and detect null tokens.
3. Parse dates and numbers, apply row-level business rules
   (:func:`src.validation.evaluate_rows`).
4. Deduplicate among the *valid* rows:

   * identical rows            -> keep the first, reject the others
     (``DUPLICATE_EXACT``);
   * same ``order_id`` but different content -> keep the first, reject the
     others (``DUPLICATE_ORDER_ID_CONFLICT``).
5. Recompute ``total_amount = quantity * unit_price``.

No row disappears silently: ``rows_clean + rows_rejected == rows_raw`` is
enforced, and every rejected row is returned with its reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd

from src.logger import get_logger
from src.validation import (
    CANONICAL_PAYMENT_METHODS,
    OPTIONAL_DIMENSIONS,
    REQUIRED_COLUMNS,
    UNKNOWN,
    evaluate_rows,
    is_missing,
    validate_schema,
)

logger = get_logger("transform")

CLEAN_COLUMNS: tuple[str, ...] = REQUIRED_COLUMNS
REJECTED_META_COLUMNS: tuple[str, ...] = ("run_id", "source_file", "source_row", "reject_reason")
_DEDUP_COLUMNS: tuple[str, ...] = tuple(c for c in REQUIRED_COLUMNS if c != "total_amount")
_TOTAL_TOLERANCE = 0.005

BUSINESS_RULES: dict[str, str] = {
    "mandatory_values": "order_id, customer_id, order_date, product_name, quantity, unit_price",
    "order_date": "YYYY-MM-DD or DD/MM/YYYY; between 2020-01-01 and the run date",
    "quantity": "whole number between 1 and 100",
    "unit_price": "number between 0.01 and 10000.00 ('.' or ',' decimal separator)",
    "total_amount": "always recomputed as quantity * unit_price (rounded to 2 decimals)",
    "optional_dimensions": "category, payment_method, city, region -> 'Unknown' when missing",
    "duplicates": "first occurrence kept; later identical or conflicting rows rejected",
}


@dataclass(frozen=True)
class CleaningResult:
    """Output of :func:`clean_orders`."""

    clean: pd.DataFrame
    rejected: pd.DataFrame
    report: dict[str, Any]


def _standardise_payment(stripped: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Map payment methods to their canonical spelling.

    Returns:
        ``(canonical, case_standardised, unrecognised)`` where ``canonical``
        holds the canonical value or ``UNKNOWN``.
    """
    missing = is_missing(stripped)
    canonical = stripped.str.lower().map(CANONICAL_PAYMENT_METHODS)
    unrecognised = ~missing & canonical.isna()
    case_standardised = ~missing & canonical.notna() & (stripped != canonical)
    return canonical.fillna(UNKNOWN).astype(object), case_standardised, unrecognised


def _build_typed_frame(stripped: pd.DataFrame, evaluation: pd.DataFrame) -> pd.DataFrame:
    """Combine parsed numeric/date values and standardised text columns."""
    typed = pd.DataFrame(index=stripped.index)
    for column in ("order_id", "customer_id", "product_name"):
        typed[column] = stripped[column].astype(object)
    typed["order_date"] = evaluation["order_date"]
    typed["quantity"] = evaluation["quantity"]
    typed["unit_price"] = evaluation["unit_price"]

    payment, _, _ = _standardise_payment(stripped["payment_method"])
    typed["payment_method"] = payment
    for column in ("category", "city", "region"):
        typed[column] = stripped[column].where(~is_missing(stripped[column]), UNKNOWN)
        typed[column] = typed[column].astype(object)
    return typed


def _compute_total(quantity: pd.Series, unit_price: pd.Series) -> pd.Series:
    """Compute ``quantity * unit_price`` using integer cents (no float drift)."""
    cents = np.rint(unit_price.to_numpy(dtype=float) * 100).astype("int64")
    return pd.Series(quantity.to_numpy(dtype="int64") * cents / 100, index=quantity.index).round(2)


def _flag_duplicates(typed: pd.DataFrame, valid: pd.Series) -> pd.Series:
    """Return a Series of duplicate reason codes (``""`` for non-duplicates)."""
    reasons = pd.Series("", index=typed.index, dtype=object)
    valid_rows = typed[valid]
    exact = valid_rows.duplicated(subset=list(_DEDUP_COLUMNS), keep="first")
    reasons.loc[exact[exact].index] = "DUPLICATE_EXACT"

    remaining = valid_rows[~exact]
    conflict = remaining.duplicated(subset=["order_id"], keep="first")
    reasons.loc[conflict[conflict].index] = "DUPLICATE_ORDER_ID_CONFLICT"
    return reasons


def _count_reasons(reasons: pd.Series) -> dict[str, int]:
    """Count how many rows carry each reason code (a row may have several)."""
    counts: dict[str, int] = {}
    for value in reasons[reasons != ""]:
        for code in str(value).split(";"):
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def clean_orders(
    bronze: pd.DataFrame,
    run_date: date | None = None,
    run_id: str = "",
    source_file: str = "",
) -> CleaningResult:
    """Clean raw orders and split them into accepted and rejected rows.

    Args:
        bronze: Raw table of strings. It must contain ``REQUIRED_COLUMNS``; an
            optional ``_row_number`` column is used to trace rejected rows.
        run_date: Latest acceptable order date (defaults to today, UTC).
        run_id: Identifier of the pipeline run, stored with rejected rows.
        source_file: Name of the source file, stored with rejected rows.

    Returns:
        A :class:`CleaningResult` with the Silver table, the rejected rows
        (original raw values + reason) and the data quality report.

    Raises:
        SchemaError: If a required column is missing.
        RuntimeError: If the row accounting does not balance.
    """
    run_date = run_date or datetime.now(UTC).date()
    # Bronze metadata columns (prefixed with '_') are expected, not 'extra'.
    extra_columns = [c for c in validate_schema(bronze.columns) if not c.startswith("_")]

    raw_values = bronze[list(REQUIRED_COLUMNS)].fillna("").astype(str)
    stripped = raw_values.apply(lambda column: column.str.strip())
    whitespace_mask = (raw_values != stripped).any(axis=1)

    evaluation = evaluate_rows(stripped, run_date)
    typed = _build_typed_frame(stripped, evaluation)

    reasons = evaluation["reject_reason"].astype(object).copy()
    valid = reasons == ""
    duplicate_reasons = _flag_duplicates(typed, valid)
    reasons = reasons.where(duplicate_reasons == "", duplicate_reasons)
    keep = reasons == ""

    clean = typed[keep].copy()
    clean["quantity"] = clean["quantity"].astype("int64")
    clean["unit_price"] = clean["unit_price"].astype(float).round(2)
    clean["total_amount"] = _compute_total(clean["quantity"], clean["unit_price"])
    clean = clean[list(CLEAN_COLUMNS)].sort_values("order_id", kind="stable").reset_index(drop=True)

    row_numbers = (
        pd.to_numeric(bronze["_row_number"], errors="coerce")
        if "_row_number" in bronze.columns
        else pd.Series(np.arange(1, len(bronze) + 1), index=bronze.index)
    )
    rejected = raw_values[~keep].copy()
    rejected.insert(0, "reject_reason", reasons[~keep].to_numpy())
    rejected.insert(0, "source_row", row_numbers[~keep].astype("int64").to_numpy())
    rejected.insert(0, "source_file", source_file)
    rejected.insert(0, "run_id", run_id)
    rejected = rejected.reset_index(drop=True)

    if len(clean) + len(rejected) != len(bronze):
        raise RuntimeError("Row accounting error: clean + rejected != raw")

    report = _build_report(
        bronze=bronze,
        stripped=stripped,
        evaluation=evaluation,
        reasons=reasons,
        keep=keep,
        clean=clean,
        rejected=rejected,
        extra_columns=extra_columns,
        whitespace_mask=whitespace_mask,
        run_date=run_date,
        run_id=run_id,
        source_file=source_file,
    )
    logger.info(
        "Silver: %d raw rows -> %d clean, %d rejected",
        len(bronze),
        len(clean),
        len(rejected),
    )
    return CleaningResult(clean=clean, rejected=rejected, report=report)


def _build_report(
    *,
    bronze: pd.DataFrame,
    stripped: pd.DataFrame,
    evaluation: pd.DataFrame,
    reasons: pd.Series,
    keep: pd.Series,
    clean: pd.DataFrame,
    rejected: pd.DataFrame,
    extra_columns: list[str],
    whitespace_mask: pd.Series,
    run_date: date,
    run_id: str,
    source_file: str,
) -> dict[str, Any]:
    """Assemble the data quality report (counts are computed on kept rows)."""
    kept_eval = evaluation[keep]
    kept_stripped = stripped[keep]

    recomputed = _compute_total(
        kept_eval["quantity"].astype("int64"), kept_eval["unit_price"].astype(float).round(2)
    )
    total_raw = kept_eval["total_raw"]
    total_filled = int(kept_eval["total_missing"].sum())
    total_recomputed = int(
        (
            ~kept_eval["total_missing"] & ((total_raw - recomputed).abs().fillna(np.inf) > 0.005)
        ).sum()
    )

    _, case_standardised, unrecognised = _standardise_payment(kept_stripped["payment_method"])
    imputed = {
        column: int(is_missing(kept_stripped[column]).sum()) for column in OPTIONAL_DIMENSIONS
    }

    rows_raw = len(bronze)
    rows_clean = len(clean)
    duplicates = reasons.isin(["DUPLICATE_EXACT", "DUPLICATE_ORDER_ID_CONFLICT"])
    return {
        "run_id": run_id,
        "run_date": run_date.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_file": source_file,
        "extra_columns_ignored": extra_columns,
        "rows_raw": rows_raw,
        "rows_clean": rows_clean,
        "rows_rejected": len(rejected),
        "rejection_rate_pct": round(100 * len(rejected) / rows_raw, 2) if rows_raw else 0.0,
        "rows_rejected_as_duplicates": int(duplicates.sum()),
        "rejection_reasons": _count_reasons(reasons),
        "normalisations_applied_to_clean_rows": {
            "rows_with_whitespace_trimmed": int(whitespace_mask[keep].sum()),
            "dates_converted_from_dd_mm_yyyy": int(kept_eval["flag_date_dd_mm_yyyy"].sum()),
            "unit_prices_with_decimal_comma": int(kept_eval["flag_price_decimal_comma"].sum()),
            "quantities_written_with_decimals": int(
                kept_eval["flag_quantity_decimal_notation"].sum()
            ),
            "payment_method_case_standardised": int(case_standardised.sum()),
            "total_amount_missing_then_computed": total_filled,
            "total_amount_inconsistent_then_recomputed": total_recomputed,
        },
        "values_replaced_by_unknown": {
            **imputed,
            "payment_method_unrecognised": int(unrecognised.sum()),
        },
        "business_rules": BUSINESS_RULES,
    }
