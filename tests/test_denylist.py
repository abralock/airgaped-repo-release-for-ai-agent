import pytest

from snapshotkit.denylist import is_denied


@pytest.mark.parametrize("p", [
    "config/secrets.yaml", "a/.env.prod", "certs/server.pem", "keys/id_rsa.pub",
    ".git/config", ".github/workflows/ci.yml", ".gitlab-ci.yml", "vault.kdbx",
])
def test_denied(p):
    assert is_denied(p)


@pytest.mark.parametrize("p", [
    "src/main.py", "src/.gitignore", ".gitattributes", "docs/key.md", "envoy.yaml",
])
def test_allowed(p):
    assert not is_denied(p)
