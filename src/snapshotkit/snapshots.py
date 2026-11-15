"""Fetch snapshots from the store into an atomically-populated local cache.

The cache is bounded by a byte budget (sum of extracted file sizes) with
least-recently-used eviction, and is only a cache: a snapshot that is not on
disk (evicted, pod restarted, another replica served get_snapshot) is fetched
again on demand, so losing it never makes a published snapshot unavailable.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import uuid
import zlib
from collections import OrderedDict
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

DEFAULT_MAX_BYTES = 4 * 1024**3


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
    def __init__(
        self, store: SnapshotStore, cache_root: Path, max_bytes: int = DEFAULT_MAX_BYTES
    ) -> None:
        self._store = store
        self._root = Path(cache_root)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        # snapshot_id -> extracted bytes, least recently used first
        self._lru: OrderedDict[str, int] = OrderedDict()
        if self._root.is_dir():
            existing = [
                p for p in self._root.iterdir() if _ID_RE.fullmatch(p.name) and p.is_dir()
            ]
            for p in sorted(existing, key=lambda p: p.stat().st_mtime):
                self._lru[p.name] = _tree_size(p)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def _index_key(self, ref: str) -> str:
        if DIGEST_RE.fullmatch(ref):
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
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise SnapshotNotFound
        if not isinstance(project, str):
            raise SnapshotNotFound
        try:
            validate_project(project)
        except ValueError:
            raise SnapshotNotFound from None
        manifest = self._load_json(manifest_key(project, digest))
        snapshot_id = digest_key(digest)
        dest = self._cached(snapshot_id)
        if dest is None:
            dest = self._root / snapshot_id
            self._extract(project, digest, dest)
        return SnapshotInfo(snapshot_id, dest, project, manifest)

    def _cached(self, snapshot_id: str) -> Path | None:
        """The cached dir, marked most recently used; None if not on disk."""
        path = self._root / snapshot_id
        with self._lock:
            if not path.is_dir():
                self._lru.pop(snapshot_id, None)
                return None
            if snapshot_id in self._lru:
                self._lru.move_to_end(snapshot_id)
            else:
                self._lru[snapshot_id] = _tree_size(path)
            return path

    def _install(self, tmp: Path, dest: Path) -> None:
        """Evict least recently used snapshots until tmp fits, then rename it into place.

        The snapshot being installed is extracted (and measured) before eviction, so the
        cache volume needs headroom of one snapshot (1 GiB extraction limit) above the
        budget. A snapshot larger than the whole budget evicts all others and is kept.
        """
        size = _tree_size(tmp)
        doomed: list[Path] = []
        try:
            with self._lock:
                if dest.is_dir():  # another worker installed it first
                    shutil.rmtree(tmp, ignore_errors=True)
                    self._lru[dest.name] = self._lru.pop(dest.name, None) or _tree_size(dest)
                    return
                while self._lru and sum(self._lru.values()) + size > self._max_bytes:
                    victim, _ = self._lru.popitem(last=False)
                    grave = self._root / f"{victim}.evict-{uuid.uuid4().hex}"
                    try:
                        os.rename(self._root / victim, grave)  # atomic: readers see it or not
                    except FileNotFoundError:
                        continue
                    doomed.append(grave)
                try:
                    os.rename(tmp, dest)
                except OSError:
                    if not dest.is_dir():
                        raise
                    shutil.rmtree(tmp, ignore_errors=True)
                    size = _tree_size(dest)
                self._lru[dest.name] = size
        finally:
            for grave in doomed:
                shutil.rmtree(grave, ignore_errors=True)

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
            self._install(tmp, dest)
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
        path = self._cached(snapshot_id)
        if path is not None:
            return path
        # Not on disk: fetch it again through the digest index, as get_snapshot does.
        info = self.get_snapshot("sha256:" + snapshot_id[len("sha256-") :])
        if info.snapshot_id != snapshot_id:  # an alias digest, not a snapshot id
            raise SnapshotNotFound
        return info.path


def _tree_size(root: Path) -> int:
    """Sum of regular-file sizes under root (symlinks not followed)."""
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total
