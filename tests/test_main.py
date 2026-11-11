"""End-to-end pipeline runs and command-line behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from src.config import load_settings
from src.extract import MANIFEST_FILENAME
from src.main import main, new_run_id, run_pipeline
from src.metrics import GOLD_TABLES


def test_run_id_is_unique_and_sortable() -> None:
    first, second = new_run_id(), new_run_id()
    assert first != second
    assert len(first.split("-")[0]) == 16  # YYYYmmddTHHMMSSZ


def test_full_run_produces_every_layer(settings) -> None:
    summary = run_pipeline(settings)
    manifest = json.loads((settings.raw_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    expected = manifest["expected_pipeline_outcome"]

    assert summary["rows_clean"] == expected["rows_clean"]
    assert summary["rows_rejected"] == expected["rows_rejected"]
    assert summary["rows_raw"] == manifest["rows_in_raw_file"]
    assert "postgres" not in summary and "s3_objects" not in summary  # both disabled by default

    assert (settings.raw_dir / "orders_raw.csv").is_file()
    assert (settings.raw_dir / "orders_bronze.csv").is_file()
    assert (settings.processed_dir / "orders_clean.csv").is_file()
    assert (settings.processed_dir / "data_quality_report.json").is_file()
    assert (settings.rejected_dir / "rejected_rows.csv").is_file()
    for table in GOLD_TABLES:
        assert (settings.gold_dir / f"{table}.csv").is_file()


def test_gold_files_are_consistent_with_silver(settings) -> None:
    summary = run_pipeline(settings)
    silver = pd.read_csv(settings.processed_dir / "orders_clean.csv")
    kpi = pd.read_csv(settings.gold_dir / "kpi_summary.csv", encoding="utf-8-sig").iloc[0]
    monthly = pd.read_csv(settings.gold_dir / "monthly_sales.csv", encoding="utf-8-sig")
    assert kpi["orders_count"] == len(silver) == summary["rows_clean"]
    assert kpi["total_revenue"] == pytest.approx(silver["total_amount"].sum(), abs=0.005)
    assert monthly["revenue"].sum() == pytest.approx(kpi["total_revenue"], abs=0.05)
    assert monthly["orders_count"].sum() == kpi["orders_count"]


def test_rerun_keeps_raw_data_and_gives_same_results(settings) -> None:
    first = run_pipeline(settings)
    raw_before = (settings.raw_dir / "orders_raw.csv").read_bytes()
    second = run_pipeline(settings)
    assert (settings.raw_dir / "orders_raw.csv").read_bytes() == raw_before
    assert second["run_id"] != first["run_id"]
    for key in ("rows_raw", "rows_clean", "rows_rejected", "total_revenue", "orders_count"):
        assert second[key] == first[key]


def test_pipeline_uploads_to_s3_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    settings = load_settings(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "LOG_DIR": str(tmp_path / "logs"),
            "NUM_ORDERS": "1000",
            "S3_ENABLED": "true",
            "S3_BUCKET": "test-pipeline-bucket",
            "S3_PREFIX": "demo",
        }
    )
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-pipeline-bucket")
        summary = run_pipeline(settings)
        keys = [o["Key"] for o in client.list_objects_v2(Bucket="test-pipeline-bucket")["Contents"]]
    layers = {key.split("/")[1] for key in keys}
    assert layers == {"bronze", "silver", "rejected", "gold"}
    assert summary["s3_objects"] == len(keys) == 2 + 2 + 1 + len(GOLD_TABLES)


# --- command line ---------------------------------------------------------------
def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("NUM_ORDERS", "1000")


def test_cli_success_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_env(monkeypatch, tmp_path)
    assert main(["--skip-db"]) == 0
    assert (tmp_path / "logs" / "pipeline.log").is_file()


def test_cli_configuration_error_exit_code(tmp_path, monkeypatch, capsys) -> None:
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NUM_ORDERS", "abc")
    assert main([]) == 2
    assert "NUM_ORDERS" in capsys.readouterr().err


def test_cli_load_db_without_password_is_a_configuration_error(
    tmp_path, monkeypatch, capsys
) -> None:
    _cli_env(monkeypatch, tmp_path)
    assert main(["--load-db"]) == 2
    assert "POSTGRES_PASSWORD" in capsys.readouterr().err


def test_cli_s3_without_bucket_is_a_configuration_error(tmp_path, monkeypatch, capsys) -> None:
    _cli_env(monkeypatch, tmp_path)
    assert main(["--s3"]) == 2
    assert "S3_BUCKET" in capsys.readouterr().err


def test_cli_unreachable_database_exit_code(tmp_path, monkeypatch) -> None:
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setenv("POSTGRES_PASSWORD", "x")
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "1")  # nothing listens here
    monkeypatch.setenv("DB_CONNECT_RETRIES", "1")
    monkeypatch.setenv("DB_CONNECT_DELAY_SECONDS", "0")
    assert main(["--load-db"]) == 4


def test_cli_broken_input_data_exit_code(tmp_path, monkeypatch) -> None:
    _cli_env(monkeypatch, tmp_path)
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "orders_raw.csv").write_text("order_id,customer_id\nA,B\n", encoding="utf-8")
    assert main(["--skip-db"]) == 3
