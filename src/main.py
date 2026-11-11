"""Pipeline orchestration and command-line interface.

Usage::

    python -m src.main                  # local run, files only
    python -m src.main --load-db        # also load PostgreSQL
    python -m src.main --s3             # also upload to S3 (needs AWS setup)
    python -m src.main --regenerate     # regenerate the synthetic raw dataset

Flow: raw CSV -> Bronze -> validation/cleaning -> Silver -> Gold
-> PostgreSQL (optional) -> S3 (optional).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from src import extract, load, metrics, transform
from src.config import ConfigError, Settings, load_settings
from src.logger import configure_logging, get_logger
from src.s3_storage import S3Storage, S3UploadError
from src.validation import SchemaError

logger = get_logger("main")


def new_run_id(now: datetime | None = None) -> str:
    """Return a unique, sortable run identifier such as ``20260101T120000Z-ab12cd``."""
    now = now or datetime.now(UTC)
    return f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def run_pipeline(
    settings: Settings, regenerate: bool = False, upload_to_s3: bool | None = None
) -> dict[str, Any]:
    """Execute the whole pipeline and return a summary.

    Args:
        settings: Validated configuration.
        regenerate: Regenerate the synthetic raw dataset (replaces the raw file).
        upload_to_s3: Override ``settings.s3_enabled`` when not ``None``.

    Returns:
        A dictionary with the row counts and the produced file paths.
    """
    started_at = datetime.now(UTC)
    run_id = new_run_id(started_at)
    logger.info("Pipeline started (run_id=%s)", run_id)
    settings.ensure_directories()

    # --- Extract + Bronze -------------------------------------------------
    raw_path = extract.ensure_raw_dataset(settings, regenerate=regenerate)
    raw = extract.read_raw_csv(raw_path)
    bronze = extract.build_bronze(raw, raw_path.name, run_id, started_at)
    bronze_path = load.export_bronze(bronze, settings.raw_dir)

    # --- Silver -------------------------------------------------------------
    result = transform.clean_orders(
        bronze, run_date=started_at.date(), run_id=run_id, source_file=raw_path.name
    )
    silver_paths = load.export_silver(result.clean, result.report, settings.processed_dir)
    rejected_path = load.export_rejected(result.rejected, settings.rejected_dir)

    # --- Gold ----------------------------------------------------------------
    gold = metrics.compute_gold_tables(result.clean)
    gold_paths = load.export_gold(gold, settings.gold_dir)

    summary: dict[str, Any] = {
        "run_id": run_id,
        "rows_raw": len(raw),
        "rows_clean": len(result.clean),
        "rows_rejected": len(result.rejected),
        "total_revenue": float(gold["kpi_summary"].at[0, "total_revenue"]),
        "orders_count": int(gold["kpi_summary"].at[0, "orders_count"]),
        "files": {
            "bronze": [str(bronze_path)],
            "silver": [str(p) for p in silver_paths],
            "rejected": [str(rejected_path)],
            "gold": [str(p) for p in gold_paths],
        },
    }

    # --- PostgreSQL (optional) -----------------------------------------------
    if settings.load_to_postgres:
        summary["postgres"] = load.load_to_postgres(
            settings,
            result.clean,
            result.rejected,
            gold,
            run_id=run_id,
            source_file=raw_path.name,
            started_at=started_at,
            rows_raw=len(raw),
        )
    else:
        logger.info("PostgreSQL loading disabled (LOAD_TO_POSTGRES=false)")

    # --- S3 (optional) ---------------------------------------------------------
    if settings.s3_enabled if upload_to_s3 is None else upload_to_s3:
        storage = S3Storage(settings.s3_bucket, settings.s3_prefix, settings.aws_region)
        keys = storage.upload_layers(
            {
                "bronze": [raw_path, bronze_path],
                "silver": silver_paths,
                "rejected": [rejected_path],
                "gold": gold_paths,
            },
            started_at.date(),
        )
        summary["s3_objects"] = len(keys)
    else:
        logger.info("S3 upload disabled (S3_ENABLED=false)")

    logger.info(
        "Pipeline finished: %d raw -> %d clean + %d rejected | revenue %.2f over %d orders",
        summary["rows_raw"],
        summary["rows_clean"],
        summary["rows_rejected"],
        summary["total_revenue"],
        summary["orders_count"],
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m src.main", description="E-commerce ETL pipeline (Bronze/Silver/Gold)"
    )
    parser.add_argument(
        "--regenerate", action="store_true", help="regenerate the synthetic raw dataset"
    )
    db_group = parser.add_mutually_exclusive_group()
    db_group.add_argument("--load-db", action="store_true", help="load PostgreSQL")
    db_group.add_argument("--skip-db", action="store_true", help="do not load PostgreSQL")
    parser.add_argument("--s3", action="store_true", help="upload layers to AWS S3")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Program entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        if args.load_db:
            settings = replace(settings, load_to_postgres=True)
            if not settings.postgres_password:
                raise ConfigError("POSTGRES_PASSWORD is required for --load-db")
        elif args.skip_db:
            settings = replace(settings, load_to_postgres=False)
        if args.s3 and not settings.s3_bucket:
            raise ConfigError("S3_BUCKET is required for --s3")
        configure_logging(settings.log_level, settings.log_dir)
        run_pipeline(settings, regenerate=args.regenerate, upload_to_s3=True if args.s3 else None)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (SchemaError, FileNotFoundError, ValueError) as exc:
        logger.error("Input data error: %s", exc)
        return 3
    except (load.DatabaseError, S3UploadError) as exc:
        logger.error("Infrastructure error: %s", exc)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
