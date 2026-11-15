import json

from snapshotkit import alias
from snapshotkit.store import InMemoryStore, digest_index_key

D = "sha256:" + "a" * 64
A = "sha256:" + "b" * 64


def run(monkeypatch, store, *argv):
    monkeypatch.setattr(alias.S3Store, "from_env", classmethod(lambda cls: store))
    return alias.main(list(argv))


def test_writes_alias(monkeypatch):
    s = InMemoryStore()
    assert run(monkeypatch, s, "--project", "p", "--digest", D, "--alias-digest", A) == 0
    assert json.loads(s.get(digest_index_key(A))) == {"project": "p", "snapshot_digest": D}


def test_existing_alias_exit_0(monkeypatch, capsys):
    s = InMemoryStore()
    args = ("--project", "p", "--digest", D, "--alias-digest", A)
    run(monkeypatch, s, *args)
    assert run(monkeypatch, s, *args) == 0
    assert "alias already exists" in capsys.readouterr().err


def test_invalid_digests_exit_1(monkeypatch):
    s = InMemoryStore()
    assert run(monkeypatch, s, "--project", "p", "--digest", "bad", "--alias-digest", A) == 1
    assert run(monkeypatch, s, "--project", "p", "--digest", D, "--alias-digest", "bad") == 1
    assert run(monkeypatch, s, "--project", "BAD!", "--digest", D, "--alias-digest", A) == 1
    assert list(s.list_keys("")) == []
