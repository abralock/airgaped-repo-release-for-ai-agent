"""One-way snapshot transfer bundle for a CI zone and production separated by a gap."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from snapshotkit.store import S3Store, SnapshotStore

PREFIXES = ("snapshots/", "index/digest/", "index/tag/")
BUNDLE_NAME = "bundle.json"
BUNDLE_VERSION = 1


class TransferError(Exception):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _check_key(key: str) -> None:
    if (
        not isinstance(key, str)
        or key.startswith("/")
        or ".." in key.split("/")
        or ".." in key
        or "\\" in key
        or "\0" in key
        or not key.startswith(PREFIXES)
    ):
        raise TransferError(f"bad key in bundle: {key!r}")


def _read_state(state_file: Path) -> dict[str, str | None]:
    """Exported keys -> sha256 at export time (None for the old list format)."""
    if not state_file.exists():
        return {}
    data = json.loads(state_file.read_text())
    if isinstance(data, list):
        return {k: None for k in data}
    if isinstance(data, dict):
        return dict(data)
    raise TransferError("state file must be a JSON object (or legacy list)")


def export_bundle(src: SnapshotStore, out_dir: Path, state_file: Path) -> int:
    state = _read_state(state_file)
    files: dict[str, str] = {}
    for key in sorted({k for p in PREFIXES for k in src.list_keys(p)}):
        if key in state and not key.startswith("index/tag/"):
            continue  # write-once keys never change; skip without fetching
        data = src.get(key)
        if data is None:
            continue  # deleted between list and get
        digest = _sha256(data)
        if state.get(key) == digest:
            continue
        _atomic_write(out_dir / key, data)
        files[key] = digest
    manifest = {"version": BUNDLE_VERSION, "files": files}
    _atomic_write(out_dir / BUNDLE_NAME, json.dumps(manifest, sort_keys=True).encode())
    _atomic_write(state_file, json.dumps({**state, **files}, sort_keys=True).encode())
    return len(files)


def _import_rank(key: str) -> int:
    if key.startswith("snapshots/"):
        return 0 if key.endswith(".tar.gz") else 1
    return 2 if key.startswith("index/digest/") else 3


def import_bundle(dst: SnapshotStore, in_dir: Path) -> int:
    try:
        bundle = json.loads((in_dir / BUNDLE_NAME).read_text())
        if bundle["version"] != BUNDLE_VERSION:
            raise TransferError(f"unsupported bundle version: {bundle['version']!r}")
        files = dict(bundle["files"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TransferError(f"unreadable bundle.json: {exc}") from exc
    for key in files:
        _check_key(key)
    root = in_dir.resolve()
    loaded: dict[str, bytes] = {}
    for key, expected in files.items():
        path = (in_dir / key).resolve()
        if not path.is_relative_to(root):
            raise TransferError(f"bad key in bundle: {key!r}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise TransferError(f"cannot read {key}: {exc}") from exc
        if _sha256(data) != expected:
            raise TransferError(f"checksum mismatch: {key}")
        loaded[key] = data
    written = 0
    for key in sorted(loaded, key=lambda k: (_import_rank(k), k)):
        if key.startswith("index/tag/"):
            dst.put(key, loaded[key])
            written += 1
        elif dst.put_new(key, loaded[key]):
            written += 1
    return written


def main(argv: Sequence[str] | None = None, *, store: SnapshotStore | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snapshot-transfer")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--out", required=True, type=Path)
    exp.add_argument("--state", required=True, type=Path)
    imp = sub.add_parser("import")
    imp.add_argument("--in", dest="in_dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        st = store if store is not None else S3Store.from_env()
        if args.command == "export":
            n = export_bundle(st, args.out, args.state)
            print(f"exported {n} keys to {args.out}")
        else:
            n = import_bundle(st, args.in_dir)
            print(f"imported {n} keys from {args.in_dir}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary: any failure is exit 1
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
