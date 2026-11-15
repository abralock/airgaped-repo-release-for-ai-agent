"""snapshot-publish: export, scan the exact bytes, then upload."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from snapshotkit.digests import digest_key, platform_digests
from snapshotkit.export import ExportError, export_from_archive, export_from_checkout
from snapshotkit.manifest import Manifest
from snapshotkit.notify import send_failure
from snapshotkit.scan import gitleaks_version, run_gitleaks
from snapshotkit.store import (
    SnapshotStore,
    S3Store,
    digest_index_key,
    manifest_key,
    snapshot_key,
    tag_index_key,
    validate_project,
)
from snapshotkit.tarball import safe_extract


@dataclass
class PublishRequest:
    repo_dir: Path
    commit: str
    mode: Literal["archive", "checkout"]
    project: str
    source_platform: str
    repo_path: str
    image_repository: str
    image_tag: str
    image_digest: str
    alias_digests: list[str]
    pipeline_run_url: str
    gitleaks_config: Path
    gitleaks_ignore: Path | None
    workdir: Path


class Outcome(Enum):
    PUBLISHED = "published"
    ALREADY_PUBLISHED = "already-published"
    SECRETS_FOUND = "secrets-found"
    EXPORT_REFUSED = "export-refused"


def _log(msg: str) -> None:
    print(f"snapshot-publish: {msg}", file=sys.stderr)


def _project_ignore(req: PublishRequest) -> bytes | None:
    """The root .gitleaksignore blob committed at the exported commit, else None.

    Reads the git object, never the working tree, and only a regular-file blob
    (not a symlink, directory or submodule).
    """
    repo = str(req.repo_dir)
    ref = req.commit if req.mode == "archive" else "HEAD"
    ls = subprocess.run(
        ["git", "-C", repo, "ls-tree", ref, "--", ".gitleaksignore"],
        capture_output=True, check=False,
    )
    if ls.returncode != 0 or not ls.stdout.strip():
        return None
    mode = ls.stdout.split()[0]
    if mode not in (b"100644", b"100755"):
        _log(f"warning: .gitleaksignore is not a regular file (mode {mode.decode()}); ignored")
        return None
    blob = subprocess.run(
        ["git", "-C", repo, "cat-file", "blob", f"{ref}:.gitleaksignore"],
        capture_output=True, check=False,
    )
    return blob.stdout if blob.returncode == 0 else None


def _ignore_file(req: PublishRequest, workdir: Path) -> Path | None:
    project = _project_ignore(req)
    admin = req.gitleaks_ignore
    if project is None:
        return admin
    target = workdir / "project.gitleaksignore"
    if admin is None:
        target.write_bytes(project)
        return target
    sep = b"" if project.endswith(b"\n") or not project else b"\n"
    target = workdir / "combined.gitleaksignore"
    target.write_bytes(project + sep + Path(admin).read_bytes())
    return target


def _warn_if_tag_has_sha(req: PublishRequest) -> None:
    """The tag is in the manifest and index, so a SHA in it reaches the agent. Warn only."""
    sha = req.commit
    if not sha:
        rev = subprocess.run(
            ["git", "-C", str(req.repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        sha = rev.stdout.strip() if rev.returncode == 0 else ""
    sha = sha.lower()
    # any >=7-char prefix of the SHA contains its first 7 characters
    if len(sha) >= 7 and sha[:7] in req.image_tag.lower():
        _log("warning: image tag contains the commit SHA; it will be visible to the agent")


def publish_snapshot(
    req: PublishRequest,
    store: SnapshotStore,
    scanner: Callable = run_gitleaks,
    scanner_version: Callable[[], str] = gitleaks_version,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Outcome:
    # Validate everything before doing any work.
    validate_project(req.project)
    digest_key(req.image_digest)
    aliases: list[str] = []
    for a in req.alias_digests:
        digest_key(a)
        if a != req.image_digest and a not in aliases:
            aliases.append(a)
    snap_key = snapshot_key(req.project, req.image_digest)
    man_key = manifest_key(req.project, req.image_digest)
    tag_key = tag_index_key(req.image_repository, req.image_tag)
    _warn_if_tag_has_sha(req)

    workdir = Path(req.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    tar_path = workdir / "snapshot.tar.gz"
    tree = workdir / "tree"
    try:
        if req.mode == "archive":
            export_from_archive(req.repo_dir, req.commit, tar_path)
        else:
            export_from_checkout(req.repo_dir, tar_path)
    except ExportError as e:
        _log(f"export refused ({e.reason}): {e}")
        return Outcome.EXPORT_REFUSED

    safe_extract(tar_path, tree)
    result = scanner(
        tree, req.gitleaks_config, workdir / "gitleaks-report.json",
        _ignore_file(req, workdir),
    )
    if not result.clean:
        _log(f"{result.findings} secret finding(s); nothing published")
        return Outcome.SECRETS_FOUND

    manifest = Manifest(
        project=req.project,
        source_platform=req.source_platform,  # type: ignore[arg-type]
        repo_path=req.repo_path,
        image_repository=req.image_repository,
        image_tag=req.image_tag,
        image_digest=req.image_digest,
        pipeline_run_url=req.pipeline_run_url,
        published_at=now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        scanner="gitleaks",
        scanner_version=scanner_version(),
        scan_result=(
            "clean-with-reviewed-allowlist" if req.gitleaks_ignore else "clean"
        ),
    )
    index_body = json.dumps(
        {"project": req.project, "snapshot_digest": req.image_digest}
    ).encode()

    created = store.put_new(snap_key, tar_path.read_bytes())
    store.put_new(man_key, manifest.to_json())
    for d in [req.image_digest, *aliases]:
        store.put_new(digest_index_key(d), index_body)
    store.put(tag_key, index_body)
    return Outcome.PUBLISHED if created else Outcome.ALREADY_PUBLISHED


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="snapshot-publish")
    p.add_argument("--repo-dir", type=Path, required=True)
    p.add_argument("--commit", default="")
    p.add_argument("--mode", choices=["archive", "checkout"], default="archive")
    p.add_argument("--project", required=True)
    p.add_argument("--source-platform", required=True, choices=["azure-devops", "gitlab"])
    p.add_argument("--repo-path", required=True)
    p.add_argument("--image-repository", required=True)
    p.add_argument("--image-tag", required=True)
    p.add_argument("--image-digest", required=True)
    p.add_argument("--alias-digest", action="append", default=[])
    p.add_argument("--platform-manifest", type=Path)
    p.add_argument("--pipeline-run-url", default="")
    p.add_argument("--gitleaks-config", type=Path, required=True)
    p.add_argument("--gitleaks-ignore", type=Path)
    p.add_argument("--workdir", type=Path)
    return p


_EXIT = {
    Outcome.PUBLISHED: 0,
    Outcome.ALREADY_PUBLISHED: 0,
    Outcome.SECRETS_FOUND: 10,
    Outcome.EXPORT_REFUSED: 11,
}


def main(argv: list[str] | None = None) -> int:
    webhook = os.environ.get("SNAPSHOT_NOTIFY_WEBHOOK")
    try:
        args = _parser().parse_args(argv)
    except _UsageError as e:
        _log(f"usage error: {e}")
        known = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        for f in ("--project", "--image-digest", "--pipeline-run-url"):
            known.add_argument(f)
        try:
            k, _ = known.parse_known_args(argv)
        except Exception:  # noqa: BLE001
            k = argparse.Namespace(project=None, image_digest=None, pipeline_run_url=None)
        send_failure(
            webhook,
            {
                "project": k.project,
                "image_digest": k.image_digest,
                "reason": f"usage error: {e}",
                "pipeline_run_url": k.pipeline_run_url,
            },
        )
        return 1
    payload = {
        "project": args.project,
        "image_digest": args.image_digest,
        "reason": "",
        "pipeline_run_url": args.pipeline_run_url,
    }
    try:
        aliases = list(args.alias_digest)
        if args.platform_manifest:
            aliases += platform_digests(args.platform_manifest.read_text())
        if args.mode == "archive" and not args.commit:
            raise ValueError("--commit is required in archive mode")
        with tempfile.TemporaryDirectory() as tmp:
            req = PublishRequest(
                repo_dir=args.repo_dir,
                commit=args.commit,
                mode=args.mode,
                project=args.project,
                source_platform=args.source_platform,
                repo_path=args.repo_path,
                image_repository=args.image_repository,
                image_tag=args.image_tag,
                image_digest=args.image_digest,
                alias_digests=aliases,
                pipeline_run_url=args.pipeline_run_url,
                gitleaks_config=args.gitleaks_config,
                gitleaks_ignore=args.gitleaks_ignore,
                workdir=args.workdir or Path(tmp),
            )
            outcome = publish_snapshot(req, S3Store.from_env())
    except Exception as e:  # noqa: BLE001
        _log(f"error: {type(e).__name__}: {e}")
        payload["reason"] = f"error: {type(e).__name__}: {e}"
        send_failure(webhook, payload)
        return 1
    _log(f"{outcome.value} project={args.project} digest={args.image_digest}")
    code = _EXIT[outcome]
    if code:
        payload["reason"] = outcome.value
        send_failure(webhook, payload)
    return code


if __name__ == "__main__":
    sys.exit(main())
