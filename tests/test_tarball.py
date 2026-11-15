import gzip
import io
import tarfile
from pathlib import Path

import pytest

from snapshotkit.tarball import Entry, UnsafeArchiveError, safe_extract, write_normalized_targz


def _entries():
    return [
        Entry("b/two.txt", 0o644, b"two"),
        Entry("a.txt", 0o755, b"one"),
        Entry("c/link", 0o777, None, link_target="../a.txt"),
    ]


def test_output_is_deterministic(tmp_path):
    e = _entries()
    write_normalized_targz(e, tmp_path / "1.tgz")
    write_normalized_targz(list(reversed(e)), tmp_path / "2.tgz")
    assert (tmp_path / "1.tgz").read_bytes() == (tmp_path / "2.tgz").read_bytes()


def test_metadata_is_stripped(tmp_path):
    out = tmp_path / "o.tgz"
    write_normalized_targz(_entries(), out)
    assert out.read_bytes()[4:8] == b"\0\0\0\0"
    with tarfile.open(out) as tar:
        assert tar.pax_headers == {}
        members = tar.getmembers()
        assert members
        for m in members:
            assert m.mtime == 0
            assert m.uid == m.gid == 0
            assert m.uname == m.gname == ""


def test_modes_normalized(tmp_path):
    out = tmp_path / "o.tgz"
    write_normalized_targz(
        [Entry("x", 0o700, b""), Entry("y", 0o600, b"")], out
    )
    with tarfile.open(out) as tar:
        assert tar.getmember("x").mode == 0o755
        assert tar.getmember("y").mode == 0o644


def test_long_path_without_pax(tmp_path):
    path = "d/" + "a" * 178
    assert len(path) == 180
    out = tmp_path / "o.tgz"
    write_normalized_targz([Entry(path, 0o644, b"hi")], out)
    with tarfile.open(out) as tar:
        assert tar.pax_headers == {}
        assert tar.extractfile(path).read() == b"hi"


def test_safe_extract_roundtrip(tmp_path):
    out = tmp_path / "o.tgz"
    write_normalized_targz(_entries(), out)
    dest = tmp_path / "dest"
    dest.mkdir()
    safe_extract(out, dest)
    assert (dest / "a.txt").read_bytes() == b"one"
    assert (dest / "b/two.txt").read_bytes() == b"two"
    assert (dest / "c/link").is_symlink()
    assert (dest / "c/link").read_bytes() == b"one"


def _evil_tar(path: Path, kind: str) -> None:
    with tarfile.open(path, "w:gz") as tar:
        ok = tarfile.TarInfo("ok.txt")
        ok.size = 2
        tar.addfile(ok, io.BytesIO(b"ok"))
        if kind == "link":
            ti = tarfile.TarInfo("link")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "../../etc/passwd"
            tar.addfile(ti)
        else:
            ti = tarfile.TarInfo(kind)
            ti.size = 1
            tar.addfile(ti, io.BytesIO(b"x"))


@pytest.mark.parametrize("kind", ["../evil", "/etc/evil", "link"])
def test_safe_extract_rejects(tmp_path, kind):
    bad = tmp_path / "bad.tgz"
    _evil_tar(bad, kind)
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(UnsafeArchiveError):
        safe_extract(bad, dest)
    assert list(dest.iterdir()) == []


def test_safe_extract_limits(tmp_path):
    out = tmp_path / "o.tgz"
    write_normalized_targz(
        [Entry("a", 0o644, b"x" * 100), Entry("b", 0o644, b"y" * 100)], out
    )
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(UnsafeArchiveError):
        safe_extract(out, dest, max_total_bytes=150)
    with pytest.raises(UnsafeArchiveError):
        safe_extract(out, dest, max_entries=1)
    assert list(dest.iterdir()) == []
