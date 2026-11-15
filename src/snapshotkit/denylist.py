"""Path denylist applied to every snapshot."""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase

DEFAULT_DENYLIST: tuple[str, ...] = (
    ".env*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "*.kdbx", "secrets.*", ".git*",
)

_ALLOWED_FINAL = {".gitignore", ".gitattributes"}


def is_denied(path: str, patterns: Sequence[str] = DEFAULT_DENYLIST) -> bool:
    parts = [p for p in path.split("/") if p]
    for i, part in enumerate(parts):
        if i == len(parts) - 1 and part in _ALLOWED_FINAL:
            continue
        if any(fnmatchcase(part, pat) for pat in patterns):
            return True
    return False
