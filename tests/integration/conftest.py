import os
import uuid
from urllib.parse import urlparse

import boto3
import pytest
from botocore.config import Config

from snapshotkit.store import S3Store


@pytest.fixture(scope="session")
def s3_settings():
    return {
        "endpoint_url": os.environ.get("SNAPSHOT_TEST_S3_ENDPOINT", "http://localhost:9000"),
        "access_key": os.environ.get("SNAPSHOT_TEST_S3_ACCESS_KEY", "minioadmin"),
        "secret_key": os.environ.get("SNAPSHOT_TEST_S3_SECRET_KEY", "minioadmin"),
    }


@pytest.fixture(scope="session")
def s3_bucket(s3_settings):
    """Fresh uniquely named bucket for the session; emptied and deleted afterwards."""
    client = boto3.client(
        "s3",
        endpoint_url=s3_settings["endpoint_url"],
        aws_access_key_id=s3_settings["access_key"],
        aws_secret_access_key=s3_settings["secret_key"],
        region_name="us-east-1",
        config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1}),
    )
    bucket = f"snapshot-test-{uuid.uuid4().hex[:12]}"
    import time

    deadline = time.monotonic() + 30
    try:
        while True:
            try:
                client.create_bucket(Bucket=bucket)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
    except Exception as e:
        host = urlparse(s3_settings["endpoint_url"]).netloc
        msg = f"S3 endpoint {host} unreachable: {e}"
        if os.environ.get("SNAPSHOT_TEST_REQUIRE_S3") == "1":
            pytest.fail(msg)
        pytest.skip(msg)
    yield bucket
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=obj["Key"])
    client.delete_bucket(Bucket=bucket)


@pytest.fixture
def s3_store(s3_settings, s3_bucket):
    """S3Store scoped to a clean per-test prefix is not possible via the API,
    so tests use unique keys; the store itself covers the whole test bucket."""
    return S3Store(
        endpoint_url=s3_settings["endpoint_url"],
        bucket=s3_bucket,
        access_key=s3_settings["access_key"],
        secret_key=s3_settings["secret_key"],
    )
