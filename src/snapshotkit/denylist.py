"""Path denylist applied to every snapshot."""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase

DEFAULT_DENYLIST: tuple[str, ...] = (
    ".env*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "*.kdbx", "secrets.*", ".git*",
    # Containers and documents gitleaks cannot inspect (and the agent cannot read):
    # never published, because their bytes could not be scanned.
    "*.pdf", "*.doc", "*.docx", "*.xls", "*.xlsx", "*.ppt", "*.pptx", "*.odt", "*.ods",
    "*.zip", "*.jar", "*.war", "*.ear", "*.tar", "*.tgz", "*.gz", "*.bz2", "*.xz", "*.7z",
    "*.rar", "*.whl",
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
