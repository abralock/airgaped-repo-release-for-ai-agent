"""Find which image digests are deployed for a workload across clusters."""
from collections import Counter
from dataclasses import dataclass

from snapshotkit.clusters import ClusterConfig, ClusterReader, KubeClusterReader
from snapshotkit.digests import parse_image_id


@dataclass
class DeployedVersion:
    cluster: str
    namespace: str
    workload: str
    container: str
    image: str
    digest: str | None
    pods: int


@dataclass
class LookupResult:
    versions: list[DeployedVersion]
    errors: dict[str, str]


def find_deployed_version(
    readers: dict[str, ClusterReader],
    allowed_namespaces: dict[str, list[str]],
    namespace: str,
    workload: str,
    cluster: str | None = None,
) -> LookupResult:
    versions: list[DeployedVersion] = []
    errors: dict[str, str] = {}
    names = [cluster] if cluster is not None else sorted(readers)
    for name in names:
        reader = readers.get(name)
        if reader is None:
            errors[name] = "unknown cluster"
            continue
        if namespace not in allowed_namespaces.get(name, []):
            errors[name] = "namespace not allowed"
            continue
        try:
            pods = reader.workload_pods(namespace, workload)
        except Exception as e:  # noqa: BLE001 - one cluster must not hide the others
            errors[name] = f"{type(e).__name__}: {e}"
            continue
        counts: Counter = Counter()
        for pc in pods:
            counts[(pc.container, pc.image, parse_image_id(pc.image_id))] += 1
        for (container, image, digest), n in counts.items():
            versions.append(
                DeployedVersion(name, namespace, workload, container, image, digest, n)
            )
    versions.sort(key=lambda v: (v.cluster, v.container, v.digest or ""))
    return LookupResult(versions, errors)


def build_readers(
    configs: list[ClusterConfig],
) -> tuple[dict[str, ClusterReader], dict[str, list[str]]]:
    readers: dict[str, ClusterReader] = {c.name: KubeClusterReader(c) for c in configs}
    return readers, {c.name: list(c.namespaces) for c in configs}
