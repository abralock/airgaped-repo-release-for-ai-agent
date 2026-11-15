"""Find which image digests are deployed for a workload across clusters."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
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


def _versions_in(name: str, namespace: str, workload: str, pods) -> list[DeployedVersion]:
    counts: Counter = Counter()
    images: dict[tuple[str, str | None], str] = {}
    for pc in pods:
        key = (pc.container, parse_image_id(pc.image_id))
        counts[key] += 1
        if pc.image and not images.get(key):
            images[key] = pc.image
        images.setdefault(key, "")
    return [
        DeployedVersion(name, namespace, workload, container, images[(container, digest)], digest, n)
        for (container, digest), n in counts.items()
    ]


def find_deployed_version(
    readers: dict[str, ClusterReader],
    allowed_namespaces: dict[str, list[str]],
    namespace: str,
    workload: str,
    cluster: str | None = None,
    deadline_seconds: float = 15,
) -> LookupResult:
    versions: list[DeployedVersion] = []
    errors: dict[str, str] = {}
    names = [cluster] if cluster is not None else sorted(readers)
    queried: list[str] = []
    for name in names:
        if name not in readers:
            errors[name] = "unknown cluster"
        elif namespace not in allowed_namespaces.get(name, []):
            errors[name] = "namespace not allowed"
        else:
            queried.append(name)
    if queried:
        # Clusters are queried concurrently under one overall deadline, so a hung
        # cluster costs at most deadline_seconds and never hides the others.
        pool = ThreadPoolExecutor(max_workers=len(queried))
        try:
            futures = {
                name: pool.submit(readers[name].workload_pods, namespace, workload)
                for name in queried
            }
            wait(futures.values(), timeout=deadline_seconds)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        for name, fut in futures.items():
            if not fut.done():
                errors[name] = f"timeout: no answer within {deadline_seconds:g}s"
                continue
            try:
                pods = fut.result()
            except Exception as e:  # noqa: BLE001 - one cluster must not hide the others
                errors[name] = f"{type(e).__name__}: {e}"
                continue
            versions.extend(_versions_in(name, namespace, workload, pods))
    versions.sort(key=lambda v: (v.cluster, v.container, v.digest or ""))
    return LookupResult(versions, dict(sorted(errors.items())))


def build_readers(
    configs: list[ClusterConfig],
) -> tuple[dict[str, ClusterReader], dict[str, list[str]]]:
    readers: dict[str, ClusterReader] = {c.name: KubeClusterReader(c) for c in configs}
    return readers, {c.name: list(c.namespaces) for c in configs}
