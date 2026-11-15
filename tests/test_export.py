import gzip
import tarfile
from pathlib import Path

import pytest

from conftest import git
from snapshotkit.export import ExportError, export_from_archive, export_from_checkout


def names(p: Path) -> list[str]:
    with tarfile.open(p, "r:gz") as t:
        return t.getnames()


def test_archive_has_no_git_or_history(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, {"a.txt": "hi"})
    out = tmp_path / "o.tar.gz"
    export_from_archive(repo, sha, out)
    assert not any(".git" in n.split("/") for n in names(out))
    raw = gzip.decompress(out.read_bytes())
    for needle in (sha.encode(), b"t@x", b"secret-commit-message"):
        assert needle not in raw


def test_archive_applies_export_ignore(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, {".gitattributes": "internal/ export-ignore\n",
                                     "internal/a.txt": "x", "b.txt": "y"})
    out = tmp_path / "o.tar.gz"
    export_from_archive(repo, sha, out)
    assert "internal/a.txt" not in names(out)
    assert "b.txt" in names(out)


def test_archive_applies_denylist(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, {".env": "A=1", "secrets.json": "{}", "src/app.py": "x"})
    out = tmp_path / "o.tar.gz"
    stats = export_from_archive(repo, sha, out)
    n = names(out)
    assert ".env" not in n and "secrets.json" not in n and "src/app.py" in n
    assert set(stats.files_denied) == {".env", "secrets.json"}
    assert stats.files_included == 1


def test_archive_refuses_export_subst(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, {".gitattributes": "*.txt export-subst\n", "a.txt": "$Format:%H$"})
    out = tmp_path / "o.tar.gz"
    with pytest.raises(ExportError) as ei:
        export_from_archive(repo, sha, out)
    assert ei.value.reason == "export-subst"
    assert not out.exists()


def test_archive_refuses_submodules(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, {".gitmodules": "[submodule \"x\"]\n", "a": "b"})
    out = tmp_path / "o.tar.gz"
    with pytest.raises(ExportError) as ei:
        export_from_archive(repo, sha, out)
    assert ei.value.reason == "needs-checkout-mode"
    assert "withSubmodulesOrLfs" in str(ei.value)
    assert not out.exists()


def test_archive_refuses_lfs_attributes(tmp_path, make_repo):
    repo, sha = make_repo(tmp_path, {".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n"})
    out = tmp_path / "o.tar.gz"
    with pytest.raises(ExportError) as ei:
        export_from_archive(repo, sha, out)
    assert ei.value.reason == "needs-checkout-mode"
    assert not out.exists()


def test_checkout_includes_submodule_files(tmp_path, make_repo):
    sub, _ = make_repo(tmp_path / "subsrc", {"lib/x.py": "print(1)"})
    sup_dir = tmp_path / "sup"
    sup_dir.mkdir()
    repo, _ = make_repo(sup_dir, {"main.py": "m"})
    git(repo, "submodule", "add", str(sub), "vendor/sub")
    git(repo, "commit", "-q", "-m", "add sub")
    out = tmp_path / "o.tar.gz"
    stats = export_from_checkout(repo, out)
    n = names(out)
    assert "vendor/sub/lib/x.py" in n and "main.py" in n
    assert not any(".git" in p.split("/") for p in n)
    assert stats.files_included == len(n)


def test_checkout_refuses_unpulled_lfs(tmp_path, make_repo):
    ptr = "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 3\n"
    repo, _ = make_repo(tmp_path, {"big.bin": ptr})
    out = tmp_path / "o.tar.gz"
    with pytest.raises(ExportError) as ei:
        export_from_checkout(repo, out)
    assert ei.value.reason == "lfs-not-pulled"
    assert not out.exists()


def test_checkout_applies_export_ignore_and_denylist(tmp_path, make_repo):
    repo, _ = make_repo(tmp_path, {".gitattributes": "internal/ export-ignore\n",
                                   "internal/a.txt": "x", ".env": "A", "src/app.py": "x"})
    out = tmp_path / "o.tar.gz"
    stats = export_from_checkout(repo, out)
    n = names(out)
    assert "internal/a.txt" not in n and ".env" not in n and "src/app.py" in n
    assert ".env" in stats.files_denied


def test_checkout_refuses_export_subst(tmp_path, make_repo):
    repo, _ = make_repo(tmp_path, {".gitattributes": "*.txt export-subst\n", "a.txt": "x"})
    out = tmp_path / "o.tar.gz"
    with pytest.raises(ExportError) as ei:
        export_from_checkout(repo, out)
    assert ei.value.reason == "export-subst"
    assert not out.exists()


def test_checkout_symlink_kept_as_link(tmp_path, make_repo):
    repo, _ = make_repo(tmp_path, {"a.txt": "x"})
    (repo / "ln").symlink_to("a.txt")
    git(repo, "add", "ln")
    out = tmp_path / "o.tar.gz"
    export_from_checkout(repo, out)
    with tarfile.open(out) as t:
        m = t.getmember("ln")
        assert m.issym() and m.linkname == "a.txt"
