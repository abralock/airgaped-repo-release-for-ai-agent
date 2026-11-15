"""Parsing of container image digests."""
import json
import re

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def parse_image_id(image_id: str) -> str | None:
    """Extract a ``sha256:<64 hex>`` digest from a Kubernetes imageID, or None."""
    if "@" in image_id:
        candidate = image_id.rsplit("@", 1)[1]
    else:
        candidate = image_id.split("://", 1)[1] if "://" in image_id else image_id
    return candidate if DIGEST_RE.match(candidate) else None


def digest_key(digest: str) -> str:
    """Filesystem/object-key safe form of a digest: ``sha256-<hex>``."""
    if not DIGEST_RE.match(digest):
        raise ValueError(f"invalid digest: {digest!r}")
    return digest.replace(":", "-", 1)


def platform_digests(manifest_json: str) -> list[str]:
    """Per-platform manifest digests of an index; attestation entries are skipped."""
    manifest = json.loads(manifest_json)
    return [
        entry["digest"]
        for entry in manifest.get("manifests", [])
        if entry.get("platform", {}).get("os") != "unknown"
    ]
