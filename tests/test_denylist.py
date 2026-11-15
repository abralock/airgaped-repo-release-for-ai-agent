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


@pytest.mark.parametrize("p", [
    "docs/manual.pdf", "a.doc", "a.docx", "a.xls", "a.xlsx", "a.ppt", "a.pptx", "a.odt",
    "a.ods", "dist/x.zip", "lib/a.jar", "a.war", "a.ear", "a.tar", "a.tgz", "a.gz",
    "a.bz2", "a.xz", "a.7z", "a.rar", "dist/pkg-1.0-py3-none-any.whl",
])
def test_unscannable_containers_denied(p):
    assert is_denied(p)


def test_gitleaksignore_denied_so_scanner_never_autoloads_it():
    assert is_denied(".gitleaksignore")
