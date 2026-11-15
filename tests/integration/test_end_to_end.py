import functools

import pytest
from fakes import FakeClusterReader

from snapshotkit import publish
from snapshotkit.clusters import PodContainer
from snapshotkit.mcp_server import make_handlers
from snapshotkit.scan import ScanResult
from snapshotkit.snapshots import SnapshotCache

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "d" * 64


def clean_scanner(tree, config, report, ignore=None):
    return ScanResult(True, 0, report)


def test_publish_then_lookup_fetch_and_read(tmp_path, make_repo, s3_store, s3_settings, s3_bucket, monkeypatch):
    repo, sha = make_repo(
        tmp_path, {"src/app.py": "print('hello')\n", "README.md": "# app\n"}
    )
    gl = tmp_path / "gl.toml"
    gl.write_text("")
    monkeypatch.setenv("SNAPSHOT_STORE_ENDPOINT", s3_settings["endpoint_url"])
    monkeypatch.setenv("SNAPSHOT_STORE_BUCKET", s3_bucket)
    monkeypatch.setenv("SNAPSHOT_STORE_ACCESS_KEY", s3_settings["access_key"])
    monkeypatch.setenv("SNAPSHOT_STORE_SECRET_KEY", s3_settings["secret_key"])
    # the real scanner needs the gitleaks binary; use an always-clean fake
    monkeypatch.setattr(
        publish, "publish_snapshot", functools.partial(publish.publish_snapshot, scanner=clean_scanner, scanner_version=lambda: "test")
    )
    rc = publish.main([
        "--repo-dir", str(repo), "--commit", sha, "--project", "e2e",
        "--source-platform", "gitlab", "--repo-path", "g/e2e",
        "--image-repository", "reg.local/e2e/app", "--image-tag", "1.0",
        "--image-digest", DIGEST, "--gitleaks-config", str(gl),
        "--workdir", str(tmp_path / "work"),
    ])
    assert rc == 0

    pods = {("prod", "web"): [
        PodContainer("web-1", "web", "reg.local/e2e/app:1.0", f"docker-pullable://reg.local/e2e/app@{DIGEST}")
    ]}
    cache = SnapshotCache(s3_store, tmp_path / "cache")
    h = make_handlers({"c1": FakeClusterReader(pods=pods)}, {"c1": ["prod"]}, cache)

    found = h.find_deployed_version("prod", "web")
    assert found["errors"] == {}
    digest = found["versions"][0]["digest"]
    assert digest == DIGEST

    snap = h.get_snapshot(digest)
    assert "error" not in snap and snap["project"] == "e2e"
    assert sha not in str(snap)

    content = h.read_file(snap["snapshot_id"], "src/app.py")
    assert content["content"] == (repo / "src/app.py").read_text()
    listed = h.list_files(snap["snapshot_id"])["files"]
    assert "src/app.py" in listed
    assert not any(p == ".git" or p.startswith(".git/") for p in listed)
