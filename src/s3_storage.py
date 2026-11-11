"""Optional AWS S3 integration (disabled by default).

Credentials are never read or stored by this project: boto3 resolves them
through the standard AWS chain (environment variables, ``~/.aws`` profile,
IAM role, SSO...).

Object layout inside the bucket::

    <prefix>/<layer>/run_date=<YYYY-MM-DD>/<filename>

where ``layer`` is ``bronze``, ``silver``, ``rejected`` or ``gold``.
Re-running the pipeline on the same day overwrites the same keys, so uploads
are idempotent. Objects are encrypted at rest with SSE-S3 (``AES256``).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)

from src.logger import get_logger

logger = get_logger("s3")

VALID_LAYERS = frozenset({"bronze", "silver", "rejected", "gold"})


class S3UploadError(RuntimeError):
    """Raised when an upload cannot be completed (with an actionable message)."""


class S3Storage:
    """Upload pipeline files to an S3 bucket."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "ecommerce-pipeline",
        region: str | None = None,
        client: Any | None = None,
    ) -> None:
        """Create the uploader.

        Args:
            bucket: Target bucket name (must already exist).
            prefix: Key prefix shared by every uploaded object.
            region: Optional AWS region.
            client: Pre-built S3 client (used by tests); a default boto3 client otherwise.
        """
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client or boto3.client("s3", region_name=region)

    def build_key(self, layer: str, run_date: date, filename: str) -> str:
        """Return the object key for a file of a given layer and run date."""
        if layer not in VALID_LAYERS:
            raise ValueError(f"Unknown layer {layer!r}; expected one of {sorted(VALID_LAYERS)}")
        parts = [self.prefix, layer, f"run_date={run_date.isoformat()}", filename]
        return "/".join(part for part in parts if part)

    def check_access(self) -> None:
        """Verify that the bucket exists and is reachable with current credentials.

        Raises:
            S3UploadError: With a message describing the likely cause.
        """
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except NoCredentialsError as exc:
            raise S3UploadError(
                "No AWS credentials found. Configure them with 'aws configure' "
                "or 'aws sso login' (nothing is stored in this project)."
            ) from exc
        except EndpointConnectionError as exc:
            raise S3UploadError(f"Network error while reaching S3: {exc}") from exc
        except ClientError as exc:
            raise S3UploadError(self._explain_client_error(exc)) from exc
        except BotoCoreError as exc:
            raise S3UploadError(f"AWS SDK error: {exc}") from exc

    def upload_file(self, path: Path, layer: str, run_date: date) -> str:
        """Upload one file and return its object key.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            S3UploadError: On network, credential or permission problems.
        """
        if not path.is_file():
            raise FileNotFoundError(f"Cannot upload missing file: {path}")
        key = self.build_key(layer, run_date, path.name)
        try:
            self._client.upload_file(
                str(path), self.bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"}
            )
        except NoCredentialsError as exc:
            raise S3UploadError("No AWS credentials found (see docs/aws_migration.md)") from exc
        except EndpointConnectionError as exc:
            raise S3UploadError(f"Network error while uploading {path.name}: {exc}") from exc
        except ClientError as exc:
            raise S3UploadError(self._explain_client_error(exc)) from exc
        except BotoCoreError as exc:
            raise S3UploadError(f"AWS SDK error while uploading {path.name}: {exc}") from exc
        logger.info("Uploaded %s -> s3://%s/%s", path.name, self.bucket, key)
        return key

    def upload_layers(self, layers: dict[str, Iterable[Path]], run_date: date) -> list[str]:
        """Upload several layers after a single access check.

        Args:
            layers: Mapping ``layer name -> files``.
            run_date: Partition date used in the object keys.

        Returns:
            The list of uploaded object keys.
        """
        self.check_access()
        keys: list[str] = []
        for layer, paths in layers.items():
            for path in paths:
                keys.append(self.upload_file(Path(path), layer, run_date))
        logger.info("S3 upload finished: %d objects in s3://%s", len(keys), self.bucket)
        return keys

    def _explain_client_error(self, exc: ClientError) -> str:
        """Translate common S3 error codes into actionable messages."""
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        hints = {
            "404": f"Bucket {self.bucket!r} not found (or wrong region)",
            "NoSuchBucket": f"Bucket {self.bucket!r} does not exist",
            "403": f"Access denied to bucket {self.bucket!r}: check the IAM policy (s3:PutObject)",
            "AccessDenied": f"Access denied to bucket {self.bucket!r}: check the IAM policy",
            "InvalidAccessKeyId": "The AWS access key is invalid",
            "SignatureDoesNotMatch": "AWS credentials are invalid or the clock is out of sync",
            "ExpiredToken": "AWS session token expired: log in again",
            "ExpiredTokenException": "AWS session token expired: log in again",
        }
        return f"S3 error [{code}]: {hints.get(code, str(exc))}"
