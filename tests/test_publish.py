import gzip
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

import shutil

from conftest import git
from snapshotkit import publish
from snapshotkit.publish import Outcome, PublishRequest, publish_snapshot
from snapshotkit.scan import ScannerError, ScanResult
from snapshotkit.store import InMemoryStore, digest_index_key, manifest_key, snapshot_key, tag_index_key

D = "sha256:" + "a" * 64
A1 = "sha256:" + "b" * 64
A2 = "sha256:" + "c" * 64
NOW = lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)  # noqa: E731


def clean_scanner(tree, config, report, ignore=None):
    return ScanResult(True, 0, report)


def mkreq(tmp_path, repo, sha, **kw):
    cfg = tmp_path / "gl.toml"
    cfg.write_text("")
    base = dict(
        repo_dir=repo, commit=sha, mode="archive", project="proj", source_platform="gitlab",
        repo_path="g/p", image_repository="reg/app", image_tag="1.0", image_digest=D,
        alias_digests=[A1, A2, A1, D], pipeline_run_url="http://ci/1", gitleaks_config=cfg,
        gitleaks_ignore=None, workdir=tmp_path / "work",
    )
    base.update(kw)
    return PublishRequest(**base)


def go(tmp_path, make_repo, store=None, scanner=clean_scanner, files=None, **kw):
    repo, sha = make_repo(tmp_path, files or {"a.txt": "hello", "d/b.txt": "world"})
    store = store if store is not None else InMemoryStore()
    req = mkreq(tmp_path, repo, sha, **kw)
    out = publish_snapshot(req, store, scanner=scanner, scanner_version=lambda: "8.test", now=NOW)
    return out, store, sha


def test_publishes_tar_manifest_and_index(tmp_path, make_repo):
    out, store, _ = go(tmp_path, make_repo)
    assert out is Outcome.PUBLISHED
    keys = set(store.list_keys(""))
    body = json.dumps({"project": "proj", "snapshot_digest": D}).encode()
    assert keys == {
        snapshot_key("proj", D), manifest_key("proj", D), digest_index_key(D),
        digest_index_key(A1), digest_index_key(A2), tag_index_key("reg/app", "1.0"),
    }
    assert store.get(digest_index_key(A1)) == body
    m = json.loads(store.get(manifest_key("proj", D)))
    assert m["published_at"] == "2026-01-02T03:04:05Z"
    assert m["scanner"] == "gitleaks" and m["scanner_version"] == "8.test"
    assert m["scan_result"] == "clean"


def test_scans_exact_upload(tmp_path, make_repo):
    seen = {}

    def scanner(tree, config, report, ignore=None):
        seen["files"] = {str(p.relative_to(tree)) for p in Path(tree).rglob("*") if p.is_file()}
        return ScanResult(True, 0, report)

    _, store, _ = go(tmp_path, make_repo, scanner=scanner)
    with tarfile.open(fileobj=io.BytesIO(store.get(snapshot_key("proj", D))), mode="r:gz") as t:
        assert seen["files"] == {m.name for m in t if m.isfile()}
    assert seen["files"] == {"a.txt", "d/b.txt"}


def test_secrets_found_writes_nothing(tmp_path, make_repo):
    out, store, _ = go(tmp_path, make_repo, scanner=lambda t, c, r, i=None: ScanResult(False, 2, r))
    assert out is Outcome.SECRETS_FOUND
    assert list(store.list_keys("")) == []


def test_scanner_error_writes_nothing(tmp_path, make_repo):
    def boom(*a, **k):
        raise ScannerError("x")

    store = InMemoryStore()
    with pytest.raises(ScannerError):
        go(tmp_path, make_repo, store=store, scanner=boom)
    assert list(store.list_keys("")) == []


def test_export_refused(tmp_path, make_repo):
    out, store, _ = go(
        tmp_path, make_repo, files={".gitattributes": "*.txt export-subst\n", "a.txt": "$Format:%H$"}
    )
    assert out is Outcome.EXPORT_REFUSED
    assert list(store.list_keys("")) == []


def test_rerun_same_digest_is_already_published(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, {"a.txt": "hello"})
    store = InMemoryStore()
    store.put_new(snapshot_key("proj", D), b"preexisting")
    req = mkreq(tmp_path, repo, sha)
    out = publish_snapshot(req, store, scanner=clean_scanner, scanner_version=lambda: "v", now=NOW)
    assert out is Outcome.ALREADY_PUBLISHED
    assert store.get(snapshot_key("proj", D)) == b"preexisting"
    assert store.get(manifest_key("proj", D)) is not None
    for d in (D, A1, A2):
        assert store.get(digest_index_key(d)) is not None
    assert store.get(tag_index_key("reg/app", "1.0")) is not None


class Recording(InMemoryStore):
    def __init__(self):
        super().__init__()
        self.order = []

    def put_new(self, key, data):
        self.order.append(key)
        return super().put_new(key, data)

    def put(self, key, data):
        self.order.append(key)
        super().put(key, data)


def test_index_written_last(tmp_path, make_repo):
    _, store, _ = go(tmp_path, make_repo, store=Recording())
    idx = [i for i, k in enumerate(store.order) if k.startswith("index/")]
    non = [i for i, k in enumerate(store.order) if not k.startswith("index/")]
    assert len(non) == 2 and idx and min(idx) > max(non)


def test_reviewed_allowlist_sets_scan_result(tmp_path, make_repo):
    ign = tmp_path / ".gitleaksignore"
    ign.write_text("")
    _, store, _ = go(tmp_path, make_repo, gitleaks_ignore=ign)
    assert json.loads(store.get(manifest_key("proj", D)))["scan_result"] == "clean-with-reviewed-allowlist"


def test_no_commit_sha_anywhere(tmp_path, make_repo):
    _, store, sha = go(tmp_path, make_repo)
    for k in store.list_keys(""):
        data = store.get(k)
        if k.endswith(".tar.gz"):
            data = gzip.decompress(data)
        assert sha.encode() not in data


def test_invalid_digest_writes_nothing(tmp_path, make_repo):
    store = InMemoryStore()
    with pytest.raises(ValueError):
        go(tmp_path, make_repo, store=store, alias_digests=["nope"])
    assert list(store.list_keys("")) == []


ARGS = [
    "--repo-dir", "r", "--commit", "abc", "--project", "p", "--source-platform", "gitlab",
    "--repo-path", "g/p", "--image-repository", "reg/app", "--image-tag", "1", "--image-digest", D,
    "--gitleaks-config", "c.toml",
]


@pytest.mark.parametrize(
    "result,code,notified",
    [
        (Outcome.PUBLISHED, 0, False), (Outcome.ALREADY_PUBLISHED, 0, False),
        (Outcome.SECRETS_FOUND, 10, True), (Outcome.EXPORT_REFUSED, 11, True),
        (RuntimeError("boom"), 1, True),
    ],
)
def test_main_exit_codes(monkeypatch, result, code, notified):
    calls = []

    def fake(req, store):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(publish, "publish_snapshot", fake)
    monkeypatch.setattr(publish.S3Store, "from_env", classmethod(lambda cls: InMemoryStore()))
    monkeypatch.setattr(publish, "send_failure", lambda url, payload: calls.append(payload))
    assert publish.main(ARGS) == code
    assert bool(calls) is notified
    if calls:
        assert set(calls[0]) == {"project", "image_digest", "reason", "pipeline_run_url"}


def test_main_platform_manifest(monkeypatch, tmp_path):
    pm = tmp_path / "m.json"
    pm.write_text(json.dumps({"manifests": [
        {"digest": A1, "platform": {"os": "linux"}},
        {"digest": A2, "platform": {"os": "unknown"}},
    ]}))
    seen = {}

    def fake(req, store):
        seen["a"] = req.alias_digests
        return Outcome.PUBLISHED

    monkeypatch.setattr(publish, "publish_snapshot", fake)
    monkeypatch.setattr(publish.S3Store, "from_env", classmethod(lambda cls: InMemoryStore()))
    assert publish.main([*ARGS, "--alias-digest", A2, "--platform-manifest", str(pm)]) == 0
    assert seen["a"] == [A2, A1]


def test_main_bad_webhook_still_exit_10(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_NOTIFY_WEBHOOK", "hooks.internal/x")
    monkeypatch.setattr(publish, "publish_snapshot", lambda r, s: Outcome.SECRETS_FOUND)
    monkeypatch.setattr(publish.S3Store, "from_env", classmethod(lambda cls: InMemoryStore()))
    assert publish.main(ARGS) == 10


def test_main_usage_error_exits_1_and_notifies(monkeypatch):
    calls = []
    monkeypatch.setattr(publish, "send_failure", lambda url, payload: calls.append(payload))
    assert publish.main(["--project", "p"]) == 1
    assert calls and calls[0]["project"] == "p"


def test_main_invalid_source_platform_exits_1(monkeypatch):
    calls = []
    monkeypatch.setattr(publish, "send_failure", lambda url, payload: calls.append(payload))
    args = [a if a != "gitlab" else "github" for a in ARGS]
    assert publish.main(args) == 1
    assert calls


def _recording_scanner(seen):
    def scanner(tree, config, report, ignore=None):
        seen["ignore"] = None if ignore is None else Path(ignore).read_text()
        seen["tree_files"] = sorted(p.name for p in Path(tree).iterdir())
        return ScanResult(True, 0, report)
    return scanner


def test_project_and_admin_ignore_files_are_combined(tmp_path, make_repo):
    admin = tmp_path / "admin.ignore"
    admin.write_text("admin:rule:2\n")
    seen = {}
    _, store, _ = go(
        tmp_path, make_repo, scanner=_recording_scanner(seen), gitleaks_ignore=admin,
        files={"a.txt": "x", ".gitleaksignore": "proj:rule:1\n"},
    )
    lines = seen["ignore"].split()
    assert lines == ["proj:rule:1", "admin:rule:2"]
    assert ".gitleaksignore" not in seen["tree_files"]
    assert json.loads(store.get(manifest_key("proj", D)))["scan_result"] == "clean-with-reviewed-allowlist"


def test_project_ignore_without_trailing_newline_still_combines(tmp_path, make_repo):
    admin = tmp_path / "admin.ignore"
    admin.write_text("admin:rule:2\n")
    seen = {}
    go(tmp_path, make_repo, scanner=_recording_scanner(seen), gitleaks_ignore=admin,
       files={"a.txt": "x", ".gitleaksignore": "proj:rule:1"})
    assert seen["ignore"].split() == ["proj:rule:1", "admin:rule:2"]


def test_project_ignore_only_is_plain_clean(tmp_path, make_repo):
    seen = {}
    _, store, _ = go(tmp_path, make_repo, scanner=_recording_scanner(seen),
                     files={"a.txt": "x", ".gitleaksignore": "proj:rule:1\n"})
    assert seen["ignore"] == "proj:rule:1\n"
    assert json.loads(store.get(manifest_key("proj", D)))["scan_result"] == "clean"


def test_no_ignore_files_passes_none(tmp_path, make_repo):
    seen = {}
    go(tmp_path, make_repo, scanner=_recording_scanner(seen))
    assert seen["ignore"] is None


def test_untracked_gitleaksignore_not_used_in_archive_mode(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, {"a.txt": "x"})
    (repo / ".gitleaksignore").write_text("proj:rule:1\n")
    seen = {}
    publish_snapshot(mkreq(tmp_path, repo, sha), InMemoryStore(),
                     scanner=_recording_scanner(seen), scanner_version=lambda: "8", now=NOW)
    assert seen["ignore"] is None


def test_checkout_mode_uses_only_committed_gitleaksignore(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, {"a.txt": "x"})
    (repo / ".gitleaksignore").write_text("proj:rule:1\n")  # untracked
    seen = {}
    publish_snapshot(mkreq(tmp_path, repo, sha, mode="checkout"), InMemoryStore(),
                     scanner=_recording_scanner(seen), scanner_version=lambda: "8", now=NOW)
    assert seen["ignore"] is None
    git(repo, "add", ".gitleaksignore")  # staged but not committed
    publish_snapshot(mkreq(tmp_path, repo, sha, mode="checkout", workdir=tmp_path / "w2"),
                     InMemoryStore(), scanner=_recording_scanner(seen),
                     scanner_version=lambda: "8", now=NOW)
    assert seen["ignore"] is None


def test_checkout_mode_uses_committed_content_not_modified_worktree(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, {"a.txt": "x", ".gitleaksignore": "committed:rule:1\n"})
    (repo / ".gitleaksignore").write_text("tampered:rule:9\n")
    seen = {}
    publish_snapshot(mkreq(tmp_path, repo, sha, mode="checkout"), InMemoryStore(),
                     scanner=_recording_scanner(seen), scanner_version=lambda: "8", now=NOW)
    assert seen["ignore"] == "committed:rule:1\n"


@pytest.mark.parametrize("mode", ["archive", "checkout"])
def test_symlink_gitleaksignore_is_not_followed(tmp_path, make_repo, mode):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside:rule:1\n")
    repo, sha = make_repo(tmp_path, {"a.txt": "x"}, commit=False)
    (repo / ".gitleaksignore").symlink_to(outside)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "m")
    sha = git(repo, "rev-parse", "HEAD")
    seen = {}
    publish_snapshot(mkreq(tmp_path, repo, sha, mode=mode), InMemoryStore(),
                     scanner=_recording_scanner(seen), scanner_version=lambda: "8", now=NOW)
    assert seen["ignore"] is None


@pytest.mark.parametrize("mode", ["archive", "checkout"])
def test_directory_gitleaksignore_is_not_used(tmp_path, make_repo, mode):
    repo, sha = make_repo(tmp_path, {"a.txt": "x", ".gitleaksignore/entry": "y"})
    seen = {}
    publish_snapshot(mkreq(tmp_path, repo, sha, mode=mode), InMemoryStore(),
                     scanner=_recording_scanner(seen), scanner_version=lambda: "8", now=NOW)
    assert seen["ignore"] is None


@pytest.mark.parametrize("mode", ["archive", "checkout"])
def test_committed_gitleaksignore_used_in_both_modes(tmp_path, make_repo, mode):
    repo, sha = make_repo(tmp_path, {"a.txt": "x", ".gitleaksignore": "p:r:1\n"})
    seen = {}
    publish_snapshot(mkreq(tmp_path, repo, sha, mode=mode), InMemoryStore(),
                     scanner=_recording_scanner(seen), scanner_version=lambda: "8", now=NOW)
    assert seen["ignore"] == "p:r:1\n"


def _aws_repo_files(ignore: bool):
    key_id = "AKIA" + "QYLPMN5HFIQR7XYZ"
    files = {"cfg.py": f'aws_access_key_id = "{key_id}"\n'}
    if ignore:
        files[".gitleaksignore"] = "cfg.py:aws-access-token:1\n"
    return files


def _real(tmp_path, make_repo, files, **kw):
    from snapshotkit.scan import run_gitleaks
    repo, sha = make_repo(tmp_path, files)
    cfg = Path(__file__).parent.parent / "config" / "gitleaks.toml"
    store = Recording()
    req = mkreq(tmp_path, repo, sha, gitleaks_config=cfg, **kw)
    out = publish_snapshot(req, store, scanner=run_gitleaks, scanner_version=lambda: "8", now=NOW)
    return out, store, repo


@pytest.mark.gitleaks
@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_project_gitleaksignore_suppresses_finding_in_archive_mode(tmp_path, make_repo):
    out, store, _ = _real(tmp_path, make_repo, _aws_repo_files(True))
    assert out is Outcome.PUBLISHED
    assert json.loads(store.get(manifest_key("proj", D)))["scan_result"] == "clean"
    with tarfile.open(fileobj=io.BytesIO(store.get(snapshot_key("proj", D))), mode="r:gz") as t:
        names = t.getnames()
    assert "cfg.py" in names and ".gitleaksignore" not in names


@pytest.mark.gitleaks
@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_same_repo_without_ignore_file_has_secrets(tmp_path, make_repo):
    out, store, _ = _real(tmp_path, make_repo, _aws_repo_files(False))
    assert out is Outcome.SECRETS_FOUND and store.order == []


@pytest.mark.gitleaks
@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_untracked_gitleaksignore_in_archive_mode_still_finds_secrets(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, _aws_repo_files(False))
    (repo / ".gitleaksignore").write_text("cfg.py:aws-access-token:1\n")
    from snapshotkit.scan import run_gitleaks
    cfg = Path(__file__).parent.parent / "config" / "gitleaks.toml"
    out = publish_snapshot(mkreq(tmp_path, repo, sha, gitleaks_config=cfg), InMemoryStore(),
                           scanner=run_gitleaks, scanner_version=lambda: "8", now=NOW)
    assert out is Outcome.SECRETS_FOUND


@pytest.mark.parametrize("mode", ["archive", "checkout"])
def test_repo_with_symlink_outside_publishes_without_it(tmp_path, make_repo, mode):
    repo, _ = make_repo(tmp_path, {"a.txt": "x"}, commit=False)
    (repo / "etc").symlink_to("/etc/passwd")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "m")
    sha = git(repo, "rev-parse", "HEAD")
    store = InMemoryStore()
    out = publish_snapshot(mkreq(tmp_path, repo, sha, mode=mode), store,
                           scanner=clean_scanner, scanner_version=lambda: "8", now=NOW)
    assert out is Outcome.PUBLISHED
    with tarfile.open(fileobj=io.BytesIO(store.get(snapshot_key("proj", D))), mode="r:gz") as t:
        assert t.getnames() == ["a.txt"]
