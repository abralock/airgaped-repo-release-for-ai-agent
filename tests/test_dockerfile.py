import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "Dockerfile"


def _text() -> str:
    return DOCKERFILE.read_text()


def test_every_from_uses_base_image_arg():
    # Every external image is ${BASE_IMAGE}; FROM <earlier stage> is allowed.
    froms = re.findall(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", _text(), flags=re.MULTILINE)
    assert froms, "no FROM lines"
    stages: set[str] = set()
    for image, alias in froms:
        assert image == "${BASE_IMAGE}" or image in stages, image
        if alias:
            stages.add(alias)


def test_required_build_args_declared():
    args = set(re.findall(r"^ARG\s+([A-Z0-9_]+)", _text(), flags=re.MULTILINE))
    expected = {
        "BASE_IMAGE",
        "PIP_INDEX_URL",
        "GITLEAKS_URL",
        "GITLEAKS_SHA256_AMD64",
        "GITLEAKS_SHA256_ARM64",
        "CRANE_URL",
        "CRANE_SHA256_AMD64",
        "CRANE_SHA256_ARM64",
    }
    assert expected <= args, expected - args


def test_downloads_are_checksum_verified():
    text = _text()
    assert text.count("sha256sum -c") >= 2
    assert "${GITLEAKS_URL" in text and "${CRANE_URL" in text


def test_no_public_hosts_hardcoded():
    text = _text()
    assert "github.com" not in text
    assert "pypi.org" not in text


def test_publisher_target_runs_non_root_with_config():
    text = _text()
    assert re.search(r"^FROM .* AS publisher", text, flags=re.MULTILINE)
    assert "USER 10001" in text
    assert "/etc/snapshotkit/gitleaks.toml" in text


def _stage(name: str) -> str:
    parts = re.split(r"^FROM .*$", _text(), flags=re.MULTILINE)
    heads = re.findall(r"^FROM .* AS (\S+)", _text(), flags=re.MULTILINE)
    return dict(zip(heads, parts[1:], strict=True))[name]


def test_publisher_has_no_entrypoint():
    # CI container jobs (Azure DevOps, GitLab) need an image without ENTRYPOINT.
    publisher = _stage("publisher")
    assert "ENTRYPOINT" not in publisher
    assert re.search(r'^CMD \["/bin/sh"\]', publisher, flags=re.MULTILINE)


def test_tool_target_is_minimal_non_root_mcp_server():
    assert re.search(r"^FROM app AS tool", _text(), flags=re.MULTILINE)
    tool = _stage("tool")
    assert 'ENTRYPOINT ["snapshot-mcp"]' in tool
    assert "USER 10001" in tool
    assert "EXPOSE 8080" in tool
    for forbidden in ("git", "gitleaks", "crane", "apt-get"):
        assert not re.search(rf"\b{forbidden}\b", tool), forbidden
