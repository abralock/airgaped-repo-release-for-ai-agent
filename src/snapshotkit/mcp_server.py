"""MCP server exposing deployed-version lookup and snapshot browsing tools."""

from __future__ import annotations

import dataclasses
import functools
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from mcp.server.mcpserver import MCPServer

from snapshotkit import files, lookup
from snapshotkit.clusters import ClusterReader, load_cluster_configs
from snapshotkit.lookup import build_readers
from snapshotkit.snapshots import DEFAULT_MAX_BYTES, SnapshotCache, SnapshotNotFound
from snapshotkit.store import S3Store

FIND_DESCRIPTION = (
    "Find which image digest is running for a workload in production. If more than one "
    "digest is returned, a rollout is in progress: tell the user. If a cluster is listed "
    "under errors, say its version could not be checked."
)
GET_SNAPSHOT_DESCRIPTION = (
    "Fetch the source code snapshot for an image digest returned by find_deployed_version. "
    "If there is no snapshot, tell the user 'No snapshot available for this version.' and "
    "do not answer from another version's code."
)
LIST_FILES_DESCRIPTION = "List files in a fetched snapshot, optionally within a subdirectory."
READ_FILE_DESCRIPTION = "Read a text file from a fetched snapshot."
SEARCH_CODE_DESCRIPTION = (
    "Search a fetched snapshot with a regular expression, optionally limited by a glob."
)

_KNOWN_ERRORS = (
    SnapshotNotFound,
    files.PathOutsideSnapshot,
    files.NotAFileInSnapshot,
    ValueError,
)


def _safe(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Handlers return a dict and never raise."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> dict:
        try:
            return fn(*args, **kwargs)
        except _KNOWN_ERRORS as e:
            return {"error": str(e)}
        except Exception:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return {"error": "internal error"}

    return wrapper


def make_handlers(
    readers: dict[str, ClusterReader],
    allowed_namespaces: dict[str, list[str]],
    cache: SnapshotCache,
) -> SimpleNamespace:
    @_safe
    def find_deployed_version(namespace: str, workload: str, cluster: str | None = None) -> dict:
        result = lookup.find_deployed_version(
            readers, allowed_namespaces, namespace, workload, cluster
        )
        return {
            "versions": [dataclasses.asdict(v) for v in result.versions],
            "errors": result.errors,
        }

    @_safe
    def get_snapshot(ref: str) -> dict:
        info = cache.get_snapshot(ref)
        return {
            "snapshot_id": info.snapshot_id,
            "project": info.project,
            "manifest": info.manifest,
        }

    @_safe
    def list_files(snapshot_id: str, subdir: str = "") -> dict:
        return files.list_files(cache.path_for(snapshot_id), subdir)

    @_safe
    def read_file(snapshot_id: str, path: str) -> dict:
        return files.read_file(cache.path_for(snapshot_id), path)

    @_safe
    def search_code(snapshot_id: str, pattern: str, glob: str | None = None) -> dict:
        return files.search_code(cache.path_for(snapshot_id), pattern, glob)

    return SimpleNamespace(
        find_deployed_version=find_deployed_version,
        get_snapshot=get_snapshot,
        list_files=list_files,
        read_file=read_file,
        search_code=search_code,
    )


def build_server(
    readers: dict[str, ClusterReader],
    allowed_namespaces: dict[str, list[str]],
    cache: SnapshotCache,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> MCPServer:
    h = make_handlers(readers, allowed_namespaces, cache)
    server = MCPServer("code-snapshots")
    # mcp 2.x takes host/port when running, not at construction; keep them for main().
    server.bind_host, server.bind_port = host, port
    server.tool(description=FIND_DESCRIPTION)(h.find_deployed_version)
    server.tool(description=GET_SNAPSHOT_DESCRIPTION)(h.get_snapshot)
    server.tool(description=LIST_FILES_DESCRIPTION)(h.list_files)
    server.tool(description=READ_FILE_DESCRIPTION)(h.read_file)
    server.tool(description=SEARCH_CODE_DESCRIPTION)(h.search_code)
    return server


def cache_from_env(store) -> SnapshotCache:
    """SNAPSHOT_CACHE_DIR (default /cache), SNAPSHOT_CACHE_MAX_BYTES (default 4 GiB)."""
    max_bytes = os.environ.get("SNAPSHOT_CACHE_MAX_BYTES")
    return SnapshotCache(
        store,
        Path(os.environ.get("SNAPSHOT_CACHE_DIR", "/cache")),
        max_bytes=int(max_bytes) if max_bytes else DEFAULT_MAX_BYTES,
    )


def main() -> None:
    configs = load_cluster_configs(Path(os.environ["SNAPSHOT_TOOL_CLUSTERS"]))
    readers, allowed = build_readers(configs)
    cache = cache_from_env(S3Store.from_env())
    server = build_server(readers, allowed, cache)
    server.run(transport="streamable-http", host=server.bind_host, port=server.bind_port)


if __name__ == "__main__":
    main()
