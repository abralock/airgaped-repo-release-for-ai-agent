from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from snapshotkit.clusters import KubeClusterReader, load_cluster_configs

EXAMPLE = Path(__file__).parent.parent / "deploy/agent-cluster/clusters-config.example.yaml"
D = "sha256:" + "a" * 64


def _write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return p


def _cluster(name="a", ns="[x]", server="https://k"):
    return f"- name: {name}\n  server: {server}\n  ca_file: /ca\n  token_file: /t\n  namespaces: {ns}\n"


def test_load_cluster_configs_validation(tmp_path):
    cfgs = load_cluster_configs(EXAMPLE)
    assert [c.name for c in cfgs] == ["prod-a", "prod-b"]
    assert cfgs[0].ca_file == Path("/etc/snapshotkit/clusters/prod-a/ca.crt")
    with pytest.raises(ValueError):
        load_cluster_configs(_write(tmp_path, "clusters:\n" + _cluster() + _cluster()))
    with pytest.raises(ValueError):
        load_cluster_configs(_write(tmp_path, "clusters:\n" + _cluster(ns="[]")))
    with pytest.raises(ValueError):
        load_cluster_configs(_write(tmp_path, "clusters:\n- name: a\n  server: https://k\n"))
    with pytest.raises(ValueError):
        load_cluster_configs(_write(tmp_path, "clusters:\n" + _cluster(server="http://k")))


def _reader(tmp_path):
    tok = tmp_path / "token"
    tok.write_text("s3cret\n")
    cfg = load_cluster_configs(EXAMPLE)[0]
    cfg.token_file = tok
    return KubeClusterReader(cfg)


def _pod(name, image_id=D, statuses=True):
    pod = MagicMock()
    pod.metadata.name = name
    if statuses:
        st = MagicMock()
        st.name, st.image, st.image_id = "app", "reg/app:1", image_id
        pod.status.container_statuses = [st]
    else:
        pod.status.container_statuses = None
        c = MagicMock()
        c.name, c.image = "app", "reg/app:1"
        pod.spec.containers = [c]
    return pod


def test_kube_reader_uses_selector_and_timeout(tmp_path):
    reader = _reader(tmp_path)
    apps, core = MagicMock(), MagicMock()
    sel = apps.read_namespaced_deployment.return_value.spec.selector
    sel.match_labels = {"app": "web"}
    expr = MagicMock()
    expr.key, expr.operator, expr.values = "tier", "In", ["a", "b"]
    sel.match_expressions = [expr]
    core.list_namespaced_pod.return_value.items = [_pod("p1"), _pod("p2", statuses=False)]
    with patch("snapshotkit.clusters.client.AppsV1Api", return_value=apps), patch(
        "snapshotkit.clusters.client.CoreV1Api", return_value=core
    ):
        out = reader.workload_pods("shop", "web")
    core.list_namespaced_pod.assert_called_once_with(
        "shop", label_selector="app=web,tier in (a,b)", _request_timeout=10
    )
    assert apps.read_namespaced_deployment.call_args.kwargs["_request_timeout"] == 10
    assert [(p.pod, p.image_id) for p in out] == [("p1", D), ("p2", "")]


def test_falls_back_to_statefulset_then_empty(tmp_path):
    reader = _reader(tmp_path)
    apps, core = MagicMock(), MagicMock()
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    apps.read_namespaced_stateful_set.side_effect = ApiException(status=404)
    apps.read_namespaced_daemon_set.side_effect = ApiException(status=404)
    with patch("snapshotkit.clusters.client.AppsV1Api", return_value=apps), patch(
        "snapshotkit.clusters.client.CoreV1Api", return_value=core
    ):
        assert reader.workload_pods("shop", "nope") == []
    core.list_namespaced_pod.assert_not_called()


def test_non_404_error_propagates(tmp_path):
    reader = _reader(tmp_path)
    apps = MagicMock()
    apps.read_namespaced_deployment.side_effect = ApiException(status=403)
    with patch("snapshotkit.clusters.client.AppsV1Api", return_value=apps), patch(
        "snapshotkit.clusters.client.CoreV1Api", return_value=MagicMock()
    ):
        with pytest.raises(ApiException):
            reader.workload_pods("shop", "web")


def test_all_image_ids_parses_and_drops_unparseable(tmp_path):
    reader = _reader(tmp_path)
    core = MagicMock()
    core.list_namespaced_pod.return_value.items = [
        _pod("p1", f"docker-pullable://reg/app@{D}"), _pod("p2", ""), _pod("p3", statuses=False)
    ]
    with patch("snapshotkit.clusters.client.AppsV1Api"), patch(
        "snapshotkit.clusters.client.CoreV1Api", return_value=core
    ):
        assert reader.all_image_ids() == {D}
    assert core.list_namespaced_pod.call_count == 2  # one per configured namespace
