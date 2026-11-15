import uuid

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def p():
    return f"t-{uuid.uuid4().hex}/"


def test_put_get_overwrite_delete(s3_store, p):
    assert s3_store.get(p + "k") is None
    s3_store.put(p + "k", b"1")
    s3_store.put(p + "k", b"2")
    assert s3_store.get(p + "k") == b"2"
    s3_store.delete(p + "k")
    assert s3_store.get(p + "k") is None
    s3_store.delete(p + "k")


def test_put_new_is_write_once(s3_store, p):
    assert s3_store.put_new(p + "k", b"original") is True
    assert s3_store.put_new(p + "k", b"other") is False
    assert s3_store.get(p + "k") == b"original"


def test_list_keys_filters_by_prefix(s3_store, p):
    for k in ("a/1", "a/2", "b/1"):
        s3_store.put(p + k, b"x")
    assert sorted(s3_store.list_keys(p + "a/")) == [p + "a/1", p + "a/2"]


def test_list_keys_paginates(s3_store, p):
    for i in range(1005):
        s3_store.put(f"{p}many/{i:05d}", b"x")
    assert len(list(s3_store.list_keys(p + "many/"))) == 1005


def test_non_404_non_412_errors_propagate(s3_settings, s3_bucket):
    from botocore.exceptions import ClientError

    from snapshotkit.store import S3Store

    bad = S3Store(s3_settings["endpoint_url"], "no-such-bucket-xyz",
                  s3_settings["access_key"], s3_settings["secret_key"])
    with pytest.raises(ClientError):
        bad.get("k")
    with pytest.raises(ClientError):
        bad.put_new("k", b"x")
