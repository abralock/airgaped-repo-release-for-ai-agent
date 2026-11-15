import asyncio
import json

import pytest
from fakes import FakeClusterReader

from snapshotkit import files, mcp_server
from snapshotkit.clusters import PodContainer
from snapshotkit.mcp_server import build_server, make_handlers
from snapshotkit.snapshots import SnapshotCache
from snapshotkit.store import InMemoryStore, digest_index_key, manifest_key, snapshot_key
from snapshotkit.tarball import Entry, write_normalized_targz

DIGEST = "sha256:" + "a" * 64
SNAP_ID = "sha256-" + "a" * 64
MSG = "No snapshot available for this version."


@pytest.fixture
def cache_root(tmp_path):
    return tmp_path / "cache"


@pytest.fixture
def cache(tmp_path, cache_root):
    store = InMemoryStore()
    tar = tmp_path / "s.tar.gz"
    write_normalized_targz([Entry("src/app.py", 0o644, b"print(1)\n")], tar)
    store.put(snapshot_key("proj", DIGEST), tar.read_bytes())
    store.put(manifest_key("proj", DIGEST), json.dumps({"project": "proj"}).encode())
    store.put(
        digest_index_key(DIGEST),
        json.dumps({"project": "proj", "snapshot_digest": DIGEST}).encode(),
    )
    return SnapshotCache(store, cache_root)


@pytest.fixture
def readers():
    pods = {("prod", "web"): [PodContainer("web-1", "web", "reg/app:1", f"docker-pullable://reg/app@{DIGEST}")]}
    return {"c1": FakeClusterReader(pods=pods)}, {"c1": ["prod"]}


@pytest.fixture
def handlers(readers, cache):
    return make_handlers(*readers, cache)


def test_tools_registered(readers, cache):
    server = build_server(*readers, cache)
    assert server.name == "code-snapshots"
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {"find_deployed_version", "get_snapshot", "list_files", "read_file", "search_code"}
    assert tools["find_deployed_version"].description == mcp_server.FIND_DESCRIPTION
    assert tools["find_deployed_version"].description.startswith(
        "Find which image digest is running for a workload in production. If more than one digest"
    )
    assert tools["get_snapshot"].description == (
        "Fetch the source code snapshot for an image digest returned by find_deployed_version. "
        "If there is no snapshot, tell the user 'No snapshot available for this version.' and "
        "do not answer from another version's code."
    )


def test_find_deployed_version_returns_dicts(handlers):
    out = handlers.find_deployed_version("prod", "web")
    assert out["errors"] == {}
    assert out["versions"][0]["digest"] == DIGEST
    json.dumps(out)


def test_get_snapshot_not_found_message(handlers):
    assert handlers.get_snapshot("sha256:" + "f" * 64) == {"error": MSG}


def test_get_snapshot_hides_path(handlers, cache_root):
    out = handlers.get_snapshot(DIGEST)
    assert out["snapshot_id"] == SNAP_ID and out["project"] == "proj"
    assert out["manifest"] == {"project": "proj"}
    assert str(cache_root) not in json.dumps(out)
    assert "path" not in out


def test_file_tools_work_after_fetch(handlers):
    handlers.get_snapshot(DIGEST)
    assert handlers.list_files(SNAP_ID)["files"] == ["src/app.py"]
    assert handlers.read_file(SNAP_ID, "src/app.py")["content"] == "print(1)\n"
    assert handlers.search_code(SNAP_ID, "print")["matches"]


def test_file_tools_require_fetched_snapshot(handlers):
    assert handlers.read_file(SNAP_ID, "src/app.py") == {"error": MSG}
    assert handlers.list_files(SNAP_ID) == {"error": MSG}
    assert handlers.search_code(SNAP_ID, "x") == {"error": MSG}


def test_errors_are_returned_not_raised(handlers):
    handlers.get_snapshot(DIGEST)
    out = handlers.read_file(SNAP_ID, "../../etc/passwd")
    assert "error" in out and "etc/passwd" not in out.get("content", "")
    assert handlers.read_file(SNAP_ID, "src") == {"error": "not a file in this snapshot: src"}
    bad = handlers.search_code(SNAP_ID, "(")
    assert bad["error"].startswith("invalid pattern:")


def test_unexpected_errors_are_opaque(readers, cache, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(files, "list_files", boom)
    h = make_handlers(*readers, cache)
    h.get_snapshot(DIGEST)
    assert h.list_files(SNAP_ID) == {"error": "internal error"}
    assert "secret detail" in capsys.readouterr().err
