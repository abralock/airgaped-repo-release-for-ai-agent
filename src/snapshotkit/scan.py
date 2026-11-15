"""gitleaks wrapper. A scanner error is never reported as clean."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ScannerError(Exception):
    """gitleaks missing, crashed, or exited unexpectedly."""


@dataclass
class ScanResult:
    clean: bool
    findings: int
    report_path: Path


def _tail(text: str, limit: int = 500) -> str:
    return text.strip()[-limit:]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as e:
        raise ScannerError(f"cannot run {argv[0]}: {e}") from e


def _read_report(report_path: Path) -> list | None:
    try:
        data = json.loads(report_path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def run_gitleaks(
    tree_dir: Path,
    config: Path,
    report_path: Path,
    ignore_file: Path | None = None,
    gitleaks_bin: str = "gitleaks",
) -> ScanResult:
    report_path = Path(report_path)
    report_path.unlink(missing_ok=True)  # never trust a stale report
    argv = [
        gitleaks_bin, "dir", str(tree_dir),
        "--config", str(config),
        "--report-format", "json",
        "--report-path", str(report_path),
        "--exit-code", "1",
        "--redact",
        "--no-banner",
    ]
    if ignore_file is not None:
        argv += ["--gitleaks-ignore-path", str(ignore_file)]
    proc = _run(argv)
    if proc.returncode == 0:
        if report_path.exists():
            report = _read_report(report_path)
            if report is None:
                raise ScannerError(
                    "gitleaks exited 0 but its report is not a parseable JSON list"
                )
            if report:
                raise ScannerError(
                    f"gitleaks exited 0 but its report lists {len(report)} findings"
                )
        return ScanResult(True, 0, report_path)
    if proc.returncode == 1:
        report = _read_report(report_path)
        if report is not None and report:
            return ScanResult(False, len(report), report_path)
    raise ScannerError(
        f"gitleaks failed with exit code {proc.returncode}: {_tail(proc.stderr)}"
    )


def gitleaks_version(gitleaks_bin: str = "gitleaks") -> str:
    proc = _run([gitleaks_bin, "version"])
    if proc.returncode != 0:
        raise ScannerError(
            f"gitleaks version failed with exit code {proc.returncode}: {_tail(proc.stderr)}"
        )
    version = proc.stdout.strip()
    return version[1:] if version.startswith("v") else version
