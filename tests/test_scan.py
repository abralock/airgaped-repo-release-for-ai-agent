import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from snapshotkit.scan import ScannerError, ScanResult, gitleaks_version, run_gitleaks


def make_fake(tmp_path: Path, exit_code: int, report: str | None = None,
              stderr: str = "") -> tuple[Path, Path]:
    """Fake gitleaks: records argv (one per line), optionally writes report, exits."""
    argv_log = tmp_path / "argv.txt"
    script = tmp_path / "fake-gitleaks"
    lines = [
        "#!/bin/sh",
        f'for a in "$@"; do printf "%s\\n" "$a" >> {argv_log}; done',
        'prev=""',
        'for a in "$@"; do',
        '  if [ "$prev" = "--report-path" ]; then rp="$a"; fi',
        '  prev="$a"',
        "done",
    ]
    if report is not None:
        lines.append(f"printf '%s' '{report}' > \"$rp\"")
    if stderr:
        lines.append(f"echo '{stderr}' >&2")
    lines.append(f"exit {exit_code}")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, argv_log


@pytest.fixture
def tree(tmp_path):
    d = tmp_path / "tree"
    d.mkdir()
    return d


def test_clean_on_exit_0(tmp_path, tree):
    fake, _ = make_fake(tmp_path, 0)
    res = run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json", gitleaks_bin=str(fake))
    assert isinstance(res, ScanResult)
    assert res.clean is True and res.findings == 0
    assert res.report_path == tmp_path / "r.json"


def test_findings_on_exit_1(tmp_path, tree):
    fake, _ = make_fake(tmp_path, 1, report='[{"a":1},{"a":2}]')
    res = run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json", gitleaks_bin=str(fake))
    assert res.clean is False and res.findings == 2


def test_exit_1_without_report_raises(tmp_path, tree):
    fake, _ = make_fake(tmp_path, 1, stderr="boom internal")
    with pytest.raises(ScannerError) as e:
        run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json", gitleaks_bin=str(fake))
    assert "exit code 1" in str(e.value) and "boom internal" in str(e.value)


def test_exit_1_with_garbage_report_raises_without_leaking_it(tmp_path, tree):
    fake, _ = make_fake(tmp_path, 1, report="SECRETCONTENT not json")
    with pytest.raises(ScannerError) as e:
        run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json", gitleaks_bin=str(fake))
    assert "SECRETCONTENT" not in str(e.value)


def test_exit_1_with_non_list_report_raises(tmp_path, tree):
    fake, _ = make_fake(tmp_path, 1, report='{"a":1}')
    with pytest.raises(ScannerError):
        run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json", gitleaks_bin=str(fake))


def test_exit_0_with_empty_list_report_is_clean(tmp_path, tree):
    fake, _ = make_fake(tmp_path, 0, report="[]")
    res = run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json", gitleaks_bin=str(fake))
    assert res.clean is True and res.findings == 0


def test_exit_0_with_garbage_report_raises(tmp_path, tree):
    fake, _ = make_fake(tmp_path, 0, report="SECRETCONTENT not json")
    with pytest.raises(ScannerError) as e:
        run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json", gitleaks_bin=str(fake))
    assert "SECRETCONTENT" not in str(e.value)


def test_exit_0_with_nonempty_report_raises(tmp_path, tree):
    fake, _ = make_fake(tmp_path, 0, report='[{"a":1}]')
    with pytest.raises(ScannerError):
        run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json", gitleaks_bin=str(fake))


def test_exit_1_with_empty_list_report_raises(tmp_path, tree):
    fake, _ = make_fake(tmp_path, 1, report="[]")
    with pytest.raises(ScannerError):
        run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json", gitleaks_bin=str(fake))


def test_unexpected_exit_raises(tmp_path, tree):
    fake, _ = make_fake(tmp_path, 2, stderr="kaput")
    with pytest.raises(ScannerError) as e:
        run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json", gitleaks_bin=str(fake))
    assert "exit code 2" in str(e.value) and "kaput" in str(e.value)


def test_missing_binary_raises(tmp_path, tree):
    with pytest.raises(ScannerError):
        run_gitleaks(tree, tmp_path / "c.toml", tmp_path / "r.json",
                     gitleaks_bin="/nonexistent")


def test_argv(tmp_path, tree):
    fake, log = make_fake(tmp_path, 0)
    cfg, rep = tmp_path / "c.toml", tmp_path / "r.json"
    run_gitleaks(tree, cfg, rep, gitleaks_bin=str(fake))
    argv = log.read_text().splitlines()
    assert argv[:2] == ["dir", "."]
    assert argv[argv.index("--config") + 1] == str(cfg.resolve())
    assert argv[argv.index("--report-format") + 1] == "json"
    assert argv[argv.index("--report-path") + 1] == str(rep.resolve())
    assert argv[argv.index("--exit-code") + 1] == "1"
    assert "--redact" in argv and "--no-banner" in argv
    assert "--gitleaks-ignore-path" not in argv

    log.unlink()
    ign = tmp_path / ".gitleaksignore"
    run_gitleaks(tree, cfg, rep, ignore_file=ign, gitleaks_bin=str(fake))
    argv = log.read_text().splitlines()
    assert argv[argv.index("--gitleaks-ignore-path") + 1] == str(ign.resolve())


def test_version_strips_v(tmp_path):
    script = tmp_path / "gl"
    script.write_text("#!/bin/sh\necho v8.18.4\n")
    script.chmod(0o755)
    assert gitleaks_version(str(script)) == "8.18.4"


def test_version_errors(tmp_path):
    with pytest.raises(ScannerError):
        gitleaks_version("/nonexistent")
    script = tmp_path / "gl"
    script.write_text("#!/bin/sh\necho nope >&2\nexit 3\n")
    script.chmod(0o755)
    with pytest.raises(ScannerError):
        gitleaks_version(str(script))


@pytest.mark.gitleaks
@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_real_gitleaks_detects_aws_key(tmp_path):
    # gitleaks' default aws rule allowlists keys containing "EXAMPLE", so the
    # fake key must not. Built at runtime so the repo holds no scannable literal.
    tree = tmp_path / "tree"
    tree.mkdir()
    key_id = "AKIA" + "QYLPMN5HFIQR7XYZ"
    secret = "x7Kp2Qm9" + "Zr4TvB8nLc1Yd6Wf3Hs5Jg0AeUo7Ni2P"
    (tree / "creds.py").write_text(
        f'aws_access_key_id = "{key_id}"\naws_secret_access_key = "{secret}"\n'
    )
    cfg = Path(__file__).parent.parent / "config" / "gitleaks.toml"
    res = run_gitleaks(tree, cfg, tmp_path / "r.json")
    assert res.clean is False and res.findings >= 1
    assert secret not in (tmp_path / "r.json").read_text()  # --redact
    assert gitleaks_version()


def test_runs_in_tree_dir_with_absolute_paths(tmp_path, tree, monkeypatch):
    fake, log = make_fake(tmp_path, 0)
    (tmp_path / "w").mkdir()
    monkeypatch.chdir(tmp_path / "w")
    run_gitleaks(tree, Path("c.toml"), Path("r.json"), ignore_file=Path("i"),
                 gitleaks_bin=str(fake))
    argv = log.read_text().splitlines()
    w = (tmp_path / "w").resolve()
    assert argv[argv.index("--config") + 1] == str(w / "c.toml")
    assert argv[argv.index("--report-path") + 1] == str(w / "r.json")
    assert argv[argv.index("--gitleaks-ignore-path") + 1] == str(w / "i")


@pytest.mark.gitleaks
@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_real_gitleaks_fingerprint_is_relative_to_tree(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    key_id = "AKIA" + "QYLPMN5HFIQR7XYZ"
    (tree / "cfg.py").write_text(f'aws_access_key_id = "{key_id}"\n')
    cfg = Path(__file__).parent.parent / "config" / "gitleaks.toml"
    res = run_gitleaks(tree, cfg, tmp_path / "r.json")
    assert res.clean is False
    fps = [f["Fingerprint"] for f in json.loads((tmp_path / "r.json").read_text())]
    assert fps == ["cfg.py:aws-access-token:1"]
    ign = tmp_path / "ign"
    ign.write_text("cfg.py:aws-access-token:1\n")
    assert run_gitleaks(tree, cfg, tmp_path / "r2.json", ignore_file=ign).clean is True
