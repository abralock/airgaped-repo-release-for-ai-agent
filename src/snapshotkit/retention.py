"""snapshot-retention: delete snapshots that are old, superseded and not deployed."""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from snapshotkit.clusters import ClusterReader, load_cluster_configs
from snapshotkit.lookup import build_readers
from snapshotkit.manifest import Manifest
from snapshotkit.store import SnapshotStore, manifest_key, snapshot_key

log = logging.getLogger(__name__)

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _load_manifests(store: SnapshotStore) -> list[Manifest]:
    manifests: list[Manifest] = []
    for key in store.list_keys("snapshots/"):
        if not key.endswith(".json"):
            continue
        project = key.split("/")[1]
        try:
            data = store.get(key)
            if data is None:
                continue
            m = Manifest.from_json(data)
            datetime.strptime(m.published_at, _TIME_FORMAT)
            if m.project != project or manifest_key(m.project, m.image_digest) != key:
                raise ValueError("manifest does not match its key")
        except Exception as e:  # noqa: BLE001 - unreadable manifests are kept
            log.warning("keeping snapshot with unparseable manifest %s: %s", key, e)
            continue
        manifests.append(m)
    return manifests


def _index_entries(store: SnapshotStore) -> list[tuple[str, str, str]]:
    """(key, project, snapshot_digest) for every parseable index entry."""
    entries = []
    for key in store.list_keys("index/"):
        data = store.get(key)
        if data is None:
            continue
        try:
            body = json.loads(data)
            entries.append((key, body["project"], body["snapshot_digest"]))
        except Exception:  # noqa: BLE001 - ignored, and never deleted
            log.warning("ignoring unparseable index entry %s", key)
    return entries


def _parse_time(m: Manifest) -> datetime:
    return datetime.strptime(m.published_at, _TIME_FORMAT).replace(tzinfo=timezone.utc)


def plan_retention(
    store: SnapshotStore,
    deployed: set[str],
    now: datetime,
    keep_last: int = 20,
    min_age_days: int = 90,
) -> list[tuple[str, str]]:
    manifests = _load_manifests(store)
    alias_digests: dict[tuple[str, str], set[str]] = {}
    for key, project, digest in _index_entries(store):
        if key.startswith("index/digest/"):
            name = key[len("index/digest/") : -len(".json")]
            alias_digests.setdefault((project, digest), set()).add(
                name.replace("-", ":", 1)
            )

    by_project: dict[str, list[Manifest]] = {}
    for m in manifests:
        by_project.setdefault(m.project, []).append(m)

    cutoff = now - timedelta(days=min_age_days)
    doomed: list[tuple[str, str]] = []
    for project, items in sorted(by_project.items()):
        items.sort(key=lambda m: (m.published_at, m.image_digest), reverse=True)
        for rank, m in enumerate(items):
            ids = {m.image_digest} | alias_digests.get((project, m.image_digest), set())
            if ids & deployed or rank < keep_last or _parse_time(m) > cutoff:
                continue
            doomed.append((project, m.image_digest))
    return doomed


def _keys_for(store: SnapshotStore, project: str, digest: str) -> list[str]:
    index_keys = sorted(
        key
        for key, p, d in _index_entries(store)
        if p == project and d == digest
    )
    return index_keys + [manifest_key(project, digest), snapshot_key(project, digest)]


def apply_retention(store: SnapshotStore, doomed: list[tuple[str, str]]) -> None:
    for project, digest in doomed:
        for key in _keys_for(store, project, digest):
            store.delete(key)


def _collect_deployed(readers: dict[str, ClusterReader]) -> tuple[set[str], list[str]]:
    deployed: set[str] = set()
    failed: list[str] = []
    for name in sorted(readers):
        try:
            ids = readers[name].all_image_ids()
        except Exception as e:  # noqa: BLE001
            print(f"cluster {name} unreachable: {type(e).__name__}: {e}", file=sys.stderr)
            failed.append(name)
            continue
        if not ids:
            # A production cluster running nothing is far more likely a wrong namespace
            # list or a token with no bindings than reality; treat it as a failure.
            print(
                f"cluster {name} reported no running image digests; refusing to delete",
                file=sys.stderr,
            )
            failed.append(name)
            continue
        deployed |= ids
    return deployed, failed


def main(
    argv: list[str] | None = None,
    *,
    store: SnapshotStore | None = None,
    readers: dict[str, ClusterReader] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="snapshot-retention")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if readers is None:
            configs = load_cluster_configs(Path(os.environ["SNAPSHOT_TOOL_CLUSTERS"]))
            readers, _ = build_readers(configs)
        deployed, failed = _collect_deployed(readers)
        if failed:
            print(
                "aborting, nothing deleted; failed clusters: " + ", ".join(failed),
                file=sys.stderr,
            )
            return 2
        if store is None:
            from snapshotkit.store import S3Store

            store = S3Store.from_env()
        total = len(_load_manifests(store))
        doomed = plan_retention(store, deployed, datetime.now(timezone.utc))
        if args.dry_run:
            for project, digest in doomed:
                print(f"would delete {project} {digest}")
                for key in _keys_for(store, project, digest):
                    print(f"  {key}")
            print(f"dry run: kept {total - len(doomed)}, would delete {len(doomed)}")
            return 0
        apply_retention(store, doomed)
        print(f"kept {total - len(doomed)}, deleted {len(doomed)}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
