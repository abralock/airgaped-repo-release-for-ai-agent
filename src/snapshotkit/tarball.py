"""Deterministic, metadata-free tar.gz writing and guarded extraction."""

from __future__ import annotations

import gzip
import io
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class UnsafeArchiveError(Exception):
    """Raised when an archive is unsafe to extract or exceeds limits."""


@dataclass(frozen=True)
class Entry:
    path: str
    mode: int
    data: bytes | None
    link_target: str | None = None


def write_normalized_targz(entries: Iterable[Entry], out_path: Path) -> None:
    with open(out_path, "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="") as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                for e in sorted(entries, key=lambda x: x.path):
                    info = tarfile.TarInfo(e.path)
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    if e.link_target is not None:
                        info.type = tarfile.SYMTYPE
                        info.linkname = e.link_target
                        info.mode = 0o755
                        tar.addfile(info)
                    else:
                        data = e.data or b""
                        info.size = len(data)
                        info.mode = 0o755 if e.mode & 0o111 else 0o644
                        tar.addfile(info, io.BytesIO(data))


def safe_extract(
    targz: Path,
    dest: Path,
    max_total_bytes: int = 1 << 30,
    max_entries: int = 200_000,
) -> None:
    """Validate every member first; extract only if all pass."""
    try:
        with tarfile.open(targz, "r:gz") as tar:
            members = []
            total = 0
            for member in tar:
                if len(members) + 1 > max_entries:
                    raise UnsafeArchiveError(f"more than {max_entries} entries")
                total += member.size if member.isfile() else 0
                if total > max_total_bytes:
                    raise UnsafeArchiveError(f"more than {max_total_bytes} bytes")
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise UnsafeArchiveError(f"unsafe member name: {member.name!r}")
                tarfile.data_filter(member, str(dest))
                members.append(member)
            tar.extractall(dest, members=members, filter="data")
    except tarfile.FilterError as exc:
        raise UnsafeArchiveError(str(exc)) from exc
