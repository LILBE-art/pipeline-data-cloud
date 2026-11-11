# Migrating from local files to AWS S3

> The S3 module is covered by automated tests using **moto mocks**. It has **not** been run against a real AWS
> account by the author of this repository: follow the verification steps at the end.

## What moves to S3

| Local folder | S3 layer | Objects |
|--------------|----------|---------|
| `data/raw/` | `bronze` | `orders_raw.csv`, `orders_bronze.csv` |
| `data/processed/` | `silver` | `orders_clean.csv`, `data_quality_report.json` |
| `data/rejected/` | `rejected` | `rejected_rows.csv` |
| `data/gold/` | `gold` | 7 CSV files |

Key format: `<S3_PREFIX>/<layer>/run_date=<YYYY-MM-DD>/<filename>`. Two runs on the same day overwrite the same
keys (idempotent). The Hive-style `run_date=` partition lets Athena/Glue discover partitions later.
PostgreSQL stays local; this step only archives and shares the files.

## Prerequisites

1. An AWS account with **budget alerts** configured (Billing → Budgets, e.g. 1 EUR).
2. AWS CLI v2 installed on Debian:
   ```bash
   curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
   unzip awscliv2.zip && sudo ./aws/install
   aws --version
   ```
3. Credentials for a **dedicated IAM user or SSO role** (never the root account):
   `aws configure` or `aws configure sso`, then `aws sts get-caller-identity` to confirm.

## Create a private bucket

```bash
export BUCKET=your-unique-pipeline-bucket-name     # globally unique, lowercase
aws s3api create-bucket --bucket "$BUCKET" --region eu-west-3 \
    --create-bucket-configuration LocationConstraint=eu-west-3
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

## Minimal IAM policy

Attach to the user/role used by the pipeline (replace the bucket name):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::your-unique-pipeline-bucket-name"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::your-unique-pipeline-bucket-name/ecommerce-pipeline/*"
    }
  ]
}
```

`s3:ListBucket` is needed because the access check uses `HeadBucket`. No delete or admin right is granted.

## Configure and run

```bash
# .env
S3_ENABLED=true
S3_BUCKET=your-unique-pipeline-bucket-name
S3_PREFIX=ecommerce-pipeline
AWS_REGION=eu-west-3

.venv/bin/python -m src.main --skip-db          # uploads because S3_ENABLED=true
# or force it for one run:
.venv/bin/python -m src.main --skip-db --s3
```

Inside Docker, mount your AWS config read-only or pass `AWS_PROFILE`; **do not** bake credentials in the image.

## Verify

```bash
aws s3 ls "s3://$BUCKET/ecommerce-pipeline/" --recursive
aws s3api head-object --bucket "$BUCKET" --key ecommerce-pipeline/gold/run_date=$(date +%F)/kpi_summary.csv
# expect "ServerSideEncryption": "AES256"
```

Common errors are translated by the module: no credentials (`aws configure`), bucket not found or wrong
region, access denied (check the policy), expired token (log in again), network failure.

## Costs and precautions

* S3 Standard storage is billed per GB-month plus request charges; this project stores a few megabytes, so
  the cost is typically negligible, but **check current pricing** at <https://aws.amazon.com/s3/pricing/> and the
  free tier eligibility of your account. Data transfer out to the internet is billed.
* Set a budget alert before the first upload, and a lifecycle rule to expire old runs, e.g.:
  ```bash
  aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
    '{"Rules":[{"ID":"expire-old-runs","Status":"Enabled","Filter":{"Prefix":"ecommerce-pipeline/"},"Expiration":{"Days":30}}]}'
  ```
* Keep the bucket private and encrypted; never commit credentials. Prefer SSO/short-lived credentials.
* **Cleanup** when finished: `aws s3 rm "s3://$BUCKET" --recursive && aws s3api delete-bucket --bucket "$BUCKET"`.
* The data are synthetic. With real customer data, apply GDPR requirements (EU region, retention, access logging).

## Next step toward a cloud analytics stack

S3 (`gold/` partitions) → AWS Glue crawler → Amazon Athena (SQL) → BI tool. These services are billed
(Athena per data scanned) and are **not** implemented here; see the roadmap in the README.
