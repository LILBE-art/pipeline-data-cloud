"""Configuration loading and error handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import PROJECT_ROOT, ConfigError, load_settings


def test_defaults_are_safe_and_offline() -> None:
    settings = load_settings({})
    assert settings.load_to_postgres is False
    assert settings.s3_enabled is False
    assert settings.random_seed == 42
    assert settings.num_orders == 5000
    assert settings.postgres_password == ""  # no secret has a default value
    assert (settings.db_connect_retries, settings.db_connect_delay_seconds) == (30, 2)
    assert settings.data_dir == PROJECT_ROOT / "data"


def test_overrides_are_applied_and_paths_resolved(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "DATA_DIR": "custom_data",
            "LOG_DIR": str(tmp_path / "logs"),
            "RANDOM_SEED": "123",
            "NUM_ORDERS": "2000",
            "LOG_LEVEL": "debug",
        }
    )
    assert settings.data_dir == (PROJECT_ROOT / "custom_data").resolve()
    assert settings.log_dir == tmp_path / "logs"
    assert (settings.random_seed, settings.num_orders, settings.log_level) == (123, 2000, "DEBUG")
    assert settings.raw_dir == settings.data_dir / "raw"
    assert settings.gold_dir == settings.data_dir / "gold"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("RANDOM_SEED", "abc"),
        ("RANDOM_SEED", "-1"),
        ("NUM_ORDERS", "10"),
        ("NUM_ORDERS", "many"),
        ("POSTGRES_PORT", "0"),
        ("DB_CONNECT_RETRIES", "0"),
        ("LOAD_TO_POSTGRES", "maybe"),
        ("S3_ENABLED", "2"),
        ("LOG_LEVEL", "LOUD"),
    ],
)
def test_invalid_values_raise_config_error(variable: str, value: str) -> None:
    with pytest.raises(ConfigError, match=variable):
        load_settings({variable: value})


def test_postgres_password_required_when_loading_enabled() -> None:
    with pytest.raises(ConfigError, match="POSTGRES_PASSWORD"):
        load_settings({"LOAD_TO_POSTGRES": "true"})


def test_postgres_connection_kwargs() -> None:
    settings = load_settings(
        {
            "LOAD_TO_POSTGRES": "yes",
            "POSTGRES_PASSWORD": "pw",
            "POSTGRES_HOST": "db",
            "POSTGRES_PORT": "5433",
        }
    )
    assert settings.postgres_connection_kwargs() == {
        "host": "db",
        "port": 5433,
        "dbname": "ecommerce",
        "user": "pipeline",
        "password": "pw",
    }


def test_s3_requires_bucket_when_enabled() -> None:
    with pytest.raises(ConfigError, match="S3_BUCKET"):
        load_settings({"S3_ENABLED": "true"})


def test_s3_rejects_invalid_bucket_name() -> None:
    with pytest.raises(ConfigError, match="not a valid bucket name"):
        load_settings({"S3_ENABLED": "true", "S3_BUCKET": "Bad_Bucket!"})


def test_s3_settings_are_normalised() -> None:
    settings = load_settings(
        {
            "S3_ENABLED": "true",
            "S3_BUCKET": "my-bucket",
            "S3_PREFIX": "/proj/",
            "AWS_REGION": "eu-west-3",
        }
    )
    assert settings.s3_enabled and settings.s3_bucket == "my-bucket"
    assert settings.s3_prefix == "proj"
    assert settings.aws_region == "eu-west-3"


def test_ensure_directories_creates_every_layer(tmp_path: Path) -> None:
    settings = load_settings({"DATA_DIR": str(tmp_path / "d")})
    settings.ensure_directories()
    for directory in (
        settings.raw_dir,
        settings.processed_dir,
        settings.rejected_dir,
        settings.gold_dir,
    ):
        assert directory.is_dir()
