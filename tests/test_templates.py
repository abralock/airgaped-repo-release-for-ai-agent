import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent / "templates"
ADO = ROOT / "azure-devops" / "snapshot-publish-job.yml"
GL = ROOT / "gitlab" / "snapshot-publish.gitlab-ci.yml"
ADO_EX = ROOT / "examples" / "azure-pipelines.example.yml"
GL_EX = ROOT / "examples" / "gitlab-ci.example.yml"


def _load(p):
    return yaml.safe_load(p.read_text())


def _ado_job():
    return _load(ADO)["jobs"][0]


def _steps(job):
    return job["steps"]


def _script_text(job):
    return "\n".join(s["script"] for s in job["steps"] if isinstance(s, dict) and "script" in s)


def test_ado_parameters():
    params = {p["name"]: p for p in _load(ADO)["parameters"]}
    assert set(params) == {"dependsOn", "project", "imageRepository", "imageTag", "imageDigest",
                           "withSubmodulesOrLfs", "multiArch", "publisherImage", "variableGroup"}
    assert params["withSubmodulesOrLfs"]["default"] is False
    assert params["multiArch"]["default"] is False
    assert params["variableGroup"]["default"] == "snapshot-store"


def test_ado_job_settings():
    job = _ado_job()
    assert job["continueOnError"] is True
    assert job["container"]["image"] == "${{ parameters.publisherImage }}"
    assert "--user 0:0" in job["container"]["options"]
    assert job["timeoutInMinutes"] == 30


def test_ado_checkout():
    job = _ado_job()
    co = next(s for s in _steps(job) if "checkout" in s)
    assert co["checkout"] == "self"
    assert co["fetchDepth"] == 1
    assert co["persistCredentials"] is False
    raw = ADO.read_text()
    assert re.search(r"\$\{\{ if eq\(parameters\.withSubmodulesOrLfs, true\) \}\}:\s+submodules: recursive\s+lfs: true", raw)


def test_ado_script_flags():
    text = _script_text(_ado_job())
    assert "--source-platform azure-devops" in text
    assert "--commit $(Build.SourceVersion)" in text
    assert '--pipeline-run-url "$(System.CollectionUri)$(System.TeamProject)/_build/results?buildId=$(Build.BuildId)"' in text
    assert "--gitleaks-config /etc/snapshotkit/gitleaks.toml" in text
    assert "##vso[task.logissue type=warning]" in text
    assert re.search(r"10\)[^;]*##vso\[task.logissue type=warning\]Secrets found", text)
    assert "withSubmodulesOrLfs" in text  # exit 11 warning names the parameter


def test_ado_multiarch():
    raw = ADO.read_text()
    assert "parameters.multiArch" in raw
    assert re.search(r'crane manifest .*> platform-manifest\.json', raw)
    assert "--platform-manifest platform-manifest.json" in raw and "iif(parameters.multiArch" in raw


def test_ado_depends_and_group():
    job = _ado_job()
    assert job["dependsOn"] == "${{ parameters.dependsOn }}"
    assert {"group": "${{ parameters.variableGroup }}"} in job["variables"]


def test_gitlab_job():
    doc = _load(GL)
    job = doc[".snapshot-publish"]
    assert job["allow_failure"] is True
    assert job["timeout"] == "30m"
    assert job["variables"]["GIT_DEPTH"] == "1"
    assert job["variables"]["SNAPSHOT_MODE"] == "archive"
    assert job["variables"]["SNAPSHOT_PROJECT"] == "$CI_PROJECT_PATH_SLUG"
    header = GL.read_text().split(".snapshot-publish:")[0]
    assert "^[a-z0-9][a-z0-9._-]{0,99}$" in header and "make-writer-policy.sh" in header
    assert job["variables"]["SNAPSHOT_MULTI_ARCH"] == "false"
    assert "SNAPSHOT_PUBLISHER_IMAGE" in GL.read_text()
    assert job["image"] == "$SNAPSHOT_PUBLISHER_IMAGE"
    text = "\n".join(job["script"])
    for frag in ["--source-platform gitlab", '--commit "$CI_COMMIT_SHA"', '--repo-path "$CI_PROJECT_PATH"',
                 '--pipeline-run-url "$CI_PIPELINE_URL"', "safe.directory", "crane manifest", "IMAGE_DIGEST"]:
        assert frag in text


def test_no_literal_secret_key():
    for p in (ADO, GL):
        for line in p.read_text().splitlines():
            m = re.search(r"SNAPSHOT_STORE_SECRET_KEY\s*[:=]\s*(\S+)", line)
            if m:
                assert m.group(1).startswith(("$(", "$", "${{")), line


def test_examples():
    ado = _load(ADO_EX)
    assert any(r.get("repository") == "templates" for r in ado["resources"]["repositories"])
    assert "snapshot-publish-job.yml@templates" in ADO_EX.read_text()
    gl = _load(GL_EX)
    assert gl["include"][0]["project"]
    assert any(isinstance(v, dict) and v.get("extends") == ".snapshot-publish" for v in gl.values())


def test_ado_step_continue_on_error_and_webhook():
    job = _ado_job()
    pub = next(s for s in job["steps"] if s.get("displayName") == "snapshot-publish")
    assert pub["continueOnError"] is True
    assert pub["env"]["SNAPSHOT_NOTIFY_WEBHOOK"] == "$(NOTIFY_WEBHOOK_RESOLVED)"
    assert "$(SNAPSHOT_NOTIFY_WEBHOOK)" not in ADO.read_text()
    assert any(isinstance(v, dict) and v.get("name") == "NOTIFY_WEBHOOK_RESOLVED"
               and "coalesce(variables['SNAPSHOT_NOTIFY_WEBHOOK']" in v["value"] for v in job["variables"])
    raw = ADO.read_text()
    crane = raw.split("Fetch image index manifest")[1].split("- script:")[0]
    assert "continueOnError: true" in crane
