"""Loading: CSV/JSON exports and PostgreSQL loading.

Idempotence strategy for PostgreSQL
-----------------------------------
* ``ecommerce.orders``: rows are staged with ``COPY`` then merged with
  ``INSERT ... ON CONFLICT (order_id) DO UPDATE``. A row is rewritten only if
  one of its business columns changed, so re-running the pipeline on the same
  data inserts nothing and updates nothing.
* ``ecommerce.rejected_orders``: it is an audit snapshot of one source file,
  therefore the rows of that file are replaced inside the same transaction.
* The whole load runs in a single transaction: it is applied entirely or not
  at all.

Orders that exist in the database but have disappeared from a later source
file are *not* deleted (documented limitation).
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg2
from psycopg2 import OperationalError
from psycopg2.extensions import connection as pg_connection

from src.config import Settings
from src.logger import get_logger
from src.transform import CLEAN_COLUMNS, REJECTED_META_COLUMNS
from src.validation import REQUIRED_COLUMNS

logger = get_logger("load")

BRONZE_FILENAME = "orders_bronze.csv"
SILVER_FILENAME = "orders_clean.csv"
REPORT_FILENAME = "data_quality_report.json"
REJECTED_FILENAME = "rejected_rows.csv"

_NUMERIC_TOLERANCE = 0.011


class DatabaseError(RuntimeError):
    """Raised when PostgreSQL is unreachable or a load/reconciliation fails."""


# ---------------------------------------------------------------------------
# File exports
# ---------------------------------------------------------------------------
def export_bronze(bronze: pd.DataFrame, raw_dir: Path) -> Path:
    """Write the Bronze snapshot (raw values + ingestion metadata)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / BRONZE_FILENAME
    bronze.to_csv(path, index=False, encoding="utf-8")
    logger.info("Bronze exported: %s (%d rows)", path, len(bronze))
    return path


def export_silver(clean: pd.DataFrame, report: dict[str, Any], processed_dir: Path) -> list[Path]:
    """Write the cleaned orders and the data quality report."""
    processed_dir.mkdir(parents=True, exist_ok=True)
    csv_path = processed_dir / SILVER_FILENAME
    clean.to_csv(
        csv_path, index=False, encoding="utf-8", date_format="%Y-%m-%d", float_format="%.2f"
    )
    report_path = processed_dir / REPORT_FILENAME
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    logger.info("Silver exported: %s (%d rows)", csv_path, len(clean))
    return [csv_path, report_path]


def export_rejected(rejected: pd.DataFrame, rejected_dir: Path) -> Path:
    """Write the rejected rows with their reason codes."""
    rejected_dir.mkdir(parents=True, exist_ok=True)
    path = rejected_dir / REJECTED_FILENAME
    rejected.to_csv(path, index=False, encoding="utf-8")
    logger.info("Rejected rows exported: %s (%d rows)", path, len(rejected))
    return path


def export_gold(gold: dict[str, pd.DataFrame], gold_dir: Path) -> list[Path]:
    """Write one BI-ready CSV per Gold table.

    Files use UTF-8 with a BOM (``utf-8-sig``) so that Power BI and Excel detect
    accented characters correctly.
    """
    gold_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, table in gold.items():
        path = gold_dir / f"{name}.csv"
        table.to_csv(path, index=False, encoding="utf-8-sig", float_format="%.2f")
        paths.append(path)
    logger.info("Gold exported: %d files in %s", len(paths), gold_dir)
    return paths


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
def wait_for_db(
    settings: Settings,
    retries: int | None = None,
    delay: float | None = None,
    connect: Callable[..., pg_connection] = psycopg2.connect,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Block until PostgreSQL accepts connections.

    Args:
        settings: Connection settings (also provides the default retry policy).
        retries: Number of attempts (default ``settings.db_connect_retries``).
        delay: Seconds between attempts (default ``settings.db_connect_delay_seconds``).
        connect: Connection factory (replaceable in tests).
        sleep: Sleep function (replaceable in tests).

    Raises:
        DatabaseError: If the database is still unreachable after all attempts.
    """
    retries = settings.db_connect_retries if retries is None else retries
    delay = settings.db_connect_delay_seconds if delay is None else delay
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            connect(connect_timeout=5, **settings.postgres_connection_kwargs()).close()
            logger.info("PostgreSQL is ready (attempt %d)", attempt)
            return
        except OperationalError as exc:
            last_error = exc
            logger.warning("PostgreSQL not ready (attempt %d/%d)", attempt, retries)
            sleep(delay)
    raise DatabaseError(
        f"PostgreSQL unreachable at {settings.postgres_host}:{settings.postgres_port} "
        f"after {retries} attempts: {str(last_error).strip()}"
    )


def apply_sql_files(conn: pg_connection, sql_dir: Path) -> None:
    """Execute ``schema.sql`` then ``analytics.sql`` (both idempotent)."""
    for filename in ("schema.sql", "analytics.sql"):
        path = sql_dir / filename
        if not path.is_file():
            raise DatabaseError(f"SQL file not found: {path}")
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
        logger.info("SQL applied: %s", filename)


def _to_csv_buffer(frame: pd.DataFrame) -> io.StringIO:
    """Serialise a DataFrame to an in-memory CSV (no header) for ``COPY``."""
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False, header=False, date_format="%Y-%m-%d", float_format="%.2f")
    buffer.seek(0)
    return buffer


def upsert_orders(conn: pg_connection, clean: pd.DataFrame, run_id: str) -> tuple[int, int]:
    """Merge validated orders into ``ecommerce.orders``.

    Returns:
        ``(inserted, updated)``. Unchanged rows are neither inserted nor updated.
    """
    if clean.empty:
        return 0, 0
    columns = list(CLEAN_COLUMNS)
    column_list = ", ".join(columns)
    business = [c for c in columns if c != "order_id"]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in business)
    target_tuple = ", ".join(f"ecommerce.orders.{c}" for c in business)
    excluded_tuple = ", ".join(f"EXCLUDED.{c}" for c in business)

    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE orders_staging "
            "(LIKE ecommerce.orders INCLUDING DEFAULTS) ON COMMIT DROP"
        )
        cur.copy_expert(
            f"COPY orders_staging ({column_list}, run_id) FROM STDIN WITH (FORMAT csv)",
            _to_csv_buffer(clean.assign(run_id=run_id)),
        )
        cur.execute(
            f"""
            INSERT INTO ecommerce.orders ({column_list}, run_id)
            SELECT {column_list}, run_id FROM orders_staging
            ON CONFLICT (order_id) DO UPDATE
               SET {set_clause}, run_id = EXCLUDED.run_id, loaded_at = now()
             WHERE ({target_tuple}) IS DISTINCT FROM ({excluded_tuple})
            RETURNING (xmax = 0) AS inserted
            """
        )
        flags = [row[0] for row in cur.fetchall()]
    inserted = sum(flags)
    return inserted, len(flags) - inserted


def replace_rejected(conn: pg_connection, rejected: pd.DataFrame, source_file: str) -> int:
    """Replace the audit snapshot of rejected rows for ``source_file``."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ecommerce.rejected_orders WHERE source_file = %s", (source_file,))
        if rejected.empty:
            return 0
        columns = [*REJECTED_META_COLUMNS, *REQUIRED_COLUMNS]
        cur.copy_expert(
            f"COPY ecommerce.rejected_orders ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv)",
            _to_csv_buffer(rejected[columns]),
        )
    return len(rejected)


def record_pipeline_run(
    conn: pg_connection,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    counts: dict[str, int],
) -> None:
    """Store the execution summary in ``ecommerce.pipeline_runs``."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ecommerce.pipeline_runs
                (run_id, started_at, finished_at, status,
                 rows_raw, rows_clean, rows_rejected, rows_inserted, rows_updated)
            VALUES (%s, %s, %s, 'SUCCESS', %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (
                run_id,
                started_at,
                finished_at,
                counts["rows_raw"],
                counts["rows_clean"],
                counts["rows_rejected"],
                counts["rows_inserted"],
                counts["rows_updated"],
            ),
        )


# ---------------------------------------------------------------------------
# Reconciliation between pandas (Gold CSV) and SQL views
# ---------------------------------------------------------------------------
_RECONCILIATION_CHECKS: tuple[tuple[str, str, str | None], ...] = (
    ("kpi_summary", "v_kpi_summary", None),
    ("monthly_sales", "v_monthly_sales", "month"),
    ("orders_trend", "v_orders_trend", "month"),
    ("sales_by_region", "v_sales_by_region", "region"),
    ("sales_by_category", "v_sales_by_category", "category"),
    ("top_products", "v_top_products", "rank"),
    ("payment_methods", "v_payment_methods", "payment_method"),
)


def _query_view(conn: pg_connection, view: str) -> pd.DataFrame:
    """Read a whole analytical view into a DataFrame."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ecommerce.{view}")
        columns = [description[0] for description in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)


def _values_match(left: Any, right: Any) -> bool:
    """Compare two scalars: numerically (with tolerance) or as text."""
    left_null, right_null = bool(pd.isna(left)), bool(pd.isna(right))
    if left_null or right_null:
        return left_null and right_null
    try:
        return abs(float(left) - float(right)) <= _NUMERIC_TOLERANCE
    except (TypeError, ValueError):
        return str(left) == str(right)


def compare_tables(
    name: str, expected: pd.DataFrame, actual: pd.DataFrame, key: str | None
) -> list[str]:
    """Compare a pandas Gold table with its SQL view; return the differences."""
    problems: list[str] = []
    if key is None:
        expected_rows = expected.reset_index(drop=True)
        actual_rows = actual.reset_index(drop=True)
    else:
        expected_rows = expected.assign(**{key: expected[key].astype(str)}).set_index(key)
        actual_rows = actual.assign(**{key: actual[key].astype(str)}).set_index(key)
    if set(expected_rows.index) != set(actual_rows.index):
        return [f"{name}: row keys differ between pandas and SQL"]
    for column in expected_rows.columns:
        if column not in actual_rows.columns:
            problems.append(f"{name}: column {column!r} missing in SQL view")
            continue
        for index in expected_rows.index:
            if not _values_match(expected_rows.at[index, column], actual_rows.at[index, column]):
                problems.append(
                    f"{name}[{index}].{column}: pandas={expected_rows.at[index, column]!r} "
                    f"sql={actual_rows.at[index, column]!r}"
                )
    return problems


def reconcile_with_gold(conn: pg_connection, gold: dict[str, pd.DataFrame]) -> list[str]:
    """Check that the SQL views return the same figures as the Gold tables.

    Returns:
        A list of human-readable differences (empty when everything matches).
    """
    problems: list[str] = []
    for table_name, view, key in _RECONCILIATION_CHECKS:
        problems.extend(compare_tables(table_name, gold[table_name], _query_view(conn, view), key))
    return problems


# ---------------------------------------------------------------------------
# Orchestration of the database stage
# ---------------------------------------------------------------------------
def load_to_postgres(
    settings: Settings,
    clean: pd.DataFrame,
    rejected: pd.DataFrame,
    gold: dict[str, pd.DataFrame],
    run_id: str,
    source_file: str,
    started_at: datetime,
    rows_raw: int,
) -> dict[str, Any]:
    """Load Silver data into PostgreSQL and reconcile it with the Gold tables.

    Raises:
        DatabaseError: If PostgreSQL is unreachable, a statement fails, or the
            SQL views disagree with the Gold tables.
    """
    wait_for_db(settings)
    try:
        conn = psycopg2.connect(**settings.postgres_connection_kwargs())
    except OperationalError as exc:
        raise DatabaseError(f"Cannot connect to PostgreSQL: {str(exc).strip()}") from exc

    try:
        with conn:  # single transaction: commit on success, rollback on error
            apply_sql_files(conn, settings.sql_dir)
            inserted, updated = upsert_orders(conn, clean, run_id)
            rejected_stored = replace_rejected(conn, rejected, source_file)
            record_pipeline_run(
                conn,
                run_id,
                started_at,
                datetime.now(started_at.tzinfo),
                {
                    "rows_raw": rows_raw,
                    "rows_clean": len(clean),
                    "rows_rejected": len(rejected),
                    "rows_inserted": inserted,
                    "rows_updated": updated,
                },
            )
        problems = reconcile_with_gold(conn, gold)
    except psycopg2.Error as exc:
        raise DatabaseError(f"PostgreSQL load failed: {str(exc).strip()}") from exc
    finally:
        conn.close()

    if problems:
        for problem in problems[:20]:
            logger.error("Reconciliation mismatch: %s", problem)
        raise DatabaseError(f"SQL views disagree with Gold tables ({len(problems)} differences)")

    unchanged = len(clean) - inserted - updated
    logger.info(
        "PostgreSQL: %d inserted, %d updated, %d unchanged, %d rejected rows stored; "
        "SQL views reconciled with Gold",
        inserted,
        updated,
        unchanged,
        rejected_stored,
    )
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_unchanged": unchanged,
        "rejected_rows_stored": rejected_stored,
        "reconciliation": "OK",
    }
