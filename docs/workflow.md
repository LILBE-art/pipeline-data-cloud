# Pipeline workflow

## Stages of one run (`python -m src.main`)

| # | Stage | Module | Output |
|---|-------|--------|--------|
| 1 | Load configuration, start logging | `config`, `logger` | `logs/pipeline.log` |
| 2 | Ensure the raw file exists (generate the synthetic dataset if absent) | `extract` | `data/raw/orders_raw.csv`, `orders_raw.manifest.json` |
| 3 | Read raw as text, build Bronze | `extract`, `load` | `data/raw/orders_bronze.csv` |
| 4 | Validate schema, clean, deduplicate, reject | `validation`, `transform` | `data/processed/orders_clean.csv`, `data_quality_report.json`, `data/rejected/rejected_rows.csv` |
| 5 | Compute Gold KPIs | `metrics`, `load` | `data/gold/*.csv` (7 files) |
| 6 | *(optional)* Load PostgreSQL and reconcile with Gold | `load` | tables and views in schema `ecommerce` |
| 7 | *(optional)* Upload Bronze/Silver/rejected/Gold to S3 | `s3_storage` | objects under `<prefix>/<layer>/run_date=…` |

The synthetic raw file is generated **only once**. Later runs reuse it, which keeps raw data immutable. Use
`--regenerate` to rebuild it (same seed → same content).

## Commands

```bash
# one-time setup
make install && cp .env.example .env     # edit POSTGRES_PASSWORD

# files only
make run

# local Python + PostgreSQL in Docker
make db-up && make run-db

# everything in Docker
make docker-run

# options
.venv/bin/python -m src.main --skip-db            # ignore LOAD_TO_POSTGRES=true
.venv/bin/python -m src.main --load-db            # load PostgreSQL
.venv/bin/python -m src.main --s3                 # also upload to S3
.venv/bin/python -m src.main --regenerate         # rebuild the synthetic raw file
```

## Environment variables

See `.env.example`. Key ones: `RANDOM_SEED`, `NUM_ORDERS` (≥ 1000), `LOAD_TO_POSTGRES`, `POSTGRES_*`,
`DB_CONNECT_RETRIES`, `DB_CONNECT_DELAY_SECONDS`, `S3_ENABLED`, `S3_BUCKET`, `S3_PREFIX`, `AWS_REGION`,
`DATA_DIR`, `LOG_LEVEL`. Real environment variables always override `.env`.

## Using a different source file

Replace `data/raw/orders_raw.csv` with a CSV that has the 11 required columns (extra columns are ignored),
then run the pipeline. Adapt the date convention (`DD/MM/YYYY`) and business ranges in `src/validation.py`
if your source differs. `orders_raw.manifest.json` then no longer describes your file and can be deleted.

## Inspecting results

```bash
cat data/processed/data_quality_report.json
head data/rejected/rejected_rows.csv
make psql        # then:  SELECT * FROM ecommerce.v_kpi_summary;
                 #        SELECT * FROM ecommerce.v_rejections_by_reason;
                 #        SELECT * FROM ecommerce.pipeline_runs ORDER BY started_at DESC;
```

## Persistence and reset

* Files: `./data` and `./logs` on the host (git-ignored). `make clean` removes generated files.
* PostgreSQL in Docker: named volume `pgdata`. `make docker-down` keeps it; `make docker-reset` deletes it.
* `sql/schema.sql` and `sql/analytics.sql` are idempotent and re-applied at every load; the copy mounted in
  `docker-entrypoint-initdb.d` only runs when the volume is first created.

## Idempotence check

Run `make run-db` twice: the second log line reads `0 inserted, 0 updated, N unchanged`, and
`SELECT count(*) FROM ecommerce.orders` does not change.

## Exit codes

`0` success · `2` configuration error · `3` input data/schema error · `4` PostgreSQL or S3 error.
