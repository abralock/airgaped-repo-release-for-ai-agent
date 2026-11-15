from fakes import FakeClusterReader

from snapshotkit.clusters import PodContainer
from snapshotkit.lookup import find_deployed_version

X = "sha256:" + "a" * 64
Y = "sha256:" + "b" * 64
KEY = ("shop", "web")
ALLOWED = {"prod-a": ["shop"], "prod-b": ["shop"]}


def pc(pod, digest, container="app"):
    return PodContainer(pod, container, "reg/app:1", f"docker-pullable://reg/app@{digest}" if digest else "")


def test_groups_pods_by_digest():
    r = FakeClusterReader({KEY: [pc("p1", X), pc("p2", X), pc("p3", X)]})
    res = find_deployed_version({"prod-a": r}, ALLOWED, "shop", "web")
    assert len(res.versions) == 1
    assert (res.versions[0].digest, res.versions[0].pods) == (X, 3)
    assert res.errors == {}


def test_mid_rollout_returns_both():
    r = FakeClusterReader({KEY: [pc("p1", X), pc("p2", X), pc("p3", Y)]})
    res = find_deployed_version({"prod-a": r}, ALLOWED, "shop", "web")
    assert {(v.digest, v.pods) for v in res.versions} == {(X, 2), (Y, 1)}


def test_starting_pod_has_none_digest():
    r = FakeClusterReader({KEY: [pc("p1", None)]})
    res = find_deployed_version({"prod-a": r}, ALLOWED, "shop", "web")
    assert res.versions[0].digest is None


def test_all_clusters_when_cluster_omitted():
    readers = {
        "prod-a": FakeClusterReader({KEY: [pc("p1", X)]}),
        "prod-b": FakeClusterReader({KEY: [pc("p1", Y)]}),
    }
    res = find_deployed_version(readers, ALLOWED, "shop", "web")
    assert [(v.cluster, v.digest) for v in res.versions] == [("prod-a", X), ("prod-b", Y)]


def test_unreachable_cluster_reported():
    readers = {
        "prod-a": FakeClusterReader({KEY: [pc("p1", X)]}),
        "prod-b": FakeClusterReader(raises=TimeoutError("timed out")),
    }
    res = find_deployed_version(readers, ALLOWED, "shop", "web")
    assert [v.cluster for v in res.versions] == ["prod-a"]
    assert list(res.errors) == ["prod-b"] and "TimeoutError" in res.errors["prod-b"]


def test_unknown_cluster():
    res = find_deployed_version({"prod-a": FakeClusterReader()}, ALLOWED, "shop", "web", cluster="nope")
    assert res.errors == {"nope": "unknown cluster"} and res.versions == []


def test_namespace_not_allowed():
    r = FakeClusterReader({("secret", "web"): [pc("p1", X)]})
    res = find_deployed_version({"prod-a": r}, ALLOWED, "secret", "web")
    assert res.errors == {"prod-a": "namespace not allowed"}
    assert r.calls == []
