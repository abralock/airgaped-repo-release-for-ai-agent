"""History-free export of a git repository into a normalized tar.gz."""

from __future__ import annotations

import os
import subprocess
import tempfile
import tarfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from snapshotkit.denylist import DEFAULT_DENYLIST, is_denied
from snapshotkit.tarball import Entry, write_normalized_targz

_HINT = "set withSubmodulesOrLfs: true (Azure DevOps) or SNAPSHOT_MODE=checkout (GitLab)"
_LFS_MAGIC = b"version https://git-lfs.github.com/spec/v1"


class ExportError(Exception):
    def __init__(
        self,
        reason: Literal[
            "export-subst", "needs-checkout-mode", "lfs-not-pulled", "submodule-not-initialized"
        ],
        message: str,
    ) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class ExportStats:
    files_included: int
    files_denied: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str, input: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repo, input=input, check=True, capture_output=True
    ).stdout


def _finish(entries: list[Entry], denied: list[str], out_path: Path) -> ExportStats:
    write_normalized_targz(entries, out_path)
    return ExportStats(files_included=len(entries), files_denied=sorted(denied))


def export_from_archive(
    repo_dir: Path, commit: str, out_path: Path, patterns: Sequence[str] = DEFAULT_DENYLIST
) -> ExportStats:
    tree = f"{commit}^{{tree}}"
    listing = _git(repo_dir, "ls-tree", "-r", "-z", tree).split(b"\0")
    for rec in filter(None, listing):
        meta, _, raw_path = rec.partition(b"\t")
        mode = meta.split()[0]
        path = raw_path.decode("utf-8", "surrogateescape")
        base = path.rsplit("/", 1)[-1]
        if path == ".gitmodules" or mode == b"160000":
            raise ExportError("needs-checkout-mode", f"repository uses submodules; {_HINT}")
        if base == ".gitattributes":
            text = _git(repo_dir, "show", f"{commit}:{path}").decode("utf-8", "replace")
            if "export-subst" in text:
                raise ExportError("export-subst", f"{path} uses export-subst, which would embed commit data")
            if "filter=lfs" in text:
                raise ExportError("needs-checkout-mode", f"repository uses Git LFS; {_HINT}")

    entries: list[Entry] = []
    denied: list[str] = []
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(
            ["git", "archive", "--format=tar", tree], cwd=repo_dir,
            stdout=subprocess.PIPE, stderr=errf,
        )
        try:
            assert proc.stdout is not None
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                for m in tar:
                    if is_denied(m.name, patterns):
                        if m.isfile() or m.issym():
                            denied.append(m.name)
                        continue
                    if m.issym():
                        entries.append(Entry(m.name, m.mode, None, m.linkname))
                    elif m.isfile():
                        f = tar.extractfile(m)
                        entries.append(Entry(m.name, m.mode, f.read() if f else b""))
            rc = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
        if rc != 0:
            errf.seek(0)
            raise RuntimeError(f"git archive failed: {errf.read().decode(errors='replace')}")
    return _finish(entries, denied, out_path)


def _owning_repo(root: Path, rel: str, cache: dict[str, str]) -> str:
    """Return the repo-relative dir ('' for root) of the (sub)repo that owns rel."""
    d = os.path.dirname(rel)
    chain = []
    while True:
        if d in cache:
            res = cache[d]
            break
        if d and (root / d / ".git").exists():
            res = d
            cache[d] = res
            break
        chain.append(d)
        if not d:
            res = ""
            break
        d = os.path.dirname(d)
    for c in chain:
        cache[c] = res
    return res


def _ancestors(rel: str) -> list[str]:
    parts = rel.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def export_from_checkout(
    repo_dir: Path, out_path: Path, patterns: Sequence[str] = DEFAULT_DENYLIST
) -> ExportStats:
    raw = _git(repo_dir, "ls-files", "-z", "--recurse-submodules")
    paths = [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]

    cache: dict[str, str] = {}
    by_repo: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        by_repo[_owning_repo(repo_dir, p, cache)].append(p)

    ignored: set[str] = set()
    for sub, members in by_repo.items():
        cwd = repo_dir / sub if sub else repo_dir
        rels = {os.path.relpath(m, sub) if sub else m for m in members}
        # directory patterns (e.g. "internal/ export-ignore") only match the directory itself
        for r in list(rels):
            d = os.path.dirname(r)
            while d:
                rels.add(d + "/")
                d = os.path.dirname(d)
        rels = sorted(rels)
        out = _git(
            cwd, "check-attr", "-z", "--stdin", "export-ignore", "export-subst",
            input=b"\0".join(r.encode("utf-8", "surrogateescape") for r in rels) + b"\0",
        ).split(b"\0")
        for i in range(0, len(out) - 2, 3):
            rel = out[i].decode("utf-8", "surrogateescape")
            attr, value = out[i + 1], out[i + 2]
            full = os.path.normpath(os.path.join(sub, rel)) if sub else rel.rstrip("/")
            if attr == b"export-subst" and value not in (b"unspecified", b"unset"):
                raise ExportError("export-subst", f"{full} uses export-subst, which would embed commit data")
            if attr == b"export-ignore" and value == b"set":
                ignored.add(full)

    entries: list[Entry] = []
    denied: list[str] = []
    for p in paths:
        if p in ignored or any(a in ignored for a in _ancestors(p)):
            continue
        fs = repo_dir / p
        if fs.is_symlink():
            target = os.readlink(fs)
            is_link, data = True, None
        elif fs.is_dir():
            raise ExportError(
                "submodule-not-initialized",
                f"{p} is a submodule that is not initialized; check out with submodules "
                f"recursive / {_HINT}",
            )
        else:
            is_link, target = False, None
            data = fs.read_bytes()  # raises OSError if missing or unreadable
        if is_denied(p, patterns):
            denied.append(p)
            continue
        if is_link:
            entries.append(Entry(p, 0o755, None, target))
            continue
        if data[: len(_LFS_MAGIC)] == _LFS_MAGIC:
            raise ExportError("lfs-not-pulled", f"{p} is an unpulled Git LFS pointer; {_HINT} and pull LFS objects")
        entries.append(Entry(p, 0o755 if os.access(fs, os.X_OK) else 0o644, data))
    return _finish(entries, denied, out_path)
