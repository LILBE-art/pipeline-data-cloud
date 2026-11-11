"""AWS S3 integration, tested with moto (no AWS account, no network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from moto import mock_aws

from src.s3_storage import S3Storage, S3UploadError

BUCKET = "test-pipeline-bucket"
RUN_DATE = date(2026, 3, 14)


@pytest.fixture(autouse=True)
def _fake_aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy credentials so that nothing can ever reach a real AWS account."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def s3_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    path = tmp_path / "kpi_summary.csv"
    path.write_text("total_revenue\n100.00\n", encoding="utf-8")
    return path


def client_error(code: str, operation: str = "PutObject") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, operation)


def test_build_key_layout() -> None:
    storage = S3Storage(BUCKET, prefix="/proj/", client=MagicMock())
    assert storage.build_key("gold", RUN_DATE, "kpi.csv") == "proj/gold/run_date=2026-03-14/kpi.csv"
    assert S3Storage(BUCKET, prefix="", client=MagicMock()).build_key(
        "bronze", RUN_DATE, "a.csv"
    ) == ("bronze/run_date=2026-03-14/a.csv")


def test_build_key_rejects_unknown_layer() -> None:
    with pytest.raises(ValueError, match="Unknown layer"):
        S3Storage(BUCKET, client=MagicMock()).build_key("platinum", RUN_DATE, "a.csv")


def test_upload_file_stores_encrypted_object(s3_bucket, sample_file: Path) -> None:
    storage = S3Storage(BUCKET, prefix="proj")
    key = storage.upload_file(sample_file, "gold", RUN_DATE)
    assert key == "proj/gold/run_date=2026-03-14/kpi_summary.csv"
    head = s3_bucket.head_object(Bucket=BUCKET, Key=key)
    assert head["ServerSideEncryption"] == "AES256"
    body = s3_bucket.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
    assert body == "total_revenue\n100.00\n"


def test_upload_is_idempotent_for_the_same_day(s3_bucket, sample_file: Path) -> None:
    storage = S3Storage(BUCKET, prefix="proj")
    storage.upload_file(sample_file, "gold", RUN_DATE)
    storage.upload_file(sample_file, "gold", RUN_DATE)
    listing = s3_bucket.list_objects_v2(Bucket=BUCKET)
    assert listing["KeyCount"] == 1


def test_upload_layers_organises_objects_by_layer(s3_bucket, tmp_path: Path) -> None:
    files = {}
    for layer in ("bronze", "silver", "rejected", "gold"):
        path = tmp_path / f"{layer}.csv"
        path.write_text("x\n1\n", encoding="utf-8")
        files[layer] = [path]
    keys = S3Storage(BUCKET, prefix="p").upload_layers(files, RUN_DATE)
    assert sorted(keys) == sorted(f"p/{layer}/run_date=2026-03-14/{layer}.csv" for layer in files)


def test_missing_bucket_gives_actionable_error(sample_file: Path) -> None:
    with mock_aws():
        storage = S3Storage("bucket-that-does-not-exist")
        with pytest.raises(S3UploadError, match="not found|does not exist"):
            storage.upload_layers({"gold": [sample_file]}, RUN_DATE)


def test_missing_local_file_is_reported(s3_bucket, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="missing file"):
        S3Storage(BUCKET).upload_file(tmp_path / "nope.csv", "gold", RUN_DATE)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (client_error("AccessDenied"), "Access denied"),
        (client_error("403"), "Access denied"),
        (client_error("NoSuchBucket"), "does not exist"),
        (client_error("ExpiredToken"), "expired"),
        (client_error("InvalidAccessKeyId"), "access key is invalid"),
        (client_error("SomethingElse"), "SomethingElse"),
    ],
)
def test_permission_and_client_errors_are_translated(
    error, expected: str, sample_file: Path
) -> None:
    client = MagicMock()
    client.upload_file.side_effect = error
    with pytest.raises(S3UploadError, match=expected):
        S3Storage(BUCKET, client=client).upload_file(sample_file, "gold", RUN_DATE)


def test_missing_credentials_are_reported_without_leaking_secrets(sample_file: Path) -> None:
    client = MagicMock()
    client.head_bucket.side_effect = NoCredentialsError()
    with pytest.raises(S3UploadError, match="aws configure"):
        S3Storage(BUCKET, client=client).upload_layers({"gold": [sample_file]}, RUN_DATE)
    client.upload_file.assert_not_called()  # nothing is sent when the access check fails


def test_network_error_is_reported(sample_file: Path) -> None:
    client = MagicMock()
    client.upload_file.side_effect = EndpointConnectionError(endpoint_url="https://s3.example")
    with pytest.raises(S3UploadError, match="Network error"):
        S3Storage(BUCKET, client=client).upload_file(sample_file, "gold", RUN_DATE)
    client.head_bucket.side_effect = EndpointConnectionError(endpoint_url="https://s3.example")
    with pytest.raises(S3UploadError, match="Network error"):
        S3Storage(BUCKET, client=client).check_access()
