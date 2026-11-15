from snapshotkit.clusters import PodContainer


class FakeClusterReader:
    def __init__(
        self,
        pods: dict[tuple[str, str], list[PodContainer]] | None = None,
        raises: Exception | None = None,
        image_ids: set[str] | None = None,
    ):
        self.pods = pods or {}
        self.raises = raises
        self.image_ids = image_ids or set()
        self.calls: list[tuple[str, str]] = []

    def workload_pods(self, namespace: str, workload: str) -> list[PodContainer]:
        self.calls.append((namespace, workload))
        if self.raises:
            raise self.raises
        return list(self.pods.get((namespace, workload), []))

    def all_image_ids(self) -> set[str]:
        if self.raises:
            raise self.raises
        return set(self.image_ids)
