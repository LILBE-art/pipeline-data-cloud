"""Dataset generation, raw file reading and Bronze layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from src.extract import (
    MANIFEST_FILENAME,
    RAW_FILENAME,
    build_bronze,
    ensure_raw_dataset,
    generate_raw_dataset,
    read_raw_csv,
    write_raw_dataset,
)
from src.validation import REQUIRED_COLUMNS


def test_generation_is_reproducible_with_a_fixed_seed() -> None:
    first, manifest_first = generate_raw_dataset(1000, seed=42)
    second, manifest_second = generate_raw_dataset(1000, seed=42)
    other, _ = generate_raw_dataset(1000, seed=43)
    assert first.equals(second)
    assert manifest_first == manifest_second
    assert not first.equals(other)


def test_generated_dataset_has_expected_shape_and_flags() -> None:
    raw, manifest = generate_raw_dataset(1000, seed=1)
    assert list(raw.columns) == list(REQUIRED_COLUMNS)
    assert manifest["synthetic_data"] is True
    assert manifest["orders_before_anomalies"] == 1000
    duplicates = (
        manifest["anomalies_injected"]["exact_duplicates"]
        + (manifest["anomalies_injected"]["conflicting_duplicates"])
    )
    assert len(raw) == manifest["rows_in_raw_file"] == 1000 + duplicates
    assert raw["order_id"].duplicated().sum() == duplicates


def test_generated_dataset_contains_the_documented_anomalies() -> None:
    raw, _ = generate_raw_dataset(1000, seed=1)
    assert (raw["order_date"].str.contains("/")).any()  # DD/MM/YYYY dates
    assert (raw["unit_price"].str.contains(",")).any()  # decimal comma
    assert (raw["customer_id"].isin(["", "N/A", "NULL"])).any()  # missing values
    assert (raw["quantity"].isin(["0", "-1", "-2", "-3"])).any()  # invalid quantities
    assert raw["order_id"].duplicated().any()  # duplicates


def test_write_raw_dataset_creates_csv_and_manifest(tmp_path: Path) -> None:
    raw, manifest = generate_raw_dataset(1000, seed=1)
    path = write_raw_dataset(raw, manifest, tmp_path)
    assert path == tmp_path / RAW_FILENAME
    saved = json.loads((tmp_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert saved["synthetic_data"] is True


def test_write_raw_dataset_never_overwrites_silently(tmp_path: Path) -> None:
    raw, manifest = generate_raw_dataset(1000, seed=1)
    write_raw_dataset(raw, manifest, tmp_path)
    with pytest.raises(FileExistsError):
        write_raw_dataset(raw, manifest, tmp_path)
    write_raw_dataset(raw, manifest, tmp_path, overwrite=True)  # explicit opt-in works


def test_read_raw_csv_keeps_every_value_as_text(tmp_path: Path) -> None:
    path = tmp_path / "raw.csv"
    path.write_text("order_id,quantity,city\nORD-1,007,\nORD-2,N/A,Paris\n", encoding="utf-8")
    frame = read_raw_csv(path)
    assert frame["quantity"].tolist() == ["007", "N/A"]  # not converted to numbers/NaN
    assert frame["city"].tolist() == ["", "Paris"]  # empty stays an empty string


def test_read_raw_csv_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        read_raw_csv(tmp_path / "absent.csv")


def test_read_raw_csv_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        read_raw_csv(path)


def test_ensure_raw_dataset_generates_then_keeps_existing_file(settings) -> None:
    path = ensure_raw_dataset(settings)
    original = path.read_bytes()
    path.write_bytes(original + b"ORD-X,manual edit\n")  # simulate a hand-fixed raw file
    ensure_raw_dataset(settings)
    assert path.read_bytes() == original + b"ORD-X,manual edit\n"  # untouched


def test_ensure_raw_dataset_regenerate_replaces_file(settings) -> None:
    path = ensure_raw_dataset(settings)
    original = path.read_bytes()
    path.write_bytes(b"garbage")
    ensure_raw_dataset(settings, regenerate=True)
    assert path.read_bytes() == original  # same seed -> same content


def test_bronze_keeps_raw_values_and_adds_metadata() -> None:
    raw, _ = generate_raw_dataset(1000, seed=1)
    ingested_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    bronze = build_bronze(raw, "orders_raw.csv", "run-1", ingested_at)
    pd.testing.assert_frame_equal(bronze[list(REQUIRED_COLUMNS)], raw)  # content unchanged
    assert (bronze["_source_file"] == "orders_raw.csv").all()
    assert (bronze["_run_id"] == "run-1").all()
    assert (bronze["_ingested_at"] == "2026-01-02T03:04:05+00:00").all()
    assert bronze["_row_number"].tolist() == list(range(1, len(raw) + 1))
