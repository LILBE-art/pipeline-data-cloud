# Data directory

All files below are **generated locally** by the pipeline and are ignored by Git
(`.gitignore` keeps only this file). Run `make run` to recreate them.

| Folder | Layer | Content |
|--------|-------|---------|
| `raw/` | Bronze | `orders_raw.csv` (synthetic source, never modified by the pipeline), `orders_raw.manifest.json` (ground truth of the injected anomalies), `orders_bronze.csv` (raw values + ingestion metadata) |
| `processed/` | Silver | `orders_clean.csv`, `data_quality_report.json` |
| `rejected/` | Silver (audit) | `rejected_rows.csv` with one reason code per rejected row |
| `gold/` | Gold | BI-ready CSV files (UTF-8 with BOM) |

The dataset is **100% synthetic** (fixed seed, reproducible). No real person, product or
transaction is involved.
