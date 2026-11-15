"""snapshot-alias: index a production digest against an existing snapshot."""
from __future__ import annotations

import argparse
import json
import sys

from snapshotkit.digests import digest_key
from snapshotkit.store import S3Store, SnapshotStore, digest_index_key, validate_project


def add_alias(store: SnapshotStore, project: str, digest: str, alias: str) -> bool:
    """Write the alias index entry; False if it already exists. Never reads."""
    validate_project(project)
    digest_key(digest)
    key = digest_index_key(alias)
    body = json.dumps({"project": project, "snapshot_digest": digest}).encode()
    return store.put_new(key, body)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="snapshot-alias")
    p.add_argument("--project", required=True)
    p.add_argument("--digest", required=True)
    p.add_argument("--alias-digest", required=True)
    args = p.parse_args(argv)
    try:
        created = add_alias(S3Store.from_env(), args.project, args.digest, args.alias_digest)
    except Exception as e:  # noqa: BLE001
        print(f"snapshot-alias: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("snapshot-alias: " + ("alias written" if created else "alias already exists"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
