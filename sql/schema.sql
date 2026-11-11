-- =============================================================================
-- Schema version 1.0.0 - e-commerce sales (Silver layer in PostgreSQL)
-- The script is idempotent: it can be executed on every pipeline run.
-- Changes must be added as a new version row (and documented in the README).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS ecommerce;

CREATE TABLE IF NOT EXISTS ecommerce.schema_version (
    version     TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO ecommerce.schema_version (version, description)
VALUES ('1.0.0', 'Initial schema: orders, rejected_orders, pipeline_runs')
ON CONFLICT (version) DO NOTHING;

-- Validated orders (one row per order_id).
CREATE TABLE IF NOT EXISTS ecommerce.orders (
    order_id       TEXT          PRIMARY KEY,
    customer_id    TEXT          NOT NULL,
    order_date     DATE          NOT NULL,
    category       TEXT          NOT NULL,
    product_name   TEXT          NOT NULL,
    quantity       INTEGER       NOT NULL CHECK (quantity BETWEEN 1 AND 100),
    unit_price     NUMERIC(10,2) NOT NULL CHECK (unit_price BETWEEN 0.01 AND 10000.00),
    total_amount   NUMERIC(12,2) NOT NULL,
    payment_method TEXT          NOT NULL,
    city           TEXT          NOT NULL,
    region         TEXT          NOT NULL,
    run_id         TEXT          NOT NULL,
    loaded_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT orders_total_consistent
        CHECK (total_amount = round(quantity * unit_price, 2))
);

CREATE INDEX IF NOT EXISTS idx_orders_order_date     ON ecommerce.orders (order_date);
CREATE INDEX IF NOT EXISTS idx_orders_category       ON ecommerce.orders (category);
CREATE INDEX IF NOT EXISTS idx_orders_region         ON ecommerce.orders (region);
CREATE INDEX IF NOT EXISTS idx_orders_product_name   ON ecommerce.orders (product_name);
CREATE INDEX IF NOT EXISTS idx_orders_payment_method ON ecommerce.orders (payment_method);
CREATE INDEX IF NOT EXISTS idx_orders_customer_id    ON ecommerce.orders (customer_id);

-- Rejected rows, kept with their original raw values for auditing.
-- Raw columns are TEXT and nullable: an empty raw value is stored as NULL.
CREATE TABLE IF NOT EXISTS ecommerce.rejected_orders (
    source_file    TEXT        NOT NULL,
    source_row     INTEGER     NOT NULL,
    run_id         TEXT        NOT NULL,
    reject_reason  TEXT        NOT NULL,
    order_id       TEXT,
    customer_id    TEXT,
    order_date     TEXT,
    category       TEXT,
    product_name   TEXT,
    quantity       TEXT,
    unit_price     TEXT,
    total_amount   TEXT,
    payment_method TEXT,
    city           TEXT,
    region         TEXT,
    rejected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_file, source_row)
);

CREATE INDEX IF NOT EXISTS idx_rejected_reason ON ecommerce.rejected_orders (reject_reason);

-- One row per pipeline execution.
CREATE TABLE IF NOT EXISTS ecommerce.pipeline_runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ NOT NULL,
    status        TEXT        NOT NULL CHECK (status IN ('SUCCESS', 'FAILED')),
    rows_raw      INTEGER     NOT NULL,
    rows_clean    INTEGER     NOT NULL,
    rows_rejected INTEGER     NOT NULL,
    rows_inserted INTEGER     NOT NULL,
    rows_updated  INTEGER     NOT NULL
);
