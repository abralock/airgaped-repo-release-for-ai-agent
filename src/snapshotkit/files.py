"""Confined, read-only file access inside an extracted snapshot."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

BINARY_SNIFF_BYTES = 8192
MAX_LINE_CHARS = 500


class PathOutsideSnapshot(Exception):
    """Raised when a path resolves outside the snapshot root."""


def resolve_inside(root: Path, rel: str) -> Path:
    if "\x00" in rel or rel.startswith("/") or Path(rel).is_absolute():
        raise PathOutsideSnapshot(f"path outside snapshot: {rel!r}")
    base = root.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise PathOutsideSnapshot(f"path outside snapshot: {rel!r}")
    return target


def _is_binary(head: bytes) -> bool:
    return b"\x00" in head[:BINARY_SNIFF_BYTES]


def list_files(root: Path, subdir: str = "", limit: int = 2000) -> dict:
    base = root.resolve()
    start = resolve_inside(root, subdir)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        names = list(filenames)
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                dirnames.remove(d)
                names.append(d)
        for name in names:
            found.append(Path(dirpath, name).relative_to(base).as_posix())
    found.sort()
    return {"files": found[:limit], "truncated": len(found) > limit}


def read_file(root: Path, rel_path: str, max_bytes: int = 200_000) -> dict:
    path = resolve_inside(root, rel_path)
    with open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    if _is_binary(data):
        return {"content": "", "truncated": False, "binary": True}
    truncated = len(data) > max_bytes
    return {
        "content": data[:max_bytes].decode("utf-8", errors="replace"),
        "truncated": truncated,
        "binary": False,
    }


def search_code(
    root: Path, pattern: str, glob: str | None = None, max_matches: int = 200
) -> dict:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid pattern: {exc}") from exc
    base = root.resolve()
    matches: list[dict] = []
    for rel in list_files(root, limit=10**9)["files"]:
        if glob and not (
            fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], glob)
        ):
            continue
        try:
            path = resolve_inside(base, rel)
        except PathOutsideSnapshot:
            continue
        if not path.is_file():
            continue
        with open(path, "rb") as f:
            if _is_binary(f.read(BINARY_SNIFF_BYTES)):
                continue
            f.seek(0)
            for lineno, raw in enumerate(f, start=1):
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if regex.search(text):
                    if len(matches) >= max_matches:
                        return {"matches": matches, "truncated": True}
                    matches.append(
                        {"path": rel, "line": lineno, "text": text[:MAX_LINE_CHARS]}
                    )
    return {"matches": matches, "truncated": False}
