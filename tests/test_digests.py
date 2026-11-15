import json

import pytest

from snapshotkit.digests import digest_key, parse_image_id, platform_digests

H = "a" * 64


@pytest.mark.parametrize("raw", [f"docker-pullable://reg.local/app@sha256:{H}",
    f"reg.local:5000/team/app@sha256:{H}", f"sha256:{H}"])
def test_parse_image_id_forms(raw):
    assert parse_image_id(raw) == f"sha256:{H}"


@pytest.mark.parametrize("raw", ["", "reg/app:1.2", "sha256:xyz", f"sha512:{H}"])
def test_parse_image_id_unparseable(raw):
    assert parse_image_id(raw) is None


def test_digest_key():
    assert digest_key(f"sha256:{H}") == f"sha256-{H}"


def test_digest_key_invalid():
    pytest.raises(ValueError, digest_key, "latest")


def test_platform_digests_index():
    idx = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
        {"digest": f"sha256:{'b'*64}", "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": f"sha256:{'c'*64}", "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": f"sha256:{'d'*64}", "platform": {"os": "unknown", "architecture": "unknown"}}]}
    assert platform_digests(json.dumps(idx)) == [f"sha256:{'b'*64}", f"sha256:{'c'*64}"]


def test_platform_digests_single_manifest():
    assert platform_digests(json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json", "layers": []})) == []


def test_trailing_newline_rejected():
    import pytest as _pytest
    from snapshotkit.digests import digest_key, parse_image_id
    nl = "sha256:" + "a" * 64 + "\n"
    with _pytest.raises(ValueError):
        digest_key(nl)
    assert parse_image_id(nl) is None
    assert parse_image_id("reg/app@" + nl) is None
