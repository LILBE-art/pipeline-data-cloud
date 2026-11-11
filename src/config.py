"""Centralised configuration, loaded from environment variables.

All settings come from the process environment (optionally populated from a
local ``.env`` file). No secret has a default value: the PostgreSQL password
must be provided explicitly whenever database loading is enabled.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


def _get_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Read a boolean flag such as ``true``/``false``/``1``/``0``."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _get_int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    """Read an integer setting and enforce a lower bound."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_str(env: Mapping[str, str], name: str, default: str) -> str:
    """Read a string setting, falling back to ``default`` when empty."""
    raw = env.get(name)
    return default if raw is None or raw.strip() == "" else raw.strip()


def _get_path(env: Mapping[str, str], name: str, default: Path) -> Path:
    """Read a path setting; relative paths are resolved from the project root."""
    raw = env.get(name)
    path = default if raw is None or raw.strip() == "" else Path(raw.strip())
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class Settings:
    """Validated, immutable application settings."""

    data_dir: Path
    sql_dir: Path
    log_dir: Path
    log_level: str
    random_seed: int
    num_orders: int
    load_to_postgres: bool
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    db_connect_retries: int
    db_connect_delay_seconds: int
    s3_enabled: bool
    s3_bucket: str
    s3_prefix: str
    aws_region: str | None

    @property
    def raw_dir(self) -> Path:
        """Directory holding the Bronze (raw) files."""
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        """Directory holding the Silver (cleaned) files."""
        return self.data_dir / "processed"

    @property
    def rejected_dir(self) -> Path:
        """Directory holding the rejected rows."""
        return self.data_dir / "rejected"

    @property
    def gold_dir(self) -> Path:
        """Directory holding the Gold (analytical) files."""
        return self.data_dir / "gold"

    def ensure_directories(self) -> None:
        """Create every data directory used by the pipeline."""
        for directory in (self.raw_dir, self.processed_dir, self.rejected_dir, self.gold_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def postgres_connection_kwargs(self) -> dict[str, str | int]:
        """Return keyword arguments for ``psycopg2.connect``."""
        return {
            "host": self.postgres_host,
            "port": self.postgres_port,
            "dbname": self.postgres_db,
            "user": self.postgres_user,
            "password": self.postgres_password,
        }


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from environment variables.

    Args:
        env: Mapping to read from. When ``None`` the process environment is
            used, after loading an optional ``.env`` file from the project root
            (existing environment variables always win over the file).

    Raises:
        ConfigError: If a value is missing or invalid.
    """
    if env is None:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        env = os.environ

    log_level = _get_str(env, "LOG_LEVEL", "INFO").upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}, got {log_level!r}")

    load_to_postgres = _get_bool(env, "LOAD_TO_POSTGRES", False)
    postgres_password = env.get("POSTGRES_PASSWORD", "")
    if load_to_postgres and not postgres_password:
        raise ConfigError("POSTGRES_PASSWORD is required when LOAD_TO_POSTGRES is enabled")

    s3_enabled = _get_bool(env, "S3_ENABLED", False)
    s3_bucket = _get_str(env, "S3_BUCKET", "")
    if s3_enabled:
        if not s3_bucket:
            raise ConfigError("S3_BUCKET is required when S3_ENABLED is true")
        if not _BUCKET_PATTERN.match(s3_bucket):
            raise ConfigError(f"S3_BUCKET is not a valid bucket name: {s3_bucket!r}")

    return Settings(
        data_dir=_get_path(env, "DATA_DIR", PROJECT_ROOT / "data"),
        sql_dir=_get_path(env, "SQL_DIR", PROJECT_ROOT / "sql"),
        log_dir=_get_path(env, "LOG_DIR", PROJECT_ROOT / "logs"),
        log_level=log_level,
        random_seed=_get_int(env, "RANDOM_SEED", 42, minimum=0),
        num_orders=_get_int(env, "NUM_ORDERS", 5000, minimum=1000),
        load_to_postgres=load_to_postgres,
        postgres_host=_get_str(env, "POSTGRES_HOST", "localhost"),
        postgres_port=_get_int(env, "POSTGRES_PORT", 5432, minimum=1),
        postgres_db=_get_str(env, "POSTGRES_DB", "ecommerce"),
        postgres_user=_get_str(env, "POSTGRES_USER", "pipeline"),
        postgres_password=postgres_password,
        db_connect_retries=_get_int(env, "DB_CONNECT_RETRIES", 30, minimum=1),
        db_connect_delay_seconds=_get_int(env, "DB_CONNECT_DELAY_SECONDS", 2, minimum=0),
        s3_enabled=s3_enabled,
        s3_bucket=s3_bucket,
        s3_prefix=_get_str(env, "S3_PREFIX", "ecommerce-pipeline").strip("/"),
        aws_region=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or None,
    )
