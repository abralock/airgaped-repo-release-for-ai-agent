"""Confined, read-only file access inside an extracted snapshot."""

from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path

import regex

BINARY_SNIFF_BYTES = 8192
MAX_LINE_CHARS = 500
MATCH_LINE_CAP = 10_000
MAX_SEARCH_FILE_BYTES = 5 * 1024 * 1024
SEARCH_TIME_BUDGET = 2.0


class PathOutsideSnapshot(Exception):
    """Raised when a path resolves outside the snapshot root."""


class NotAFileInSnapshot(Exception):
    """Raised when a requested path is missing or not a regular file."""


def resolve_inside(root: Path, rel: str) -> Path:
    if "\x00" in rel or rel.startswith("/") or Path(rel).is_absolute():
        raise PathOutsideSnapshot(f"path outside snapshot: {rel!r}")
    base = root.resolve()
    try:
        target = (base / rel).resolve()
    except (RuntimeError, OSError):
        raise PathOutsideSnapshot(f"path outside snapshot: {rel!r}") from None
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
    if not path.is_file():
        raise NotAFileInSnapshot(f"not a file in this snapshot: {rel_path}")
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes + 1)
    except OSError:
        raise NotAFileInSnapshot(f"not a file in this snapshot: {rel_path}") from None
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
        rx = regex.compile(pattern)
    except regex.error as exc:
        raise ValueError(f"invalid pattern: {exc}") from exc
    base = root.resolve()
    deadline = time.monotonic() + SEARCH_TIME_BUDGET
    matches: list[dict] = []

    def result(truncated: bool, timed_out: bool = False) -> dict:
        out = {"matches": matches, "truncated": truncated}
        if timed_out:
            out["timed_out"] = True
        return out

    for rel in list_files(root, limit=10**9)["files"]:
        if glob and not (
            fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], glob)
        ):
            continue
        try:
            path = resolve_inside(base, rel)
            if not path.is_file() or path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            with open(path, "rb") as f:
                if _is_binary(f.read(BINARY_SNIFF_BYTES)):
                    continue
                f.seek(0)
                for lineno, raw in enumerate(f, start=1):
                    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return result(True, True)
                    try:
                        hit = rx.search(text[:MATCH_LINE_CAP], timeout=remaining)
                    except TimeoutError:
                        return result(True, True)
                    if hit:
                        if len(matches) >= max_matches:
                            return result(True)
                        matches.append(
                            {"path": rel, "line": lineno, "text": text[:MAX_LINE_CHARS]}
                        )
        except (PathOutsideSnapshot, OSError):
            continue
    return result(False)
