import json
import threading
from pathlib import Path

import pytest

from snapshotkit.store import (
    InMemoryStore,
    digest_index_key,
    manifest_key,
    snapshot_key,
    tag_index_key,
)
from snapshotkit.snapshots import SnapshotCache, SnapshotNotFound
from snapshotkit.tarball import Entry, write_normalized_targz

PRIMARY = "sha256:" + "a" * 64
ALIAS = "sha256:" + "b" * 64
PRIMARY_ID = "sha256-" + "a" * 64
MSG = "No snapshot available for this version."


class CountingStore(InMemoryStore):
    def __init__(self):
        super().__init__()
        self.gets: dict[str, int] = {}

    def get(self, key):
        self.gets[key] = self.gets.get(key, 0) + 1
        return super().get(key)


def _index(project="proj", digest=PRIMARY):
    return json.dumps({"project": project, "snapshot_digest": digest}).encode()


@pytest.fixture
def store(tmp_path):
    s = CountingStore()
    tar = tmp_path / "s.tar.gz"
    write_normalized_targz([Entry("src/app.py", 0o644, b"print(1)\n")], tar)
    s.put(snapshot_key("proj", PRIMARY), tar.read_bytes())
    s.put(manifest_key("proj", PRIMARY), json.dumps({"project": "proj"}).encode())
    s.put(digest_index_key(PRIMARY), _index())
    s.put(digest_index_key(ALIAS), _index())
    s.put(tag_index_key("reg.local/team/app", "v1"), _index())
    return s


@pytest.fixture
def cache(store, tmp_path):
    return SnapshotCache(store, tmp_path / "cache")


def test_resolves_alias_digest_to_primary(cache):
    info = cache.get_snapshot(ALIAS)
    assert info.snapshot_id == PRIMARY_ID
    assert info.project == "proj"
    assert info.manifest == {"project": "proj"}
    assert (info.path / "src" / "app.py").read_text() == "print(1)\n"
    assert cache.path_for(PRIMARY_ID) == info.path


def test_resolves_tag_ref(cache):
    assert cache.get_snapshot("reg.local/team/app:v1").snapshot_id == PRIMARY_ID


def test_registry_port_not_mistaken_for_tag(cache):
    with pytest.raises(SnapshotNotFound):
        cache.get_snapshot("reg.local:5000/team/app")


def test_missing_index_raises_not_found(cache):
    with pytest.raises(SnapshotNotFound) as e:
        cache.get_snapshot("sha256:" + "c" * 64)
    assert str(e.value) == MSG


@pytest.mark.parametrize("ref", ["", "garbage", "sha256:xyz", "a/b:"])
def test_bad_ref_not_found(cache, ref):
    with pytest.raises(SnapshotNotFound):
        cache.get_snapshot(ref)


def test_index_present_tar_missing_raises_not_found(cache, store):
    store.delete(snapshot_key("proj", PRIMARY))
    with pytest.raises(SnapshotNotFound):
        cache.get_snapshot(PRIMARY)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"project": "proj", "snapshot_digest": "nope"}).encode(),
        json.dumps({"project": "../x", "snapshot_digest": PRIMARY}).encode(),
        json.dumps({"snapshot_digest": PRIMARY}).encode(),
    ],
)
def test_malformed_index_not_found(cache, store, body):
    store.put(digest_index_key(ALIAS), body)
    with pytest.raises(SnapshotNotFound):
        cache.get_snapshot(ALIAS)


def test_malformed_manifest_not_found(cache, store):
    store.put(manifest_key("proj", PRIMARY), b"{nope")
    with pytest.raises(SnapshotNotFound):
        cache.get_snapshot(PRIMARY)


def test_corrupt_tar_not_found_and_cleaned(cache, store, tmp_path):
    store.put(snapshot_key("proj", PRIMARY), b"this is not a tarball")
    with pytest.raises(SnapshotNotFound):
        cache.get_snapshot(PRIMARY)
    assert list((tmp_path / "cache").iterdir()) == []


def test_unsafe_tar_not_found(cache, store, tmp_path):
    bad = tmp_path / "bad.tar.gz"
    write_normalized_targz([Entry("../evil", 0o644, b"x")], bad)
    store.put(snapshot_key("proj", PRIMARY), bad.read_bytes())
    with pytest.raises(SnapshotNotFound):
        cache.get_snapshot(PRIMARY)
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.parametrize("sid", ["../etc", "sha256-abc", "sha256:" + "a" * 64, ""])
def test_path_for_validates_id(cache, sid):
    with pytest.raises(SnapshotNotFound):
        cache.path_for(sid)


def test_path_for_unknown_snapshot_not_found(cache):
    with pytest.raises(SnapshotNotFound):
        cache.path_for("sha256-" + "c" * 64)


def test_path_for_never_fetched_snapshot_fetches_it(cache):
    path = cache.path_for(PRIMARY_ID)
    assert (path / "src" / "app.py").read_text() == "print(1)\n"


def test_path_for_refetches_after_cache_loss(cache, store, tmp_path):
    import shutil
    info = cache.get_snapshot(PRIMARY)
    shutil.rmtree(tmp_path / "cache")  # pod restart / another replica
    assert cache.path_for(PRIMARY_ID) == info.path
    assert (info.path / "src" / "app.py").read_text() == "print(1)\n"
    assert store.gets[snapshot_key("proj", PRIMARY)] == 2


def test_path_for_alias_id_is_not_a_snapshot_id(cache):
    with pytest.raises(SnapshotNotFound):
        cache.path_for("sha256-" + "b" * 64)


def test_second_call_uses_cache(cache, store):
    cache.get_snapshot(PRIMARY)
    cache.get_snapshot(ALIAS)
    assert store.gets[snapshot_key("proj", PRIMARY)] == 1


def test_concurrent_extract_is_atomic(store, tmp_path):
    root = tmp_path / "cache"
    results, errors = [], []
    barrier = threading.Barrier(2)

    def run():
        try:
            barrier.wait()
            results.append(SnapshotCache(store, root).get_snapshot(PRIMARY))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(results) == 2
    assert [p.name for p in root.iterdir()] == [PRIMARY_ID]
    assert not any(".tmp" in p.name for p in root.iterdir())


def _digest(c):
    return "sha256:" + c * 64


def _sized_store(tmp_path, sizes: dict[str, int]):
    s = CountingStore()
    for c, n in sizes.items():
        d = _digest(c)
        tar = tmp_path / f"{c}.tar.gz"
        write_normalized_targz([Entry("a.bin", 0o644, b"x" * n), Entry("d/b.bin", 0o644, b"y" * n)], tar)
        s.put(snapshot_key("proj", d), tar.read_bytes())
        s.put(manifest_key("proj", d), json.dumps({"project": "proj"}).encode())
        s.put(digest_index_key(d), _index(digest=d))
    return s


def _cached_ids(root):
    return sorted(p.name[len("sha256-"):][0] for p in root.iterdir())


def _du(root):
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def test_eviction_keeps_cache_under_budget(tmp_path):
    root = tmp_path / "cache"
    cache = SnapshotCache(_sized_store(tmp_path, {"c": 500, "d": 500, "e": 500}), root,
                          max_bytes=2500)
    for c in "cde":
        cache.get_snapshot(_digest(c))
        assert _du(root) <= 2500
    assert _cached_ids(root) == ["d", "e"]
    assert not any(".evict" in p.name or ".tmp" in p.name for p in root.iterdir())


def test_eviction_is_least_recently_used(tmp_path):
    root = tmp_path / "cache"
    store = _sized_store(tmp_path, {"c": 500, "d": 500, "e": 500})
    cache = SnapshotCache(store, root, max_bytes=2500)
    cache.get_snapshot(_digest("c"))
    cache.get_snapshot(_digest("d"))
    cache.path_for("sha256-" + "c" * 64)  # c is now more recent than d
    cache.get_snapshot(_digest("e"))
    assert _cached_ids(root) == ["c", "e"]
    cache.path_for("sha256-" + "d" * 64)  # evicted, fetched again; c is now oldest
    assert _cached_ids(root) == ["d", "e"]
    assert store.gets[snapshot_key("proj", _digest("d"))] == 2


def test_snapshot_larger_than_budget_is_still_served(tmp_path):
    root = tmp_path / "cache"
    cache = SnapshotCache(_sized_store(tmp_path, {"c": 500, "d": 5000}), root, max_bytes=2500)
    cache.get_snapshot(_digest("c"))
    info = cache.get_snapshot(_digest("d"))
    assert _cached_ids(root) == ["d"] and (info.path / "a.bin").stat().st_size == 5000


def test_existing_cache_dirs_count_toward_budget(tmp_path):
    root = tmp_path / "cache"
    store = _sized_store(tmp_path, {"c": 500, "d": 500, "e": 500})
    SnapshotCache(store, root).get_snapshot(_digest("c"))
    cache = SnapshotCache(store, root, max_bytes=2500)  # restarted with a warm cache
    cache.get_snapshot(_digest("d"))
    cache.get_snapshot(_digest("e"))
    assert _cached_ids(root) == ["d", "e"]


def test_default_budget_is_4_gib(tmp_path):
    import inspect
    assert inspect.signature(SnapshotCache).parameters["max_bytes"].default == 4 * 1024**3
