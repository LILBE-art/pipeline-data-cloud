"""File exports, database helpers and PostgreSQL integration tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import psycopg2
import pytest
from psycopg2 import OperationalError

from src import load
from src.config import Settings, load_settings
from src.extract import build_bronze, generate_raw_dataset
from src.load import DatabaseError, compare_tables, wait_for_db
from src.metrics import GOLD_TABLES, compute_gold_tables
from src.transform import clean_orders

# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline_frames():
    raw, _ = generate_raw_dataset(1000, seed=42)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    bronze = build_bronze(raw, "orders_raw.csv", "run-x", started)
    result = clean_orders(
        bronze, run_date=date(2026, 6, 1), run_id="run-x", source_file="orders_raw.csv"
    )
    return bronze, result, compute_gold_tables(result.clean), started


def test_exports_write_every_layer(tmp_path: Path, pipeline_frames) -> None:
    bronze, result, gold, _ = pipeline_frames
    bronze_path = load.export_bronze(bronze, tmp_path / "raw")
    silver_paths = load.export_silver(result.clean, result.report, tmp_path / "processed")
    rejected_path = load.export_rejected(result.rejected, tmp_path / "rejected")
    gold_paths = load.export_gold(gold, tmp_path / "gold")

    assert bronze_path.name == "orders_bronze.csv"
    assert pd.read_csv(bronze_path, dtype=str, keep_default_na=False).shape[0] == len(bronze)
    assert pd.read_csv(silver_paths[0]).shape[0] == len(result.clean)
    report = json.loads(silver_paths[1].read_text(encoding="utf-8"))
    assert report["rows_clean"] == len(result.clean)
    assert pd.read_csv(rejected_path).shape[0] == len(result.rejected)
    assert sorted(p.stem for p in gold_paths) == sorted(GOLD_TABLES)


def test_silver_csv_uses_iso_dates_and_two_decimals(tmp_path: Path, pipeline_frames) -> None:
    _, result, _, _ = pipeline_frames
    path, _ = load.export_silver(result.clean, result.report, tmp_path)
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert table["order_date"].str.fullmatch(r"\d{4}-\d{2}-\d{2}").all()
    for column in ("unit_price", "total_amount"):
        assert table[column].str.fullmatch(r"\d+\.\d{2}").all()
    assert len(table) == len(result.clean)


def test_gold_csv_is_bi_friendly(tmp_path: Path, pipeline_frames) -> None:
    _, _, gold, _ = pipeline_frames
    paths = load.export_gold(gold, tmp_path)
    region_csv = next(p for p in paths if p.stem == "sales_by_region")
    assert region_csv.read_bytes().startswith(b"\xef\xbb\xbf")  # UTF-8 BOM for Power BI / Excel
    table = pd.read_csv(region_csv, encoding="utf-8-sig")
    assert "Île-de-France" in table["region"].tolist()  # accents preserved
    assert table["revenue_share_pct"].sum() == pytest.approx(100.0, abs=0.1)


# --------------------------------------------------------------------------
# Database helpers (no server needed)
# --------------------------------------------------------------------------
def _db_settings() -> Settings:
    return load_settings(
        {"LOAD_TO_POSTGRES": "true", "POSTGRES_PASSWORD": "x", "POSTGRES_HOST": "nohost"}
    )


def test_wait_for_db_retries_until_ready() -> None:
    attempts: list[int] = []

    class FakeConnection:
        def close(self) -> None:
            pass

    def fake_connect(**_kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise OperationalError("connection refused")
        return FakeConnection()

    sleeps: list[float] = []
    wait_for_db(_db_settings(), retries=5, delay=0.5, connect=fake_connect, sleep=sleeps.append)
    assert len(attempts) == 3 and sleeps == [0.5, 0.5]


def test_wait_for_db_gives_up_with_clear_error() -> None:
    def always_down(**_kwargs):
        raise OperationalError("connection refused")

    with pytest.raises(DatabaseError, match=r"nohost:5432 after 2 attempts"):
        wait_for_db(_db_settings(), retries=2, connect=always_down, sleep=lambda _s: None)


def test_compare_tables_detects_numeric_and_key_differences() -> None:
    expected = pd.DataFrame({"region": ["A", "B"], "revenue": [10.0, 20.0]})
    same = pd.DataFrame(
        {"region": ["B", "A"], "revenue": [20.004, 10.0]}
    )  # order ignored, tolerance
    different = pd.DataFrame({"region": ["A", "B"], "revenue": [10.0, 25.0]})
    missing_key = pd.DataFrame({"region": ["A"], "revenue": [10.0]})
    assert compare_tables("t", expected, same, "region") == []
    assert "revenue" in compare_tables("t", expected, different, "region")[0]
    assert "row keys differ" in compare_tables("t", expected, missing_key, "region")[0]


def test_compare_tables_treats_nulls_as_equal() -> None:
    expected = pd.DataFrame({"month": ["2025-01-01"], "mom": [float("nan")]})
    actual = pd.DataFrame({"month": ["2025-01-01"], "mom": [None]})
    assert compare_tables("t", expected, actual, "month") == []


# --------------------------------------------------------------------------
# PostgreSQL integration (skipped unless TEST_POSTGRES_* is configured)
# --------------------------------------------------------------------------
def _load(settings: Settings, pipeline_frames, run_id: str = "run-x") -> dict:
    _, result, gold, started = pipeline_frames
    return load.load_to_postgres(
        settings,
        result.clean,
        result.rejected,
        gold,
        run_id=run_id,
        source_file="orders_raw.csv",
        started_at=started,
        rows_raw=1075,
    )


def _scalar(settings: Settings, sql: str):
    conn = psycopg2.connect(**settings.postgres_connection_kwargs())
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]
    finally:
        conn.close()


@pytest.mark.integration
def test_first_load_inserts_everything_and_reconciles(
    pg_settings: Settings, pipeline_frames
) -> None:
    _, result, _, _ = pipeline_frames
    stats = _load(pg_settings, pipeline_frames)
    assert stats["rows_inserted"] == len(result.clean)
    assert stats["rows_updated"] == 0
    assert stats["rejected_rows_stored"] == len(result.rejected)
    assert stats["reconciliation"] == "OK"
    assert _scalar(pg_settings, "SELECT count(*) FROM ecommerce.orders") == len(result.clean)
    revenue = float(_scalar(pg_settings, "SELECT total_revenue FROM ecommerce.v_kpi_summary"))
    assert revenue == pytest.approx(float(result.clean["total_amount"].sum()), abs=0.005)


@pytest.mark.integration
def test_second_load_is_idempotent(pg_settings: Settings, pipeline_frames) -> None:
    _, result, _, _ = pipeline_frames
    _load(pg_settings, pipeline_frames, run_id="run-1")
    stats = _load(pg_settings, pipeline_frames, run_id="run-2")
    assert (stats["rows_inserted"], stats["rows_updated"]) == (0, 0)
    assert stats["rows_unchanged"] == len(result.clean)
    assert _scalar(pg_settings, "SELECT count(*) FROM ecommerce.orders") == len(result.clean)
    assert _scalar(pg_settings, "SELECT count(*) FROM ecommerce.rejected_orders") == len(
        result.rejected
    )
    assert _scalar(pg_settings, "SELECT count(*) FROM ecommerce.pipeline_runs") == 2


@pytest.mark.integration
def test_changed_row_is_updated_not_duplicated(pg_settings: Settings, pipeline_frames) -> None:
    _, result, gold, started = pipeline_frames
    _load(pg_settings, pipeline_frames)
    modified = result.clean.copy()
    modified.loc[0, "quantity"] += 1
    modified.loc[0, "total_amount"] = round(
        modified.loc[0, "quantity"] * modified.loc[0, "unit_price"], 2
    )
    stats = load.load_to_postgres(
        pg_settings,
        modified,
        result.rejected,
        compute_gold_tables(modified),
        run_id="run-2",
        source_file="orders_raw.csv",
        started_at=started,
        rows_raw=1075,
    )
    assert (stats["rows_inserted"], stats["rows_updated"]) == (0, 1)
    assert _scalar(pg_settings, "SELECT count(*) FROM ecommerce.orders") == len(result.clean)


@pytest.mark.integration
def test_database_constraints_reject_invalid_data(pg_settings: Settings, pipeline_frames) -> None:
    _load(pg_settings, pipeline_frames)
    conn = psycopg2.connect(**pg_settings.postgres_connection_kwargs())
    try:
        for statement in (
            # inconsistent total_amount
            (
                "INSERT INTO ecommerce.orders VALUES "
                "('X1','C','2025-01-01','c','p',2,10.00,99.00,'PayPal','x','y','r')"
            ),
            # quantity out of range
            (
                "INSERT INTO ecommerce.orders VALUES "
                "('X2','C','2025-01-01','c','p',0,10.00,0.00,'PayPal','x','y','r')"
            ),
            # duplicate primary key
            "INSERT INTO ecommerce.orders SELECT * FROM ecommerce.orders LIMIT 1",
        ):
            with pytest.raises(psycopg2.errors.IntegrityError), conn.cursor() as cur:
                cur.execute(statement)
            conn.rollback()
    finally:
        conn.close()


@pytest.mark.integration
def test_reconciliation_detects_tampered_gold_table(pg_settings: Settings, pipeline_frames) -> None:
    _, result, gold, started = pipeline_frames
    tampered = dict(gold)
    tampered["sales_by_region"] = gold["sales_by_region"].assign(revenue=lambda d: d["revenue"] + 1)
    with pytest.raises(DatabaseError, match="disagree"):
        load.load_to_postgres(
            pg_settings,
            result.clean,
            result.rejected,
            tampered,
            run_id="run-x",
            source_file="orders_raw.csv",
            started_at=started,
            rows_raw=1075,
        )


@pytest.mark.integration
def test_unreachable_database_raises_database_error(pg_settings: Settings, pipeline_frames) -> None:
    broken = load_settings(
        {
            "LOAD_TO_POSTGRES": "true",
            "POSTGRES_PASSWORD": "wrong",
            "POSTGRES_HOST": pg_settings.postgres_host,
            "POSTGRES_PORT": str(pg_settings.postgres_port),
            "POSTGRES_DB": pg_settings.postgres_db,
            "POSTGRES_USER": pg_settings.postgres_user,
        }
    )
    with pytest.raises(DatabaseError):
        load.wait_for_db(broken, retries=1, delay=0, sleep=lambda _s: None)
