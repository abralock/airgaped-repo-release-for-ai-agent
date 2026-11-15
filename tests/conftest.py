import subprocess
from pathlib import Path

import pytest

GIT = [
    "git",
    "-c", "user.name=T",
    "-c", "user.email=t@x",
    "-c", "commit.gpgsign=false",
    "-c", "init.defaultBranch=main",
    "-c", "protocol.file.allow=always",
]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(repo: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode() if isinstance(content, str) else content)


@pytest.fixture
def make_repo():
    def _make(tmp_path: Path, files: dict[str, str | bytes], commit: bool = True):
        repo = tmp_path / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q")
        _write(repo, files)
        git(repo, "add", "-A")
        sha = ""
        if commit:
            git(repo, "commit", "-q", "-m", "secret-commit-message")
            sha = git(repo, "rev-parse", "HEAD")
        return repo, sha

    return _make
