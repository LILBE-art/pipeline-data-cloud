"""Extraction: synthetic dataset generation, raw file reading and Bronze layer.

The dataset is **100% synthetic**: no real customer, product or transaction is
involved. It is reproducible (fixed seed) and deliberately polluted with data
quality problems. The exact number of injected anomalies is saved next to the
raw file (``orders_raw.manifest.json``), which acts as a ground truth that the
tests compare with what the pipeline detects.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import Settings
from src.logger import get_logger
from src.validation import REQUIRED_COLUMNS

logger = get_logger("extract")

RAW_FILENAME = "orders_raw.csv"
MANIFEST_FILENAME = "orders_raw.manifest.json"
BRONZE_FILENAME = "orders_bronze.csv"
BRONZE_METADATA_COLUMNS: tuple[str, ...] = (
    "_source_file",
    "_run_id",
    "_ingested_at",
    "_row_number",
)

START_DATE = "2024-01-01"
END_DATE = "2025-06-30"
CUSTOMER_POOL_SIZE = 1_200

CATALOG: dict[str, list[tuple[str, float]]] = {
    "Electronics": [
        ("Wireless Mouse", 24.90),
        ("Mechanical Keyboard", 89.00),
        ("USB-C Hub", 39.90),
        ("Bluetooth Speaker", 59.90),
        ("27-inch Monitor", 229.00),
        ("Noise-Cancelling Headphones", 149.00),
    ],
    "Home & Kitchen": [
        ("Air Fryer", 99.00),
        ("Coffee Grinder", 34.90),
        ("Non-stick Pan Set", 64.50),
        ("Robot Vacuum", 249.00),
        ("Electric Kettle", 29.90),
    ],
    "Fashion": [
        ("Running Shoes", 79.90),
        ("Denim Jacket", 69.00),
        ("Wool Scarf", 24.00),
        ("Leather Belt", 35.00),
        ("Cotton T-Shirt", 15.90),
    ],
    "Books": [
        ("Data Engineering Handbook", 44.90),
        ("SQL Cookbook", 38.00),
        ("Python Crash Guide", 32.50),
        ("Cloud Architecture Primer", 41.00),
    ],
    "Sports": [
        ("Yoga Mat", 22.00),
        ("Adjustable Dumbbells", 119.00),
        ("Cycling Helmet", 54.90),
        ("Trail Backpack", 74.00),
        ("Resistance Bands", 14.90),
    ],
    "Beauty": [
        ("Face Serum", 28.90),
        ("Hair Dryer", 59.00),
        ("Perfume", 72.00),
        ("Moisturizer", 19.90),
        ("Electric Toothbrush", 49.90),
    ],
}
CATEGORY_WEIGHTS = [0.25, 0.20, 0.22, 0.10, 0.12, 0.11]

CITY_REGION: dict[str, str] = {
    "Paris": "Île-de-France",
    "Lyon": "Auvergne-Rhône-Alpes",
    "Marseille": "Provence-Alpes-Côte d'Azur",
    "Nice": "Provence-Alpes-Côte d'Azur",
    "Toulouse": "Occitanie",
    "Montpellier": "Occitanie",
    "Nantes": "Pays de la Loire",
    "Strasbourg": "Grand Est",
    "Bordeaux": "Nouvelle-Aquitaine",
    "Lille": "Hauts-de-France",
    "Rennes": "Bretagne",
    "Reims": "Grand Est",
}
CITY_WEIGHTS = [0.22, 0.12, 0.10, 0.06, 0.09, 0.06, 0.07, 0.06, 0.08, 0.07, 0.05, 0.02]

PAYMENT_METHODS = ["Credit Card", "PayPal", "Debit Card", "Apple Pay", "Bank Transfer"]
PAYMENT_WEIGHTS = [0.42, 0.22, 0.20, 0.10, 0.06]

# Number of rows affected by each injected anomaly (all sets are disjoint).
ANOMALY_PLAN: dict[str, int] = {
    # --- rows the pipeline must REJECT -------------------------------------
    "missing_customer_id": 20,
    "missing_order_date": 10,
    "missing_product_name": 8,
    "missing_quantity": 5,
    "missing_unit_price": 5,
    "invalid_order_date": 15,
    "order_date_out_of_range": 5,
    "invalid_quantity_text": 8,
    "quantity_non_positive": 12,
    "quantity_above_max": 4,
    "invalid_unit_price_text": 10,
    "unit_price_non_positive": 10,
    "unit_price_above_max": 4,
    # --- rows the pipeline must KEEP after a documented normalisation ------
    "order_date_dd_mm_yyyy": 25,
    "unit_price_decimal_comma": 20,
    "quantity_decimal_notation": 10,
    "total_amount_inconsistent": 40,
    "total_amount_missing": 15,
    "product_name_whitespace": 20,
    "payment_method_case_variant": 20,
    # --- rows kept with the value replaced by 'Unknown' --------------------
    "missing_category": 30,
    "missing_payment_method": 30,
    "unrecognised_payment_method": 6,
    "missing_city": 25,
    "missing_region": 25,
    # --- extra rows appended to the file -----------------------------------
    "exact_duplicates": 60,
    "conflicting_duplicates": 15,
}
REJECTED_BASE_ANOMALIES = (
    "missing_customer_id",
    "missing_order_date",
    "missing_product_name",
    "missing_quantity",
    "missing_unit_price",
    "invalid_order_date",
    "order_date_out_of_range",
    "invalid_quantity_text",
    "quantity_non_positive",
    "quantity_above_max",
    "invalid_unit_price_text",
    "unit_price_non_positive",
    "unit_price_above_max",
)
DUPLICATE_ANOMALIES = ("exact_duplicates", "conflicting_duplicates")


def _day_probabilities(days: pd.DatetimeIndex) -> np.ndarray:
    """Weight days to create seasonality (weekends and Nov/Dec peaks)."""
    month_factor = {1: 0.9, 7: 0.95, 11: 1.5, 12: 1.8}
    weights = np.array(
        [month_factor.get(day.month, 1.0) * (1.15 if day.weekday() >= 5 else 1.0) for day in days]
    )
    return weights / weights.sum()


def build_clean_orders(n_orders: int, rng: np.random.Generator) -> pd.DataFrame:
    """Create ``n_orders`` valid synthetic orders (all columns as strings)."""
    days = pd.date_range(START_DATE, END_DATE, freq="D")
    order_days = np.sort(rng.choice(days.to_numpy(), size=n_orders, p=_day_probabilities(days)))

    categories = list(CATALOG)
    category_idx = rng.choice(len(categories), size=n_orders, p=CATEGORY_WEIGHTS)
    cities = list(CITY_REGION)
    city_idx = rng.choice(len(cities), size=n_orders, p=CITY_WEIGHTS)
    payment_idx = rng.choice(len(PAYMENT_METHODS), size=n_orders, p=PAYMENT_WEIGHTS)
    quantity = rng.choice([1, 2, 3, 4, 5], size=n_orders, p=[0.55, 0.25, 0.10, 0.06, 0.04])
    customer = rng.integers(1, CUSTOMER_POOL_SIZE + 1, size=n_orders)
    price_noise = rng.uniform(0.92, 1.08, size=n_orders)

    rows: list[dict[str, str]] = []
    for i in range(n_orders):
        category = categories[category_idx[i]]
        products = CATALOG[category]
        product, base_price = products[int(rng.integers(0, len(products)))]
        unit_price = max(round(base_price * float(price_noise[i]), 2), 0.01)
        total = round(int(quantity[i]) * round(unit_price * 100) / 100, 2)
        city = cities[city_idx[i]]
        rows.append(
            {
                "order_id": f"ORD-{i + 1:06d}",
                "customer_id": f"CUST-{int(customer[i]):05d}",
                "order_date": pd.Timestamp(order_days[i]).strftime("%Y-%m-%d"),
                "category": category,
                "product_name": product,
                "quantity": str(int(quantity[i])),
                "unit_price": f"{unit_price:.2f}",
                "total_amount": f"{total:.2f}",
                "payment_method": PAYMENT_METHODS[payment_idx[i]],
                "city": city,
                "region": CITY_REGION[city],
            }
        )
    return pd.DataFrame(rows, columns=list(REQUIRED_COLUMNS))


def _cycle(values: list[str], i: int) -> str:
    """Pick ``values[i]`` cyclically."""
    return values[i % len(values)]


def _deltas_at(deltas: list[float], i: int) -> float:
    """Return the ``i``-th amount shift used to corrupt ``total_amount``."""
    return deltas[i % len(deltas)]


def inject_anomalies(
    clean: pd.DataFrame, rng: np.random.Generator
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Inject documented data quality problems into a clean orders table.

    Returns:
        ``(raw_table, manifest)``. The manifest lists the injected counts and
        the expected pipeline outcome.
    """
    df = clean.copy()
    n_base = len(df)
    pool = rng.permutation(n_base)
    cursor = 0
    chosen: dict[str, np.ndarray] = {}
    for name, count in ANOMALY_PLAN.items():
        chosen[name] = pool[cursor : cursor + count]
        cursor += count
    if cursor > n_base:
        raise ValueError("Not enough rows to inject the requested anomalies")

    def blank(i: int) -> str:
        return _cycle(["", "N/A", "NULL"], i)

    def set_values(name: str, column: str, make_value: Any) -> None:
        for i, idx in enumerate(chosen[name]):
            df.at[idx, column] = make_value(i, idx)

    set_values("missing_customer_id", "customer_id", lambda i, _: blank(i))
    set_values("missing_order_date", "order_date", lambda i, _: blank(i))
    set_values("missing_product_name", "product_name", lambda i, _: blank(i))
    set_values("missing_quantity", "quantity", lambda i, _: blank(i))
    set_values("missing_unit_price", "unit_price", lambda i, _: blank(i))
    garbage_dates = ["31-02-2024", "2024/13/01", "yesterday", "2024-02-30", "15-03-24"]
    set_values("invalid_order_date", "order_date", lambda i, _: _cycle(garbage_dates, i))
    set_values(
        "order_date_out_of_range",
        "order_date",
        lambda i, _: _cycle(["2031-05-01", "1999-12-31"], i),
    )
    set_values(
        "invalid_quantity_text", "quantity", lambda i, _: _cycle(["two", "three", "abc", "1x"], i)
    )
    set_values("quantity_non_positive", "quantity", lambda i, _: _cycle(["0", "-1", "-2", "-3"], i))
    set_values(
        "quantity_above_max", "quantity", lambda i, _: _cycle(["250", "500", "1000", "101"], i)
    )
    set_values(
        "invalid_unit_price_text",
        "unit_price",
        lambda i, _: _cycle(["abc", "free", "12.5.0", "€12"], i),
    )
    set_values(
        "unit_price_non_positive",
        "unit_price",
        lambda i, _: _cycle(["0.00", "-5.00", "-19.90", "0"], i),
    )
    set_values(
        "unit_price_above_max",
        "unit_price",
        lambda i, _: _cycle(["99999.99", "15000.00", "10000.01", "250000"], i),
    )

    set_values(
        "order_date_dd_mm_yyyy",
        "order_date",
        lambda _, idx: pd.Timestamp(df.at[idx, "order_date"]).strftime("%d/%m/%Y"),
    )
    set_values(
        "unit_price_decimal_comma",
        "unit_price",
        lambda _, idx: str(df.at[idx, "unit_price"]).replace(".", ","),
    )
    set_values(
        "quantity_decimal_notation", "quantity", lambda _, idx: f"{df.at[idx, 'quantity']}.0"
    )
    deltas = [5.0, 10.0, -3.5, 12.25]
    set_values(
        "total_amount_inconsistent",
        "total_amount",
        lambda i, idx: f"{float(df.at[idx, 'total_amount']) + _deltas_at(deltas, i):.2f}",
    )
    set_values("total_amount_missing", "total_amount", lambda i, _: _cycle(["", "NULL"], i))
    set_values(
        "product_name_whitespace",
        "product_name",
        lambda i, idx: (
            f"  {df.at[idx, 'product_name']} " if i % 2 == 0 else f"{df.at[idx, 'product_name']}\t"
        ),
    )
    set_values(
        "payment_method_case_variant",
        "payment_method",
        lambda i, idx: (
            str(df.at[idx, "payment_method"]).lower()
            if i % 2 == 0
            else str(df.at[idx, "payment_method"]).upper()
        ),
    )
    set_values("missing_category", "category", lambda i, _: blank(i))
    set_values("missing_payment_method", "payment_method", lambda i, _: blank(i))
    set_values(
        "unrecognised_payment_method",
        "payment_method",
        lambda i, _: _cycle(["Bitcoin", "Cash on delivery", "Gift voucher"], i),
    )
    set_values("missing_city", "city", lambda i, _: blank(i))
    set_values("missing_region", "region", lambda i, _: blank(i))

    # Duplicates are copied from the untouched valid rows (``clean``), so the
    # original always comes first in the file and keeps its own validity.
    exact = clean.loc[chosen["exact_duplicates"]].reset_index(drop=True)
    conflicting = clean.loc[chosen["conflicting_duplicates"]].reset_index(drop=True)
    conflicting["quantity"] = (conflicting["quantity"].astype(int) + 1).astype(str)
    conflicting["total_amount"] = [
        f"{int(q) * float(p):.2f}"
        for q, p in zip(conflicting["quantity"], conflicting["unit_price"], strict=True)
    ]
    raw = pd.concat([df, exact, conflicting], ignore_index=True)

    rejected_base = sum(ANOMALY_PLAN[name] for name in REJECTED_BASE_ANOMALIES)
    duplicates = sum(ANOMALY_PLAN[name] for name in DUPLICATE_ANOMALIES)
    manifest: dict[str, Any] = {
        "synthetic_data": True,
        "note": "Fully synthetic dataset. No real customer, product or transaction.",
        "orders_before_anomalies": n_base,
        "rows_in_raw_file": len(raw),
        "anomalies_injected": dict(ANOMALY_PLAN),
        "expected_pipeline_outcome": {
            "rows_rejected": rejected_base + duplicates,
            "rows_rejected_as_duplicates": duplicates,
            "rows_clean": n_base - rejected_base,
        },
    }
    return raw, manifest


def generate_raw_dataset(n_orders: int, seed: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Generate the reproducible raw dataset and its manifest.

    The same ``(n_orders, seed)`` always yields the same output.
    """
    rng = np.random.default_rng(seed)
    clean = build_clean_orders(n_orders, rng)
    raw, manifest = inject_anomalies(clean, rng)
    manifest["seed"] = seed
    return raw, manifest


def write_raw_dataset(
    raw: pd.DataFrame, manifest: dict[str, Any], raw_dir: Path, overwrite: bool = False
) -> Path:
    """Write the raw CSV and its manifest into ``raw_dir``.

    Raises:
        FileExistsError: If the raw file exists and ``overwrite`` is false
            (raw data is never replaced silently).
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    csv_path = raw_dir / RAW_FILENAME
    if csv_path.exists() and not overwrite:
        raise FileExistsError(f"{csv_path} already exists; use overwrite to replace it")
    raw.to_csv(csv_path, index=False, encoding="utf-8")
    (raw_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    logger.info("Raw dataset written: %s (%d rows)", csv_path, len(raw))
    return csv_path


def read_raw_csv(path: Path) -> pd.DataFrame:
    """Read a raw CSV without altering any value (everything stays a string).

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is empty or unreadable as CSV.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Raw file not found: {path}")
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"Raw file is empty: {path}") from exc
    except pd.errors.ParserError as exc:
        raise ValueError(f"Raw file is not valid CSV: {path} ({exc})") from exc
    logger.info("Raw file read: %s (%d rows, %d columns)", path, len(frame), frame.shape[1])
    return frame


def ensure_raw_dataset(settings: Settings, regenerate: bool = False) -> Path:
    """Return the raw CSV path, generating the synthetic dataset if needed."""
    csv_path = settings.raw_dir / RAW_FILENAME
    if regenerate or not csv_path.exists():
        raw, manifest = generate_raw_dataset(settings.num_orders, settings.random_seed)
        write_raw_dataset(raw, manifest, settings.raw_dir, overwrite=True)
    else:
        logger.info("Existing raw file kept untouched: %s", csv_path)
    return csv_path


def build_bronze(
    raw: pd.DataFrame, source_file: str, run_id: str, ingested_at: datetime
) -> pd.DataFrame:
    """Create the Bronze table: raw values unchanged plus ingestion metadata.

    ``_row_number`` is the 1-based position of the row in the source file
    (header excluded).
    """
    bronze = raw.copy()
    bronze["_source_file"] = source_file
    bronze["_run_id"] = run_id
    bronze["_ingested_at"] = ingested_at.isoformat(timespec="seconds")
    bronze["_row_number"] = np.arange(1, len(bronze) + 1)
    return bronze
