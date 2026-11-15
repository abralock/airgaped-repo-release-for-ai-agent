import pytest

from snapshotkit.store import (
    InMemoryStore,
    digest_index_key,
    manifest_key,
    snapshot_key,
    tag_index_key,
    validate_project,
)

D = "sha256:" + "ab" * 32
DK = "sha256-" + "ab" * 32


def test_key_builders():
    assert snapshot_key("orders-api", D) == f"snapshots/orders-api/{DK}.tar.gz"
    assert manifest_key("orders-api", D) == f"snapshots/orders-api/{DK}.json"
    assert digest_index_key(D) == f"index/digest/{DK}.json"
    assert tag_index_key("team/app", "1.2.3") == "index/tag/team/app/1.2.3.json"


def test_key_builders_validate_inputs():
    with pytest.raises(ValueError):
        snapshot_key("../x", D)
    with pytest.raises(ValueError):
        manifest_key("A", D)
    with pytest.raises(ValueError):
        snapshot_key("ok", "sha256:zz")
    with pytest.raises(ValueError):
        digest_index_key("nope")


@pytest.mark.parametrize("name", ["", "../x", "A", "a/b", "a" * 101])
def test_validate_project_rejects(name):
    with pytest.raises(ValueError):
        validate_project(name)


def test_validate_project_accepts():
    assert validate_project("orders-api") == "orders-api"


@pytest.mark.parametrize(
    "repo,tag",
    [
        ("../x", "1"),
        ("a/../b", "1"),
        ("a//b", "1"),
        ("/a", "1"),
        ("a b", "1"),
        ("a\tb", "1"),
        ("", "1"),
        ("a/", "1"),
        ("a", ".."),
        ("a", "1/2"),
        ("a", "/1"),
        ("a", "1 2"),
        ("a", ""),
        ("a", "1\n"),
        ("a", "v..1"),
    ],
)
def test_tag_index_key_rejects(repo, tag):
    with pytest.raises(ValueError):
        tag_index_key(repo, tag)


def test_put_new_is_write_once():
    s = InMemoryStore()
    assert s.put_new("k", b"one") is True
    assert s.put_new("k", b"two") is False
    assert s.get("k") == b"one"


def test_put_overwrites_get_missing_delete():
    s = InMemoryStore()
    assert s.get("k") is None
    s.put("k", b"1")
    s.put("k", b"2")
    assert s.get("k") == b"2"
    s.delete("k")
    assert s.get("k") is None
    s.delete("k")  # deleting a missing key is not an error


def test_list_keys_filters_by_prefix():
    s = InMemoryStore()
    for k in ("a/1", "a/2", "b/1"):
        s.put(k, b"x")
    assert sorted(s.list_keys("a/")) == ["a/1", "a/2"]
    assert sorted(s.list_keys("")) == ["a/1", "a/2", "b/1"]


def test_s3_client_config_suits_non_aws_stores():
    from snapshotkit.store import S3Store
    s = S3Store("http://127.0.0.1:1", "code-snapshots", "ak", "sk")
    cfg = s._client.meta.config
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"
    assert cfg.s3 == {"addressing_style": "path"}
    # botocore stores retries={"max_attempts": 3} as 3 retries + 1 initial attempt
    assert cfg.retries == {"total_max_attempts": 4, "mode": "standard"}


def test_boto_minimum_versions_pinned():
    import tomllib
    from pathlib import Path
    deps = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())[
        "project"]["dependencies"]
    # 1.36.0: first botocore with request_checksum_calculation (IfNoneMatch is older)
    assert "boto3>=1.36.0" in deps and "botocore>=1.36.0" in deps


def test_project_with_trailing_newline_rejected():
    with pytest.raises(ValueError):
        validate_project("proj\n")
