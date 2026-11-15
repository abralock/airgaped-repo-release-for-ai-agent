import json
from datetime import datetime, timedelta, timezone

from fakes import FakeClusterReader

from snapshotkit.manifest import Manifest
from snapshotkit.retention import apply_retention, main, plan_retention
from snapshotkit.store import (
    InMemoryStore,
    digest_index_key,
    manifest_key,
    snapshot_key,
    tag_index_key,
)

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)


def dg(n: int) -> str:
    return "sha256:" + f"{n:064x}"


def add(store, project, n, age_days, tag=None):
    d = dg(n)
    published = (NOW - timedelta(days=age_days, seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = Manifest(
        project=project,
        source_platform="gitlab",
        repo_path="g/r",
        image_repository=f"reg/{project}",
        image_tag=tag or f"t{n}",
        image_digest=d,
        pipeline_run_url="https://ci/1",
        published_at=published,
        scanner="gitleaks",
        scanner_version="8",
        scan_result="clean",
    )
    store.put(snapshot_key(project, d), b"tar")
    store.put(manifest_key(project, d), m.to_json())
    body = json.dumps({"project": project, "snapshot_digest": d}).encode()
    store.put(digest_index_key(d), body)
    store.put(tag_index_key(f"reg/{project}", m.image_tag), body)
    return d


class LogStore(InMemoryStore):
    def __init__(self):
        super().__init__()
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)
        super().delete(key)


def test_keeps_deployed_even_if_old():
    s = InMemoryStore()
    d = add(s, "a", 1, 200)
    for i in range(2, 23):
        add(s, "a", i, 100)  # 21 newer snapshots push d out of the newest 20
    alias = dg(9999)
    s.put(
        digest_index_key(alias),
        json.dumps({"project": "a", "snapshot_digest": d}).encode(),
    )
    assert ("a", d) not in plan_retention(s, {alias}, NOW)
    assert ("a", d) in plan_retention(s, set(), NOW)


def test_keeps_newest_20_per_project():
    s = InMemoryStore()
    ds = [add(s, "a", i, 100 + i) for i in range(1, 26)]
    for i in range(101, 104):
        add(s, "b", i, 200 + i)
    doomed = plan_retention(s, set(), NOW)
    assert sorted(doomed) == sorted(("a", d) for d in ds[20:])


def test_keeps_younger_than_90_days():
    s = InMemoryStore()
    for i in range(1, 26):
        add(s, "a", i, 30)
    assert plan_retention(s, set(), NOW) == []


def test_unparseable_manifest_never_doomed(caplog):
    s = InMemoryStore()
    s.put(manifest_key("a", dg(1)), b"not json")
    assert plan_retention(s, set(), NOW) == []
    assert manifest_key("a", dg(1)) in caplog.text


def test_apply_deletes_index_before_objects():
    s = LogStore()
    d = add(s, "a", 1, 200)
    keep = add(s, "a", 2, 200)
    bad = digest_index_key(dg(77))
    s.put(bad, b"garbage")
    apply_retention(s, [("a", d)])
    assert s.deleted[-2:] == [manifest_key("a", d), snapshot_key("a", d)]
    idx = s.deleted[:-2]
    assert sorted(idx) == sorted([digest_index_key(d), tag_index_key("reg/a", "t1")])
    assert all(k.startswith("index/") for k in idx)
    assert s.get(bad) == b"garbage"
    assert s.get(snapshot_key("a", keep)) is not None
    assert s.get(digest_index_key(keep)) is not None


def _env(monkeypatch, tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("x")
    monkeypatch.setenv("SNAPSHOT_TOOL_CLUSTERS", str(cfg))


def _old_store():
    s = LogStore()
    for i in range(1, 26):
        add(s, "a", i, 100 + i)
    return s


def test_unreachable_cluster_deletes_nothing(capsys):
    s = _old_store()
    before = dict(s._objects)
    readers = {
        "ok": FakeClusterReader(image_ids=set()),
        "bad": FakeClusterReader(raises=RuntimeError("timeout")),
    }
    assert main([], store=s, readers=readers) == 2
    assert s._objects == before and s.deleted == []
    assert "bad" in capsys.readouterr().err


def test_dry_run_deletes_nothing(capsys):
    s = _old_store()
    before = dict(s._objects)
    readers = {"ok": FakeClusterReader()}
    assert main(["--dry-run"], store=s, readers=readers) == 0
    assert s._objects == before
    out = capsys.readouterr().out
    assert dg(25) in out and snapshot_key("a", dg(25)) in out


def test_main_deletes_and_summarizes(capsys):
    s = _old_store()
    readers = {"ok": FakeClusterReader(image_ids={dg(25)})}
    assert main([], store=s, readers=readers) == 0
    assert "kept 21, deleted 4" in capsys.readouterr().out
    assert s.get(snapshot_key("a", dg(24))) is None
    assert s.get(snapshot_key("a", dg(25))) is not None
