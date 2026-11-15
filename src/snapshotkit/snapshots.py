"""Fetch snapshots from the store into an atomically-populated local cache."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import tarfile
import tempfile
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path

from snapshotkit.digests import DIGEST_RE, digest_key
from snapshotkit.store import (
    SnapshotStore,
    digest_index_key,
    manifest_key,
    snapshot_key,
    tag_index_key,
    validate_project,
)
from snapshotkit.tarball import UnsafeArchiveError, safe_extract

_ID_RE = re.compile(r"^sha256-[0-9a-f]{64}$")


class SnapshotNotFound(Exception):
    def __init__(self) -> None:
        super().__init__("No snapshot available for this version.")


@dataclass
class SnapshotInfo:
    snapshot_id: str
    path: Path
    project: str
    manifest: dict


class SnapshotCache:
    def __init__(self, store: SnapshotStore, cache_root: Path) -> None:
        self._store = store
        self._root = Path(cache_root)

    def _index_key(self, ref: str) -> str:
        if DIGEST_RE.match(ref):
            return digest_index_key(ref)
        slash = ref.rfind("/")
        colon = ref.rfind(":")
        if colon <= slash or colon <= 0 or colon == len(ref) - 1:
            raise SnapshotNotFound
        try:
            return tag_index_key(ref[:colon], ref[colon + 1 :])
        except ValueError:
            raise SnapshotNotFound from None

    def _load_json(self, key: str) -> dict:
        raw = self._store.get(key)
        if raw is None:
            raise SnapshotNotFound
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise SnapshotNotFound from None
        if not isinstance(body, dict):
            raise SnapshotNotFound
        return body

    def get_snapshot(self, ref: str) -> SnapshotInfo:
        index = self._load_json(self._index_key(ref))
        digest, project = index.get("snapshot_digest"), index.get("project")
        if not isinstance(digest, str) or not DIGEST_RE.match(digest):
            raise SnapshotNotFound
        if not isinstance(project, str):
            raise SnapshotNotFound
        try:
            validate_project(project)
        except ValueError:
            raise SnapshotNotFound from None
        manifest = self._load_json(manifest_key(project, digest))
        snapshot_id = digest_key(digest)
        dest = self._root / snapshot_id
        if not dest.is_dir():
            self._extract(project, digest, dest)
        return SnapshotInfo(snapshot_id, dest, project, manifest)

    def _extract(self, project: str, digest: str, dest: Path) -> None:
        data = self._store.get(snapshot_key(project, digest))
        if data is None:
            raise SnapshotNotFound
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._root / f"{dest.name}.tmp-{uuid.uuid4().hex}"
        tmp.mkdir()
        try:
            with tempfile.NamedTemporaryFile(
                dir=self._root, suffix=".tmp-tar", delete=False
            ) as f:
                f.write(data)
                tar_path = Path(f.name)
            try:
                safe_extract(tar_path, tmp)
            finally:
                tar_path.unlink(missing_ok=True)
            try:
                os.rename(tmp, dest)
            except OSError:
                if not dest.is_dir():
                    raise
                shutil.rmtree(tmp, ignore_errors=True)
        except (
            UnsafeArchiveError,
            tarfile.ReadError,
            EOFError,
            gzip.BadGzipFile,
            zlib.error,
        ):
            shutil.rmtree(tmp, ignore_errors=True)
            raise SnapshotNotFound from None
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def path_for(self, snapshot_id: str) -> Path:
        if not isinstance(snapshot_id, str) or not _ID_RE.fullmatch(snapshot_id):
            raise SnapshotNotFound
        path = self._root / snapshot_id
        if not path.is_dir():
            raise SnapshotNotFound
        return path
