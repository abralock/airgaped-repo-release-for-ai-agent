"""Cluster configuration and read-only Kubernetes access."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

from snapshotkit.digests import parse_image_id


@dataclass
class ClusterConfig:
    name: str
    server: str
    ca_file: Path
    token_file: Path
    namespaces: list[str]


def load_cluster_configs(path: Path) -> list[ClusterConfig]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    entries = data.get("clusters") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("clusters config must have a non-empty 'clusters' list")
    configs: list[ClusterConfig] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each cluster entry must be a mapping")
        for field in ("name", "server", "ca_file", "token_file", "namespaces"):
            if not entry.get(field):
                raise ValueError(f"cluster entry missing required field: {field}")
        name = entry["name"]
        if name in seen:
            raise ValueError(f"duplicate cluster name: {name}")
        seen.add(name)
        if not str(entry["server"]).startswith("https://"):
            raise ValueError(f"cluster {name}: server must start with https://")
        namespaces = entry["namespaces"]
        if not isinstance(namespaces, list) or not all(
            isinstance(n, str) and n for n in namespaces
        ):
            raise ValueError(f"cluster {name}: namespaces must be a list of names")
        configs.append(
            ClusterConfig(
                name=name,
                server=entry["server"],
                ca_file=Path(entry["ca_file"]),
                token_file=Path(entry["token_file"]),
                namespaces=list(namespaces),
            )
        )
    return configs


@dataclass
class PodContainer:
    pod: str
    container: str
    image: str
    image_id: str


class ClusterReader(Protocol):
    def workload_pods(self, namespace: str, workload: str) -> list[PodContainer]: ...

    def all_image_ids(self) -> set[str]: ...


def _selector_string(selector) -> str:
    parts: list[str] = []
    for key, value in sorted((selector.match_labels or {}).items()):
        parts.append(f"{key}={value}")
    for expr in selector.match_expressions or []:
        op = expr.operator
        values = ",".join(expr.values or [])
        if op == "In":
            parts.append(f"{expr.key} in ({values})")
        elif op == "NotIn":
            parts.append(f"{expr.key} notin ({values})")
        elif op == "Exists":
            parts.append(expr.key)
        elif op == "DoesNotExist":
            parts.append(f"!{expr.key}")
        else:
            raise ValueError(f"unsupported selector operator: {op}")
    return ",".join(parts)


class KubeClusterReader:
    def __init__(self, config: ClusterConfig, timeout_seconds: int = 10):
        self.config = config
        self.timeout = timeout_seconds

    def _api_client(self) -> client.ApiClient:
        cfg = client.Configuration()
        cfg.host = self.config.server
        cfg.ssl_ca_cert = str(self.config.ca_file)
        token = self.config.token_file.read_text().strip()
        cfg.api_key = {"authorization": f"Bearer {token}"}
        # urllib3 retries 3 times by default, so an unreachable cluster would cost
        # 4 x timeout per call. Fail after one attempt; callers report the error.
        cfg.retries = 0
        return client.ApiClient(cfg)

    def _apis(self):
        api = self._api_client()
        return client.AppsV1Api(api), client.CoreV1Api(api)

    def workload_pods(self, namespace: str, workload: str) -> list[PodContainer]:
        apps, core = self._apis()
        t = self.timeout
        lookups = (
            apps.read_namespaced_deployment,
            apps.read_namespaced_stateful_set,
            apps.read_namespaced_daemon_set,
        )
        obj = None
        for read in lookups:
            try:
                obj = read(workload, namespace, _request_timeout=t)
                break
            except ApiException as e:
                if e.status != 404:
                    raise
        if obj is None:
            return []
        selector = _selector_string(obj.spec.selector)
        pods = core.list_namespaced_pod(
            namespace, label_selector=selector, _request_timeout=t
        )
        return [pc for pod in pods.items for pc in _containers(pod)]

    def all_image_ids(self) -> set[str]:
        _, core = self._apis()
        ids: set[str] = set()
        for ns in self.config.namespaces:
            pods = core.list_namespaced_pod(ns, _request_timeout=self.timeout)
            for pod in pods.items:
                for pc in _containers(pod):
                    digest = parse_image_id(pc.image_id)
                    if digest:
                        ids.add(digest)
        return ids


def _containers(pod) -> list[PodContainer]:
    name = pod.metadata.name
    statuses = pod.status.container_statuses
    if statuses is None:
        return [
            PodContainer(name, c.name, c.image, "") for c in pod.spec.containers
        ]
    spec_images = {c.name: c.image for c in pod.spec.containers or []}
    return [
        PodContainer(
            name, s.name, s.image or spec_images.get(s.name, ""), s.image_id or ""
        )
        for s in statuses
    ]
