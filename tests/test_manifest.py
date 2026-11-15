import json

import pytest

from snapshotkit.manifest import Manifest

KW = dict(
    project="p", source_platform="gitlab", repo_path="g/p", image_repository="reg/app",
    image_tag="1.0", image_digest="sha256:" + "a" * 64, pipeline_run_url="http://ci/1",
    published_at="2026-01-01T00:00:00Z", scanner="gitleaks", scanner_version="8.0",
    scan_result="clean",
)


def test_round_trip_and_keys():
    m = Manifest(**KW)
    assert Manifest.from_json(m.to_json()) == m
    assert set(json.loads(m.to_json())) == set(KW) and len(KW) == 11


def test_invalid_values():
    with pytest.raises(ValueError):
        Manifest(**{**KW, "source_platform": "github"})
    with pytest.raises(ValueError):
        Manifest(**{**KW, "scan_result": "dirty"})
