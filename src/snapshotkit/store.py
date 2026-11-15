"""Snapshot object-store layout and backends."""
import os
import re
from collections.abc import Iterator
from typing import Protocol

from snapshotkit.digests import digest_key

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_WHITESPACE_RE = re.compile(r"\s")


def validate_project(name: str) -> str:
    if not PROJECT_RE.match(name) or name.endswith("\n"):
        raise ValueError(f"invalid project name: {name!r}")
    return name


def snapshot_key(project: str, digest: str) -> str:
    return f"snapshots/{validate_project(project)}/{digest_key(digest)}.tar.gz"


def manifest_key(project: str, digest: str) -> str:
    return f"snapshots/{validate_project(project)}/{digest_key(digest)}.json"


def digest_index_key(digest: str) -> str:
    return f"index/digest/{digest_key(digest)}.json"


def _check_path(value: str, what: str, allow_slash: bool) -> str:
    segments = value.split("/")
    if (
        not value
        or value.startswith("/")
        or ".." in value
        or _WHITESPACE_RE.search(value)
        or any(s == "" for s in segments)
        or (not allow_slash and len(segments) > 1)
    ):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def tag_index_key(image_repository: str, tag: str) -> str:
    repo = _check_path(image_repository, "image repository", allow_slash=True)
    tag = _check_path(tag, "image tag", allow_slash=False)
    return f"index/tag/{repo}/{tag}.json"


class SnapshotStore(Protocol):
    def put_new(self, key: str, data: bytes) -> bool:
        """Write only if the key is absent. Returns False if it exists."""

    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes | None: ...

    def list_keys(self, prefix: str) -> Iterator[str]: ...

    def delete(self, key: str) -> None: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put_new(self, key: str, data: bytes) -> bool:
        if key in self._objects:
            return False
        self._objects[key] = data
        return True

    def put(self, key: str, data: bytes) -> None:
        self._objects[key] = data

    def get(self, key: str) -> bytes | None:
        return self._objects.get(key)

    def list_keys(self, prefix: str) -> Iterator[str]:
        return iter([k for k in self._objects if k.startswith(prefix)])

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)


class S3Store:
    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        import boto3

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    @classmethod
    def from_env(cls) -> "S3Store":
        return cls(
            endpoint_url=os.environ["SNAPSHOT_STORE_ENDPOINT"],
            bucket=os.environ.get("SNAPSHOT_STORE_BUCKET", "code-snapshots"),
            access_key=os.environ["SNAPSHOT_STORE_ACCESS_KEY"],
            secret_key=os.environ["SNAPSHOT_STORE_SECRET_KEY"],
        )

    def put_new(self, key: str, data: bytes) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, IfNoneMatch="*"
            )
        except ClientError as e:
            if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412:
                return False
            raise
        return True

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchKey":
                return None
            raise

    def list_keys(self, prefix: str) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)
