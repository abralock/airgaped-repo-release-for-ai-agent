import ipaddress
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"

_REQUIRE = bool(os.environ.get("CI")) or os.environ.get("SNAPSHOT_TEST_REQUIRE_KUBECTL") == "1"
_HAVE_KUBECTL = shutil.which("kubectl") is not None
if _REQUIRE and not _HAVE_KUBECTL:

    def needs_kubectl(fn):
        def fail(*a, **k):
            pytest.fail("kubectl is required (CI or SNAPSHOT_TEST_REQUIRE_KUBECTL=1) but missing")

        fail.__name__ = fn.__name__
        return fail

else:
    needs_kubectl = pytest.mark.skipif(
        not _HAVE_KUBECTL, reason="kubectl not installed; cannot render kustomize"
    )


def render(path: Path) -> list[dict]:
    out = subprocess.run(
        ["kubectl", "kustomize", str(path)], capture_output=True, text=True, check=True
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


def named(docs: list[dict], kind: str, name: str) -> dict:
    (doc,) = [d for d in of_kind(docs, kind) if d["metadata"]["name"] == name]
    return doc


@pytest.fixture(scope="module")
def rbac():
    return render(DEPLOY / "prod-cluster-rbac" / "overlays" / "example")


@pytest.fixture(scope="module")
def agent():
    return render(DEPLOY / "agent-cluster" / "overlays" / "example")


# ---------- production RBAC ----------


@needs_kubectl
def test_clusterrole_is_exactly_get_list_on_workloads(rbac):
    role = named(rbac, "ClusterRole", "snapshot-reader")
    rules = role["rules"]
    assert len(rules) == 2
    by_group = {r["apiGroups"][0]: r for r in rules}
    assert set(by_group[""]["resources"]) == {"pods"}
    assert set(by_group["apps"]["resources"]) == {
        "deployments",
        "statefulsets",
        "daemonsets",
        "replicasets",
    }
    for r in rules:
        assert r["verbs"] == ["get", "list"]
        assert not {"secrets", "configmaps"} & set(r["resources"])


@needs_kubectl
def test_no_clusterrolebinding_and_rolebindings_reference_clusterrole(rbac):
    assert of_kind(rbac, "ClusterRoleBinding") == []
    bindings = of_kind(rbac, "RoleBinding")
    assert bindings
    for b in bindings:
        assert b["roleRef"] == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "snapshot-reader",
        }
        assert b["metadata"]["namespace"] != "snapshot-reader"


@needs_kubectl
def test_reader_identity_objects(rbac):
    ns = named(rbac, "Namespace", "snapshot-reader")
    assert ns
    sa = named(rbac, "ServiceAccount", "snapshot-reader")
    assert sa["metadata"]["namespace"] == "snapshot-reader"
    secret = named(rbac, "Secret", "snapshot-reader-token")
    assert secret["type"] == "kubernetes.io/service-account-token"
    assert secret["metadata"]["annotations"]["kubernetes.io/service-account.name"] == (
        "snapshot-reader"
    )


# ---------- agent cluster ----------


def _tool_pod(agent):
    return named(agent, "Deployment", "snapshot-mcp")["spec"]["template"]["spec"]


@needs_kubectl
def test_tool_deployment_hardening(agent):
    pod = _tool_pod(agent)
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    (c,) = pod["containers"]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    cache = [v for v in pod["volumes"] if v["name"] == "cache"][0]
    assert cache["emptyDir"]["sizeLimit"] == "5Gi"


@needs_kubectl
def test_tool_mounts_cluster_secret_with_items(agent):
    pod = _tool_pod(agent)
    (c,) = pod["containers"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["SNAPSHOT_TOOL_CLUSTERS"] == "/etc/snapshotkit/clusters/clusters.yaml"
    vol = [v for v in pod["volumes"] if v["name"] == "clusters"][0]
    assert vol["secret"]["secretName"] == "snapshot-clusters"
    paths = {i["path"] for i in vol["secret"]["items"]}
    assert {"clusters.yaml", "prod-a/ca.crt", "prod-a/token"} <= paths
    assert {"secretRef": {"name": "snapshot-store-reader"}} in c["envFrom"]


def _pod_specs(agent):
    for doc in of_kind(agent, "Deployment"):
        yield doc["spec"]["template"]["spec"]
    for doc in of_kind(agent, "CronJob"):
        yield doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]


@needs_kubectl
def test_no_git_platform_env_names(agent):
    forbidden = ("AZURE", "ADO", "GITLAB", "GIT_")
    for pod in _pod_specs(agent):
        for c in pod["containers"]:
            for e in c.get("env", []):
                assert not any(f in e["name"] for f in forbidden), e["name"]
            for ref in c.get("envFrom", []):
                name = ref["secretRef"]["name"].upper()
                assert not any(f in name for f in forbidden), name
            assert {r["secretRef"]["name"] for r in c["envFrom"]} <= {
                "snapshot-store-reader",
                "snapshot-store-admin",
            }


def _norm(rule: dict) -> tuple:
    peers = []
    for t in rule["to"]:
        if "ipBlock" in t:
            peers.append(("cidr", t["ipBlock"]["cidr"]))
        else:
            peers.append(
                (
                    "pods",
                    json.dumps(t["namespaceSelector"]["matchLabels"], sort_keys=True),
                    json.dumps(t["podSelector"]["matchLabels"], sort_keys=True),
                )
            )
    ports = [(p["protocol"], p["port"]) for p in rule["ports"]]
    return (tuple(sorted(peers)), tuple(sorted(ports)))


def _check_closed(policy: dict):
    for rule in policy["spec"]["egress"]:
        assert rule.get("to"), "egress rule without peers allows everything"
        assert rule.get("ports"), "egress rule without ports allows every port"
        for t in rule["to"]:
            if "ipBlock" in t:
                net = ipaddress.ip_network(t["ipBlock"]["cidr"])
                assert net.prefixlen > 0, t
            else:
                assert t.get("namespaceSelector", {}).get("matchLabels")
                assert t.get("podSelector", {}).get("matchLabels")


DNS = (
    (
        (
            "pods",
            '{"kubernetes.io/metadata.name": "kube-system"}',
            '{"k8s-app": "kube-dns"}',
        ),
    ),
    (("TCP", 53), ("UDP", 53)),
)


@needs_kubectl
def test_network_policies_have_no_open_egress(agent):
    policies = of_kind(agent, "NetworkPolicy")
    assert len(policies) == 2
    for p in policies:
        assert set(p["spec"]["policyTypes"]) >= {"Egress"}
        _check_closed(p)


@needs_kubectl
def test_tool_egress_is_exactly_dns_store_and_api_servers(agent):
    p = named(agent, "NetworkPolicy", "snapshot-tool")
    got = {_norm(r) for r in p["spec"]["egress"]}
    expected = {
        DNS,
        ((("cidr", "10.20.0.5/32"),), (("TCP", 9000),)),
        ((("cidr", "10.30.0.10/32"), ("cidr", "10.31.0.10/32")), (("TCP", 6443),)),
    }
    assert got == expected
    assert len(p["spec"]["egress"]) == 3


@needs_kubectl
def test_agent_egress_is_exactly_tool_dns_and_model(agent):
    p = named(agent, "NetworkPolicy", "agent-egress")
    got = {_norm(r) for r in p["spec"]["egress"]}
    expected = {
        (
            (
                (
                    "pods",
                    '{"kubernetes.io/metadata.name": "code-snapshots"}',
                    '{"app.kubernetes.io/name": "snapshot-mcp"}',
                ),
            ),
            (("TCP", 8080),),
        ),
        DNS,
        ((("cidr", "10.40.0.0/28"),), (("TCP", 443),)),
    }
    assert got == expected
    assert len(p["spec"]["egress"]) == 3


@needs_kubectl
def test_tool_ingress_only_from_agent_label(agent):
    p = named(agent, "NetworkPolicy", "snapshot-tool")
    assert set(p["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    (rule,) = p["spec"]["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8080}]
    (peer,) = rule["from"]
    assert peer["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "ai-agent"
    }
    assert peer["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "ai-agent"}


@needs_kubectl
def test_tool_policy_covers_retention_pods(agent):
    tool_policy = named(agent, "NetworkPolicy", "snapshot-tool")
    sel = tool_policy["spec"]["podSelector"]["matchLabels"]
    tool_labels = named(agent, "Deployment", "snapshot-mcp")["spec"]["template"]["metadata"][
        "labels"
    ]
    cron = named(agent, "CronJob", "snapshot-retention")
    cron_labels = cron["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    for k, v in sel.items():
        assert tool_labels[k] == v
        assert cron_labels[k] == v


@needs_kubectl
def test_agent_policy_limits_egress(agent):
    p = named(agent, "NetworkPolicy", "agent-egress")
    assert p["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "ai-agent"}
    assert p["metadata"]["namespace"] == "ai-agent"
    assert p["spec"]["policyTypes"] == ["Egress"]
    egress = p["spec"]["egress"]
    assert len(egress) == 3  # tool, DNS, model endpoint
    tool_rule = [r for r in egress if r["ports"] == [{"protocol": "TCP", "port": 8080}]]
    assert len(tool_rule) == 1
    peer = tool_rule[0]["to"][0]
    assert peer["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "snapshot-mcp"}
    assert "0.0.0.0/0" not in json.dumps(p)


@needs_kubectl
def test_retention_cronjob(agent):
    cj = named(agent, "CronJob", "snapshot-retention")
    assert cj["spec"]["schedule"] == "0 3 * * *"
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    pod = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    (c,) = pod["containers"]
    assert c["command"] == ["snapshot-retention"]
    assert {"secretRef": {"name": "snapshot-store-admin"}} in c["envFrom"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["automountServiceAccountToken"] is False
    vol = [v for v in pod["volumes"] if v["name"] == "clusters"][0]
    assert vol["secret"]["secretName"] == "snapshot-clusters"


@needs_kubectl
def test_bases_render_on_their_own():
    assert render(DEPLOY / "agent-cluster" / "base")
    assert render(DEPLOY / "prod-cluster-rbac" / "base")


# ---------- store policies ----------

POLICIES = DEPLOY / "store" / "policies"


def _statements(path: Path) -> list[dict]:
    return json.loads(path.read_text())["Statement"]


def test_writer_policy_template_allows_only_put_object():
    text = (POLICIES / "pipeline-writer.template.json").read_text()
    stmts = json.loads(text)["Statement"]
    actions = {a for s in stmts for a in s["Action"]}
    assert actions == {"s3:PutObject"}
    resources = {r for s in stmts for r in s["Resource"]}
    assert any(r.endswith("code-snapshots/snapshots/PROJECT/*") for r in resources)
    assert any(r.endswith("code-snapshots/index/*") for r in resources)
    assert len(resources) == 2


def test_reader_policy_is_read_only():
    stmts = _statements(POLICIES / "tool-reader.json")
    actions = {a for s in stmts for a in s["Action"]}
    assert actions == {"s3:GetObject", "s3:ListBucket"}
    assert all(s["Effect"] == "Allow" for s in stmts)


def test_retention_policy_has_delete_but_no_put():
    actions = {a for s in _statements(POLICIES / "retention-admin.json") for a in s["Action"]}
    assert "s3:DeleteObject" in actions
    assert "s3:PutObject" not in actions


def _make(name: str):
    return subprocess.run(
        ["bash", str(DEPLOY / "store" / "make-writer-policy.sh"), name],
        capture_output=True,
        text=True,
    )


def test_make_writer_policy_substitutes_project():
    r = _make("orders-api")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    resources = {x for s in doc["Statement"] for x in s["Resource"]}
    assert any(x.endswith("code-snapshots/snapshots/orders-api/*") for x in resources)
    assert "PROJECT" not in r.stdout


@pytest.mark.parametrize("bad", ["Orders", "a/b", "../x", "-a", "", "a b", "x" * 101, 'a"b'])
def test_make_writer_policy_rejects_bad_names(bad):
    r = _make(bad)
    assert r.returncode != 0
    assert r.stdout == ""
