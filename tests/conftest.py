"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg2
import pytest

from src.config import PROJECT_ROOT, Settings, load_settings
from src.validation import REQUIRED_COLUMNS

VALID_ROW: dict[str, str] = {
    "order_id": "ORD-000001",
    "customer_id": "CUST-00001",
    "order_date": "2025-01-15",
    "category": "Books",
    "product_name": "SQL Cookbook",
    "quantity": "2",
    "unit_price": "10.50",
    "total_amount": "21.00",
    "payment_method": "PayPal",
    "city": "Paris",
    "region": "Île-de-France",
}


def make_raw(*rows: dict[str, Any]) -> pd.DataFrame:
    """Build a raw (all-string) table; each row overrides ``VALID_ROW`` values."""
    records = [{**VALID_ROW, **row} for row in rows]
    return pd.DataFrame(records, columns=list(REQUIRED_COLUMNS)).astype(str)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings writing everything under a temporary directory (no database, no S3)."""
    return load_settings(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "LOG_DIR": str(tmp_path / "logs"),
            "NUM_ORDERS": "1000",
            "RANDOM_SEED": "7",
        }
    )


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent a developer's ``.env`` or shell variables from leaking into tests."""
    monkeypatch.setattr("src.config.load_dotenv", lambda *args, **kwargs: False)
    for name in list(os.environ):
        if name.startswith(("POSTGRES_", "S3_", "LOAD_TO_POSTGRES", "NUM_ORDERS", "RANDOM_SEED")):
            monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# PostgreSQL integration tests (skipped unless TEST_POSTGRES_* is configured)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pg_env() -> dict[str, str]:
    """Return PostgreSQL settings for tests, or skip when unavailable.

    Safety: the database name must end with ``_test`` because the fixtures
    drop the ``ecommerce`` schema.
    """
    env = {
        "POSTGRES_HOST": os.environ.get("TEST_POSTGRES_HOST", ""),
        "POSTGRES_PORT": os.environ.get("TEST_POSTGRES_PORT", "5432"),
        "POSTGRES_DB": os.environ.get("TEST_POSTGRES_DB", ""),
        "POSTGRES_USER": os.environ.get("TEST_POSTGRES_USER", ""),
        "POSTGRES_PASSWORD": os.environ.get("TEST_POSTGRES_PASSWORD", ""),
    }
    if not all(
        env[k] for k in ("POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    ):
        pytest.skip("TEST_POSTGRES_* variables not set")
    if not env["POSTGRES_DB"].endswith("_test"):
        pytest.skip("TEST_POSTGRES_DB must end with '_test' (safety guard)")
    try:
        psycopg2.connect(
            connect_timeout=3,
            host=env["POSTGRES_HOST"],
            port=env["POSTGRES_PORT"],
            dbname=env["POSTGRES_DB"],
            user=env["POSTGRES_USER"],
            password=env["POSTGRES_PASSWORD"],
        ).close()
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL test database unreachable: {exc}")
    return env


@pytest.fixture
def pg_settings(pg_env: dict[str, str], tmp_path: Path) -> Iterator[Settings]:
    """Settings pointing at the test database, with a freshly dropped schema."""
    settings = load_settings(
        {
            **pg_env,
            "LOAD_TO_POSTGRES": "true",
            "DATA_DIR": str(tmp_path / "data"),
            "LOG_DIR": str(tmp_path / "logs"),
            "SQL_DIR": str(PROJECT_ROOT / "sql"),
        }
    )
    _drop_schema(settings)
    yield settings
    _drop_schema(settings)


def _drop_schema(settings: Settings) -> None:
    conn = psycopg2.connect(**settings.postgres_connection_kwargs())
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS ecommerce CASCADE")
    finally:
        conn.close()
