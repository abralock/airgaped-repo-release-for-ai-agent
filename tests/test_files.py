import os

import pytest

from snapshotkit.files import (
    PathOutsideSnapshot,
    list_files,
    read_file,
    resolve_inside,
    search_code,
)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.py").write_text("import os\nprint('hello')\n")
    (r / "src" / "b.txt").write_text("hello world\nhello again\n")
    (r / "README.md").write_text("hello readme\n")
    return r


def test_read_file_traversal_refused(root):
    with pytest.raises(PathOutsideSnapshot):
        read_file(root, "../../etc/passwd")


def test_absolute_path_refused(root):
    with pytest.raises(PathOutsideSnapshot):
        resolve_inside(root, "/etc/passwd")


def test_symlink_outside_refused(root, tmp_path):
    (tmp_path / "outside.txt").write_text("secret")
    os.symlink(tmp_path / "outside.txt", root / "link.txt")
    with pytest.raises(PathOutsideSnapshot):
        read_file(root, "link.txt")


def test_read_file_ok_and_truncates(root):
    assert read_file(root, "src/a.py") == {
        "content": "import os\nprint('hello')\n",
        "truncated": False,
        "binary": False,
    }
    r = read_file(root, "src/a.py", max_bytes=6)
    assert r["content"] == "import" and r["truncated"] is True


def test_binary_file_reported(root):
    (root / "bin.dat").write_bytes(b"ab\x00cd")
    assert read_file(root, "bin.dat") == {
        "content": "",
        "truncated": False,
        "binary": True,
    }


def test_search_limit_and_glob(root):
    (root / "bin.dat").write_bytes(b"hello\x00")
    r = search_code(root, "hello")
    assert {m["path"] for m in r["matches"]} == {"src/a.py", "src/b.txt", "README.md"}
    assert not r["truncated"]
    r = search_code(root, "hello", glob="*.txt")
    assert [(m["path"], m["line"], m["text"]) for m in r["matches"]] == [
        ("src/b.txt", 1, "hello world"),
        ("src/b.txt", 2, "hello again"),
    ]
    r = search_code(root, "hello", max_matches=2)
    assert len(r["matches"]) == 2 and r["truncated"] is True


def test_search_skips_escaping_symlink(root, tmp_path):
    (tmp_path / "outside.txt").write_text("hello outside\n")
    os.symlink(tmp_path / "outside.txt", root / "link.txt")
    r = search_code(root, "outside")
    assert r["matches"] == []


def test_invalid_regex_message(root):
    with pytest.raises(ValueError, match="^invalid pattern:"):
        search_code(root, "(")


def test_list_files_sorted_and_limited(root):
    assert list_files(root) == {
        "files": ["README.md", "src/a.py", "src/b.txt"],
        "truncated": False,
    }
    assert list_files(root, subdir="src")["files"] == ["src/a.py", "src/b.txt"]
    r = list_files(root, limit=2)
    assert len(r["files"]) == 2 and r["truncated"] is True
    with pytest.raises(PathOutsideSnapshot):
        list_files(root, subdir="..")
