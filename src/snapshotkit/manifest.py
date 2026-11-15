"""Snapshot manifest: exactly the spec fields, and never a commit SHA."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Literal

SOURCE_PLATFORMS = ("azure-devops", "gitlab")
SCAN_RESULTS = ("clean", "clean-with-reviewed-allowlist")


@dataclass(frozen=True)
class Manifest:
    project: str
    source_platform: Literal["azure-devops", "gitlab"]
    repo_path: str
    image_repository: str
    image_tag: str
    image_digest: str
    pipeline_run_url: str
    published_at: str
    scanner: str
    scanner_version: str
    scan_result: Literal["clean", "clean-with-reviewed-allowlist"]

    def __post_init__(self) -> None:
        if self.source_platform not in SOURCE_PLATFORMS:
            raise ValueError(f"invalid source_platform: {self.source_platform!r}")
        if self.scan_result not in SCAN_RESULTS:
            raise ValueError(f"invalid scan_result: {self.scan_result!r}")

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), indent=2, sort_keys=True).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "Manifest":
        raw = json.loads(data)
        names = {f.name for f in fields(cls)}
        if not isinstance(raw, dict) or set(raw) != names:
            raise ValueError("manifest does not have exactly the spec fields")
        return cls(**raw)
