import json

import pytest

from snapshotkit.store import InMemoryStore
from snapshotkit.transfer import TransferError, export_bundle, import_bundle, main

SNAP = "snapshots/app/sha256-aa.tar.gz"
MAN = "snapshots/app/sha256-aa.json"
DIG = "index/digest/sha256-aa.json"
TAG = "index/tag/reg/app/v1.json"


def _src():
    s = InMemoryStore()
    for k in (SNAP, MAN, DIG, TAG):
        s.put(k, k.encode())
    s.put("other/ignored", b"x")
    return s


def test_roundtrip_between_stores(tmp_path):
    src, dst = _src(), InMemoryStore()
    assert export_bundle(src, tmp_path / "b", tmp_path / "state.json") == 4
    assert import_bundle(dst, tmp_path / "b") == 4
    for k in (SNAP, MAN, DIG, TAG):
        assert dst.get(k) == k.encode()
    assert dst.get("other/ignored") is None


def test_second_export_only_new_keys(tmp_path):
    src = _src()
    state = tmp_path / "state.json"
    export_bundle(src, tmp_path / "b1", state)
    src.put("snapshots/app/sha256-bb.tar.gz", b"new")
    assert export_bundle(src, tmp_path / "b2", state) == 1
    files = json.loads((tmp_path / "b2" / "bundle.json").read_text())["files"]
    assert list(files) == ["snapshots/app/sha256-bb.tar.gz"]


def test_tampered_file_aborts_before_any_write(tmp_path):
    export_bundle(_src(), tmp_path / "b", tmp_path / "s.json")
    (tmp_path / "b" / TAG).write_bytes(b"tampered")
    dst = InMemoryStore()
    with pytest.raises(TransferError, match="checksum"):
        import_bundle(dst, tmp_path / "b")
    assert list(dst.list_keys("")) == []


def test_import_order_snapshots_before_index(tmp_path):
    export_bundle(_src(), tmp_path / "b", tmp_path / "s.json")

    class Recording(InMemoryStore):
        order: list[str] = []

        def put_new(self, key, data):
            self.order.append(key)
            return super().put_new(key, data)

        def put(self, key, data):
            self.order.append(key)
            super().put(key, data)

    dst = Recording()
    import_bundle(dst, tmp_path / "b")
    assert dst.order == [SNAP, MAN, DIG, TAG]


def test_import_existing_snapshot_not_overwritten(tmp_path):
    export_bundle(_src(), tmp_path / "b", tmp_path / "s.json")
    dst = InMemoryStore()
    dst.put(SNAP, b"original")
    dst.put(TAG, b"old-tag")
    assert import_bundle(dst, tmp_path / "b") == 3  # snapshot skipped
    assert dst.get(SNAP) == b"original"
    assert dst.get(TAG) == TAG.encode()  # tag index overwritten


@pytest.mark.parametrize("key", ["/abs/x", "snapshots/../x", "evil/x", "index/tag/../../x"])
def test_bad_key_rejected_before_write(tmp_path, key):
    b = tmp_path / "b"
    b.mkdir()
    (b / "bundle.json").write_text(json.dumps({"version": 1, "files": {SNAP: "0", key: "0"}}))
    dst = InMemoryStore()
    with pytest.raises(TransferError, match="bad key"):
        import_bundle(dst, b)
    assert list(dst.list_keys("")) == []


def test_missing_state_file_means_nothing_exported(tmp_path):
    state = tmp_path / "s.json"
    assert not state.exists()
    export_bundle(_src(), tmp_path / "b", state)
    assert sorted(json.loads(state.read_text())) == sorted([SNAP, MAN, DIG, TAG])


def test_retagged_key_is_reexported_and_import_updates_it(tmp_path):
    src, dst = _src(), InMemoryStore()
    state = tmp_path / "s.json"
    export_bundle(src, tmp_path / "b1", state)
    import_bundle(dst, tmp_path / "b1")
    src.put(TAG, b"moved-to-new-digest")
    assert export_bundle(src, tmp_path / "b2", state) == 1
    assert (tmp_path / "b2" / TAG).read_bytes() == b"moved-to-new-digest"
    assert import_bundle(dst, tmp_path / "b2") == 1
    assert dst.get(TAG) == b"moved-to-new-digest"


def test_unchanged_keys_not_reexported(tmp_path):
    src = _src()
    state = tmp_path / "s.json"
    export_bundle(src, tmp_path / "b1", state)
    assert export_bundle(src, tmp_path / "b2", state) == 0


def test_legacy_list_state_accepted(tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps([SNAP, MAN, DIG, TAG]))
    assert export_bundle(_src(), tmp_path / "b", state) == 1  # only the tag
    files = json.loads((tmp_path / "b" / "bundle.json").read_text())["files"]
    assert list(files) == [TAG]
    assert set(json.loads(state.read_text())) == {SNAP, MAN, DIG, TAG}


def test_nul_in_key_rejected(tmp_path):
    b = tmp_path / "b"
    b.mkdir()
    (b / "bundle.json").write_text(json.dumps({"version": 1, "files": {SNAP + "\0x": "0"}}))
    with pytest.raises(TransferError, match="bad key"):
        import_bundle(InMemoryStore(), b)


def test_failed_export_leaves_state_unchanged(tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({SNAP: "0"}))

    class Failing(InMemoryStore):
        def get(self, key):
            if key == DIG:
                raise RuntimeError("boom")
            return super().get(key)

    src = Failing()
    for k in (SNAP, MAN, DIG, TAG):
        src.put(k, k.encode())
    with pytest.raises(RuntimeError):
        export_bundle(src, tmp_path / "b", state)
    assert json.loads(state.read_text()) == {SNAP: "0"}
    assert not (tmp_path / "b" / "bundle.json").exists()


def test_cli_exit_codes(tmp_path, capsys):
    src, dst = _src(), InMemoryStore()
    out, state = str(tmp_path / "b"), str(tmp_path / "s.json")
    assert main(["export", "--out", out, "--state", state], store=src) == 0
    assert "exported 4" in capsys.readouterr().out
    assert main(["import", "--in", out], store=dst) == 0
    assert "imported 4" in capsys.readouterr().out
    (tmp_path / "b" / SNAP).write_bytes(b"bad")
    assert main(["import", "--in", out], store=InMemoryStore()) == 1
    assert "checksum" in capsys.readouterr().err
