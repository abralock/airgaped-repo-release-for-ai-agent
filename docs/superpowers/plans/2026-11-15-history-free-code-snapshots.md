# History-free Code Snapshots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the existing production-support AI agent read the exact source code running in production, from scanned, history-free snapshots published by each build pipeline, with no access to Azure DevOps or GitLab.

**Architecture:** One Python package, `snapshotkit`, provides the following.
- **CLIs:**
  - `snapshot-publish` runs in the pipelines. It exports the tree, normalizes the tar, scans it and uploads write-once.
  - `snapshot-alias` maps an extra image digest to an existing snapshot.
  - `snapshot-retention` is a CronJob.
  - `snapshot-transfer` exists only if there is a network gap.
- **An MCP server, `snapshot-mcp`,** that the existing agent connects to. It resolves the running image digest from the production clusters and serves files from the matching snapshot, confined to that snapshot.
- **Thin CI templates.** Azure DevOps and GitLab templates call the same publisher container.
- **Kubernetes manifests** for read-only RBAC and network policy.

**Tech Stack:**
- Python 3.12, using the tarfile `data` filter.
- Libraries: `boto3` (S3-compatible store), `kubernetes`, `mcp` (FastMCP), `PyYAML`, `pytest`.
- Tools in the publisher image: gitleaks ≥ 8.21.2, which has the `dir` command, plus git, git-lfs and crane.
- MinIO for integration tests.
- Kustomize via `kubectl kustomize`.

**Spec:** `docs/superpowers/specs/2026-11-14-history-free-code-snapshots-design.md`

## Global Constraints

- Air-gapped. All images come from the internal registry and all Python packages from the internal index (`PIP_INDEX_URL` build arg). Nothing may fetch from the internet at build or run time.
- The agent, the MCP tool and the retention job never hold Azure DevOps or GitLab credentials. Their pods have no network route to either.
- Snapshots contain no `.git` directory, no commit SHA, no author data and no file timestamps: all mtimes are 0 and owner names are empty.
- The commit SHA never appears in anything the agent can read: snapshot tar, manifest or index.
- A snapshot is published only after the exact bytes being uploaded have passed gitleaks. A scanner error never counts as clean.
- A scan failure never fails the build. Templates use `continueOnError: true` (Azure DevOps) and `allow_failure: true` (GitLab).
- Snapshot keys are write-once per digest (`If-None-Match: *`). Tag index entries are overwritable.
- Store layout (verbatim from spec):
  - `snapshots/<project>/<image-digest>.tar.gz`
  - `snapshots/<project>/<image-digest>.json`
  - an index resolving a digest or `image:tag`
- Manifest fields (verbatim from spec, no others): `project`, `source_platform` (`azure-devops` | `gitlab`), `repo_path`, `image_repository`, `image_tag`, `image_digest`, `pipeline_run_url`, `published_at`, `scanner`, `scanner_version`, `scan_result`.
- Denylist (verbatim from spec): `.env*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `*.kdbx`, `secrets.*`, and `.git*` except `.gitignore` / `.gitattributes`.
- Kubernetes RBAC for the tool: `get` and `list` only, on `pods`, `deployments`, `statefulsets`, `daemonsets` and `replicasets`, in the production namespaces only. No `secrets`, no `configmaps`.
- Retention: keep a snapshot if it is deployed anywhere, or among the 20 newest for its project, or younger than 90 days.
- Message copy fixed by spec: when no snapshot exists, the agent is told `No snapshot available for this version.`

## Calls made in this plan (beyond the spec)

- **Exit codes for `snapshot-publish`:**
  - `0`: published, or already published.
  - `10`: secrets found, not published.
  - `11`: export refused.
  - `1`: any other error, not published.
- The digest is written into keys as `sha256-<hex>`, because `:` in object keys trips some tools.
- The bucket is named `code-snapshots`. Project names must match `^[a-z0-9][a-z0-9._-]{0,99}$`.
- The MCP server gives the agent `list_files`, `read_file` and `search_code`, scoped to a snapshot ID. Confinement is therefore enforced by the tool and doesn't depend on how the existing agent's own file tools are configured. Transport is streamable HTTP on port 8080, run as its own Deployment.
- Owner notification is an optional `SNAPSHOT_NOTIFY_WEBHOOK`, used because GitLab sends no notification for jobs allowed to fail.
- Limits:
  - extraction: 1 GiB uncompressed and 200 000 entries per snapshot;
  - `read_file`: 200 000 bytes;
  - `search_code`: 200 matches;
  - cache: `emptyDir` with a 5Gi size limit.

## Review Focus

1. **`imageID` comes in different forms on different container runtimes:** `docker-pullable://reg/app@sha256:…`, `reg/app@sha256:…`, bare `sha256:…`, or an empty string while a pod is starting. All parseable forms must resolve to the same digest. Empty or unparseable values must be reported as "not running yet" rather than crashing. Tests: Task 1, Task 9.
2. **The scanner binary is missing, crashes, or exits with an unexpected code.** This must never count as clean, nothing may be uploaded, and the exit code must be `1`. Tests: Task 4, Task 6.
3. **One production cluster is unreachable or times out.** `find_deployed_version` must return results from the reachable clusters plus a named error for the failed one. Retention must delete nothing. Tests: Task 9, Task 12.
4. **A snapshot contains `..`, an absolute path, or a symlink pointing outside, or the agent asks for `../../etc/passwd`.** Extraction must refuse the archive, and file reads must refuse paths that resolve outside the snapshot. Tests: Task 2, Task 10.
5. **A repo uses LFS or submodules but the template wasn't switched to checkout mode.** The step must exit `11` with a message naming the template parameter. It must not silently publish LFS pointer files or leave submodule code out. Tests: Task 3.

---

## Task 0 (manual, by the team): Network and digest check — Rollout step 0

Not code. Record the answers in the spec's Constraints section before Task 5 is deployed. They decide whether Task 15 is needed.

- [ ] Can the Azure DevOps build agents and the GitLab runners open HTTPS to a host that the agent's cluster can also reach? Yes means a single store, and Task 15 is skipped. No means two stores plus Task 15.
- [ ] For one pilot service:
  1. Record the digest the pipeline pushed.
  2. Run `kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[*].imageID}'` in production.
  3. Equal means no alias is needed. Different means the promotion pipeline must call `snapshot-alias` (Task 6).
- [ ] Is the pilot image multi-arch? If so, set `multiArch: true` in its template (Task 8).

---

### Task 1: Project scaffold and digest parsing

**Files:**
- Create: `pyproject.toml`, `src/snapshotkit/__init__.py`, `src/snapshotkit/digests.py`, `.gitignore`
- Test: `tests/test_digests.py`

**Interfaces:**
- Produces:
  - `parse_image_id(image_id: str) -> str | None` returns `"sha256:<64 hex>"`, or `None`.
  - `digest_key(digest: str) -> str` returns `"sha256-<hex>"`. It raises `ValueError` on an invalid digest.
  - `platform_digests(manifest_json: str) -> list[str]`.
  - `DIGEST_RE`.

- [ ] **Step 1: Initialise the repo and scaffold.** Run `git init`. In `pyproject.toml`:
  - set `requires-python = ">=3.12"`;
  - declare the dependencies `boto3`, `kubernetes`, `mcp`, `PyYAML`, and the dev extra `pytest`;
  - add the console scripts `snapshot-publish = snapshotkit.publish:main`, `snapshot-alias = snapshotkit.alias:main`, `snapshot-retention = snapshotkit.retention:main`, `snapshot-mcp = snapshotkit.mcp_server:main`, `snapshot-transfer = snapshotkit.transfer:main`;
  - add the pytest markers `integration` and `gitleaks`.

  Then run `pip install -e '.[dev]'`.

- [ ] **Step 2: Write failing tests**

```python
H = "a" * 64
@pytest.mark.parametrize("raw", [f"docker-pullable://reg.local/app@sha256:{H}",
    f"reg.local:5000/team/app@sha256:{H}", f"sha256:{H}"])
def test_parse_image_id_forms(raw): assert parse_image_id(raw) == f"sha256:{H}"
@pytest.mark.parametrize("raw", ["", "reg/app:1.2", "sha256:xyz", f"sha512:{H}"])
def test_parse_image_id_unparseable(raw): assert parse_image_id(raw) is None
def test_digest_key(): assert digest_key(f"sha256:{H}") == f"sha256-{H}"
def test_digest_key_invalid(): pytest.raises(ValueError, digest_key, "latest")
def test_platform_digests_index():
    idx = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
        {"digest": f"sha256:{'b'*64}", "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": f"sha256:{'c'*64}", "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": f"sha256:{'d'*64}", "platform": {"os": "unknown", "architecture": "unknown"}}]}
    assert platform_digests(json.dumps(idx)) == [f"sha256:{'b'*64}", f"sha256:{'c'*64}"]
def test_platform_digests_single_manifest():
    assert platform_digests(json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json", "layers": []})) == []
```

- [ ] **Step 3: Run `pytest tests/test_digests.py -v`.** Expected: FAIL (ImportError).
- [ ] **Step 4: Implement `digests.py`.**
  - `parse_image_id` takes the text after the last `@` if there is one. Otherwise it strips a `scheme://` prefix. It then matches `DIGEST_RE = ^sha256:[0-9a-f]{64}$`.
  - `platform_digests` skips entries whose `platform.os == "unknown"`; these are attestation manifests.
- [ ] **Step 5: Run the tests.** Expected: PASS.
- [ ] **Step 6: Commit** with the message `feat: scaffold snapshotkit and digest parsing`.

---

### Task 2: Normalized tarball writer and safe extraction

**Files:**
- Create: `src/snapshotkit/tarball.py`
- Test: `tests/test_tarball.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Entry(path: str, mode: int, data: bytes | None, link_target: str | None = None)`. `data` is set for files and `link_target` for symlinks.
  - `write_normalized_targz(entries: Iterable[Entry], out_path: Path) -> None`.
  - `safe_extract(targz: Path, dest: Path, max_total_bytes: int = 1 << 30, max_entries: int = 200_000) -> None`.
  - `class UnsafeArchiveError(Exception)`.

- [ ] **Step 1: Write failing tests**
  - `test_output_is_deterministic`: writing the same entries in a different order twice gives byte-identical files.
  - `test_metadata_is_stripped`: every member has `mtime == 0`, `uid == gid == 0` and `uname == gname == ""`, and `tarfile.open(...).pax_headers == {}`. The gzip header bytes 4–8 (the mtime field) are `b"\0\0\0\0"`.
  - `test_modes_normalized`: an entry with mode `0o700` comes out as `0o755`, and `0o600` comes out as `0o644`.
  - `test_long_path_without_pax`: a 180-character path round-trips, and `pax_headers == {}`.
  - `test_safe_extract_roundtrip`: files and an inside-pointing symlink extract correctly.
  - `test_safe_extract_rejects` is parametrized over hand-built tars containing `../evil`, `/etc/evil` and a symlink `link -> ../../etc/passwd`. Each raises `UnsafeArchiveError`, and `dest` stays empty.
  - `test_safe_extract_limits`: an archive over `max_total_bytes`, or over `max_entries`, raises `UnsafeArchiveError` before anything is written.

- [ ] **Step 2: Run `pytest tests/test_tarball.py -v`.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **Writer:** sort entries by path and use `tarfile.GNU_FORMAT`, so long names don't need pax headers. Write the gzip with `gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="")`. Any executable bit becomes `0o755`; everything else becomes `0o644`.
  - **Extract:** first pass over the members to check the limits. Then `tar.extractall(dest, filter="data")`, with `tarfile.FilterError` re-raised as `UnsafeArchiveError`.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat: normalized tarball writer and safe extract`.

---

### Task 3: History-free export (archive mode and checkout mode) with denylist

**Files:**
- Create: `src/snapshotkit/denylist.py`, `src/snapshotkit/export.py`
- Test: `tests/test_denylist.py`, `tests/test_export.py`, `tests/conftest.py` (fixture `make_repo`)

**Interfaces:**
- Consumes: `Entry` and `write_normalized_targz` (Task 2).
- Produces:
  - `DEFAULT_DENYLIST: tuple[str, ...]`.
  - `is_denied(path: str, patterns: Sequence[str] = DEFAULT_DENYLIST) -> bool`.
  - `class ExportError(Exception)`, with an attribute `reason: Literal["export-subst", "needs-checkout-mode", "lfs-not-pulled"]` and a human message.
  - `@dataclass ExportStats(files_included: int, files_denied: list[str])`.
  - `export_from_archive(repo_dir: Path, commit: str, out_path: Path, patterns=DEFAULT_DENYLIST) -> ExportStats`.
  - `export_from_checkout(repo_dir: Path, out_path: Path, patterns=DEFAULT_DENYLIST) -> ExportStats`.

- [ ] **Step 1: Write the failing denylist tests.**
  - Denied: `config/secrets.yaml`, `a/.env.prod`, `certs/server.pem`, `keys/id_rsa.pub`, `.git/config`, `.github/workflows/ci.yml`, `.gitlab-ci.yml`, `vault.kdbx`.
  - Allowed: `src/main.py`, `src/.gitignore`, `.gitattributes`, `docs/key.md`, `envoy.yaml`.

  Rule: a path is denied if any of its components matches a pattern via `fnmatch`. The only exception is a final component equal to `.gitignore` or `.gitattributes`.
- [ ] **Step 2: Write the failing export tests.** `make_repo(tmp_path, files: dict[str, str | bytes], commit=True) -> tuple[Path, str]` returns the repo dir and the commit SHA. It uses `git -c user.name=T -c user.email=t@x commit`.
  - `test_archive_has_no_git_or_history`: the output contains no `.git` member. The commit SHA, `t@x` and the commit message don't appear in the gunzipped bytes.
  - `test_archive_applies_export_ignore`: with `.gitattributes` set to `internal/ export-ignore`, `internal/a.txt` is absent.
  - `test_archive_applies_denylist`: `.env` and `secrets.json` are absent and listed in `files_denied`. `src/app.py` is present.
  - `test_archive_refuses_export_subst`: `.gitattributes` set to `*.txt export-subst` raises `ExportError`, with `reason == "export-subst"`, and no output file is created.
  - `test_archive_refuses_submodules`: a repo containing `.gitmodules` raises `reason == "needs-checkout-mode"`. The message contains `withSubmodulesOrLfs`.
  - `test_archive_refuses_lfs_attributes`: `.gitattributes` set to `*.bin filter=lfs diff=lfs merge=lfs -text` raises `needs-checkout-mode`.
  - `test_checkout_includes_submodule_files`: the superproject has a submodule added with `git -c protocol.file.allow=always submodule add`, and the submodule's `lib/x.py` is present in the output.
  - `test_checkout_refuses_unpulled_lfs`: a tracked file whose content starts with `version https://git-lfs.github.com/spec/v1` raises `reason == "lfs-not-pulled"`.
  - `test_checkout_applies_export_ignore_and_denylist`: same checks as the archive tests.
- [ ] **Step 3: Run `pytest tests/test_denylist.py tests/test_export.py -v`.** Expected: FAIL.
- [ ] **Step 4: Implement.**
  - **Archive mode.**
    - Inspect the tree with `git ls-tree -r --name-only <commit>` and `git show <commit>:<path>` for every `.gitattributes`. Refuse if any contains `export-subst` or `filter=lfs`, or if `.gitmodules` is at the root.
    - Then stream `git archive --format=tar <commit>^{tree}` into `tarfile`. Archiving the tree means there's no commit pax header.
    - Drop denied members and convert the rest to `Entry`.
  - **Checkout mode.**
    - List files with `git ls-files -z --recurse-submodules`.
    - Run `git check-attr -z export-ignore` in the repository that owns each path; for submodule paths, run it in the submodule directory.
    - Refuse the `export-subst` attribute in the same way.
    - Refuse LFS pointer content.
    - Read symlinks as links, not as their targets.
- [ ] **Step 5: Run the tests.** Expected: PASS.
- [ ] **Step 6: Commit** with the message `feat: history-free export with denylist and export-subst refusal`.

---

### Task 4: gitleaks scan wrapper

**Files:**
- Create: `src/snapshotkit/scan.py`, `config/gitleaks.toml`
- Test: `tests/test_scan.py`

**Interfaces:**
- Produces:
  - `@dataclass ScanResult(clean: bool, findings: int, report_path: Path)`.
  - `class ScannerError(Exception)`.
  - `run_gitleaks(tree_dir: Path, config: Path, report_path: Path, ignore_file: Path | None = None, gitleaks_bin: str = "gitleaks") -> ScanResult`.
  - `gitleaks_version(gitleaks_bin: str = "gitleaks") -> str`.

- [ ] **Step 1: Write the failing tests.** The unit tests use a fake `gitleaks` shell script written to `tmp_path`. The fake records its argv and exits with a configurable code.
  - `test_clean_on_exit_0`.
  - `test_findings_on_exit_1`: the fake writes a JSON array of 2 items to the report path, so `clean is False` and `findings == 2`.
  - `test_unexpected_exit_raises`: exit code 2 raises `ScannerError`.
  - `test_missing_binary_raises`: `gitleaks_bin="/nonexistent"` raises `ScannerError`.
  - `test_argv`: the recorded argv starts with `["dir", str(tree_dir)]` and contains `--config`, `--report-format json`, `--report-path`, `--exit-code 1`, `--redact` and `--no-banner`. It contains `--gitleaks-ignore-path <file>` only when `ignore_file` is given.
  - Marked `@pytest.mark.gitleaks` and skipped when `shutil.which("gitleaks")` is None: `test_real_gitleaks_detects_aws_key`. A file containing `AKIAIOSFODNN7EXAMPLE` plus a 40-character secret-key-shaped string gives `clean is False`.
- [ ] **Step 2: Run `pytest tests/test_scan.py -v`.** Expected: FAIL.
- [ ] **Step 3: Implement** with `subprocess.run`, no shell. In `config/gitleaks.toml`, set `[extend] useDefault = true` and add a comment that org rules go below. Tuning the rules is left to the builder, per the spec.
- [ ] **Step 4: Run the tests.** Expected: PASS. The gitleaks-marked test is skipped if the binary is absent.
- [ ] **Step 5: Commit** with the message `feat: gitleaks wrapper that never treats scanner errors as clean`.

---

### Task 5: Snapshot store (layout, in-memory fake, S3)

**Files:**
- Create: `src/snapshotkit/store.py`, `tests/integration/compose.yaml`
- Test: `tests/test_store.py`, `tests/integration/test_s3_store.py`

**Interfaces:**
- Consumes: `digest_key` (Task 1).
- Produces:
  - `validate_project(name: str) -> str`, which raises `ValueError`.
  - Key builders:
    - `snapshot_key(project, digest) -> "snapshots/<project>/<digest_key>.tar.gz"`
    - `manifest_key(project, digest) -> "snapshots/<project>/<digest_key>.json"`
    - `digest_index_key(digest) -> "index/digest/<digest_key>.json"`
    - `tag_index_key(image_repository, tag) -> "index/tag/<image_repository>/<tag>.json"`
  - The `SnapshotStore` Protocol:
    - `put_new(key: str, data: bytes) -> bool` returns `False` if the key exists.
    - `put(key: str, data: bytes) -> None`.
    - `get(key: str) -> bytes | None`.
    - `list_keys(prefix: str) -> Iterator[str]`.
    - `delete(key: str) -> None`.
  - `InMemoryStore`.
  - `S3Store(endpoint_url, bucket, access_key, secret_key, region="us-east-1")`, with `S3Store.from_env()`. It reads `SNAPSHOT_STORE_ENDPOINT`, `SNAPSHOT_STORE_BUCKET` (default `code-snapshots`), `SNAPSHOT_STORE_ACCESS_KEY` and `SNAPSHOT_STORE_SECRET_KEY`.
  - An index entry's JSON body is `{"project": str, "snapshot_digest": str}`.

- [ ] **Step 1: Write the failing unit tests.**
  - The key builders give exact strings for a fixed digest.
  - `validate_project` rejects `""`, `"../x"`, `"A"`, `"a/b"` and a 101-character name. It accepts `"orders-api"`.
  - `InMemoryStore.put_new` returns `True` and then `False`, and the second call does not overwrite.
  - `list_keys` filters by prefix.
- [ ] **Step 2: Write the failing integration test.** It is marked `integration` and runs against MinIO from `tests/integration/compose.yaml`, whose image comes from `${MINIO_IMAGE}`. It runs the same contract tests against `S3Store`, plus: `put_new` on an existing key returns `False` and leaves the original bytes. That confirms the deployed MinIO honours `If-None-Match`.
- [ ] **Step 3: Run `pytest tests/test_store.py -v`.** Expected: FAIL.
- [ ] **Step 4: Implement.**
  - `S3Store.put_new` uses `put_object(..., IfNoneMatch="*")` and maps `ClientError` with HTTP status 412 to `False`.
  - `get` maps `NoSuchKey` to `None`.
  - `list_keys` uses the `list_objects_v2` paginator.
- [ ] **Step 5: Run the unit tests.** Then run `docker compose -f tests/integration/compose.yaml up -d && pytest -m integration tests/integration/test_s3_store.py -v`. Expected: PASS.
- [ ] **Step 6: Commit** with the message `feat: snapshot store layout with write-once S3 backend`.

---

### Task 6: Manifest, publish orchestration and CLIs

**Files:**
- Create: `src/snapshotkit/manifest.py`, `src/snapshotkit/notify.py`, `src/snapshotkit/publish.py`, `src/snapshotkit/alias.py`
- Test: `tests/test_manifest.py`, `tests/test_notify.py`, `tests/test_publish.py`, `tests/test_alias.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces:
  - `@dataclass(frozen=True) Manifest` holds exactly the spec fields. `published_at` is an ISO-8601 UTC string. `scan_result` is `Literal["clean", "clean-with-reviewed-allowlist"]`. Methods: `.to_json() -> bytes` and `Manifest.from_json(b) -> Manifest`.
  - `send_failure(webhook_url: str | None, payload: dict) -> None` never raises. The payload keys are `project`, `image_digest`, `reason` and `pipeline_run_url`.
  - `@dataclass PublishRequest(repo_dir: Path, commit: str, mode: Literal["archive", "checkout"], project: str, source_platform: str, repo_path: str, image_repository: str, image_tag: str, image_digest: str, alias_digests: list[str], pipeline_run_url: str, gitleaks_config: Path, gitleaks_ignore: Path | None, workdir: Path)`.
  - `class Outcome(Enum)`, with the members `PUBLISHED`, `ALREADY_PUBLISHED`, `SECRETS_FOUND` and `EXPORT_REFUSED`.
  - `publish_snapshot(req, store, scanner=run_gitleaks, scanner_version=gitleaks_version, now=lambda: datetime.now(UTC)) -> Outcome`. The manifest gets `scanner="gitleaks"`, and tests inject a fake `scanner_version`.
  - `publish.main(argv: list[str] | None = None) -> int`.
  - `alias.main(argv) -> int`.

- [ ] **Step 1: Write failing tests for the manifest and notify.**
  - Manifest: round-trips; `json.loads(to_json())` has exactly the 11 spec keys; an invalid `source_platform` or `scan_result` raises `ValueError`.
  - Notify: against a local `http.server` thread, it POSTs JSON with the 4 keys. An unreachable URL returns normally within 6 seconds.
- [ ] **Step 2: Write failing publish tests** using `InMemoryStore`, a `make_repo` repo and a fake scanner.
  - `test_publishes_tar_manifest_and_index`: the store has the tar, the manifest, `index/digest/<primary>`, `index/digest/<each alias>` and `index/tag/<repo>/<tag>`, and the result is `PUBLISHED`.
  - `test_scans_exact_upload`: the fake scanner receives a directory whose file set equals the uploaded tar's member set.
  - `test_secrets_found_writes_nothing`: the fake returns `clean=False`, the outcome is `SECRETS_FOUND`, and `list(store.list_keys("")) == []`.
  - `test_scanner_error_writes_nothing`: the fake raises `ScannerError`. `publish_snapshot` re-raises it, and the store is empty.
  - `test_export_refused`: an `export-subst` repo gives `EXPORT_REFUSED` and an empty store.
  - `test_rerun_same_digest_is_already_published`: a second call gives `ALREADY_PUBLISHED`, the tar bytes are unchanged, and the index entries still exist.
  - `test_index_written_last`: wrap the store to record the order of writes. Every `index/` write comes after the tar and the manifest.
  - `test_reviewed_allowlist_sets_scan_result`: with `gitleaks_ignore` set, the manifest has `scan_result == "clean-with-reviewed-allowlist"`.
  - `test_no_commit_sha_anywhere`: the commit SHA does not occur in any stored object, including the gunzipped tar.
  - CLI: `test_main_exit_codes` monkeypatches `publish_snapshot` and the store. It checks that `PUBLISHED` and `ALREADY_PUBLISHED` give 0, `SECRETS_FOUND` gives 10, `EXPORT_REFUSED` gives 11, and an exception gives 1. `send_failure` is called for 10, 11 and 1 only.
  - `test_main_platform_manifest`: `--platform-manifest file.json` adds `platform_digests(...)` to `alias_digests`.
- [ ] **Step 3: Write failing alias tests.** `snapshot-alias --project p --digest D --alias-digest A` writes `index/digest/<A>` with the body `{"project": "p", "snapshot_digest": D}`. If the entry already exists, it exits 0 with `alias already exists`. Invalid digests exit 1.
- [ ] **Step 4: Run `pytest tests/test_manifest.py tests/test_notify.py tests/test_publish.py tests/test_alias.py -v`.** Expected: FAIL.
- [ ] **Step 5: Implement.**
  - **The publish sequence:**
    1. Export to `workdir/snapshot.tar.gz`.
    2. `safe_extract` it to `workdir/tree`.
    3. Scan `workdir/tree`.
    4. `put_new` the tar.
    5. `put_new` the manifest.
    6. `put_new` each digest index entry: primary first, then the aliases.
    7. `put` the tag index entry.
  - `ALREADY_PUBLISHED` means the tar's `put_new` returned `False`; steps 5–7 still run, which makes re-runs idempotent.
  - **CLI:**
    - The flags are kebab-case versions of the `PublishRequest` fields: `--repo-dir`, `--commit`, `--mode`, `--project`, `--source-platform`, `--repo-path`, `--image-repository`, `--image-tag`, `--image-digest`, `--alias-digest` (repeatable), `--platform-manifest`, `--pipeline-run-url`, `--gitleaks-config`, `--gitleaks-ignore`, `--workdir` (defaults to a temp dir).
    - `SNAPSHOT_NOTIFY_WEBHOOK` is read from the environment.
    - The log names the outcome, the project and the digest. It never prints file contents.
- [ ] **Step 6: Run the tests.** Expected: PASS.
- [ ] **Step 7: Commit** with the message `feat: snapshot-publish and snapshot-alias CLIs`.

---

### Task 7: Publisher container image

**Files:**
- Create: `docker/Dockerfile` (the `publisher` target; Task 11 adds `tool`), `.dockerignore`
- Test: `tests/test_dockerfile.py`

**Interfaces:**
- Produces an image whose entrypoint is a shell, with `snapshot-publish`, `snapshot-alias`, `gitleaks`, `git`, `git-lfs` and `crane` on `PATH`. The gitleaks config is at `/etc/snapshotkit/gitleaks.toml`. The process runs as non-root uid 10001.

- [ ] **Step 1: Write a failing static test.** It parses `docker/Dockerfile` and asserts:
  - every `FROM` uses `${BASE_IMAGE}`;
  - the build args `BASE_IMAGE`, `PIP_INDEX_URL`, `GITLEAKS_URL`, `GITLEAKS_SHA256`, `CRANE_URL` and `CRANE_SHA256` exist;
  - each downloaded binary is checked with `sha256sum -c`;
  - the file contains no literal `github.com` or `pypi.org`.
- [ ] **Step 2: Run `pytest tests/test_dockerfile.py -v`.** Expected: FAIL.
- [ ] **Step 3: Write the `publisher` stage.** Copy `config/gitleaks.toml`. Pin gitleaks ≥ 8.21.2 through `GITLEAKS_URL`, which points at the internal mirror.
- [ ] **Step 4: Verify.**
  - Run `pytest tests/test_dockerfile.py -v`. Expected: PASS.
  - Run `docker build --target publisher --build-arg BASE_IMAGE=… --build-arg PIP_INDEX_URL=… … -t snapshot-publisher:dev docker/..`.
  - Run `docker run --rm snapshot-publisher:dev sh -c 'gitleaks version && git lfs version && crane version && snapshot-publish --help'`. Expected: all four print, and the exit code is 0.
- [ ] **Step 5: Commit** with the message `feat: air-gap publisher image`.

---

### Task 8: Azure DevOps and GitLab templates

**Files:**
- Create: `templates/azure-devops/snapshot-publish-job.yml`, `templates/gitlab/snapshot-publish.gitlab-ci.yml`, `templates/examples/azure-pipelines.example.yml`, `templates/examples/gitlab-ci.example.yml`
- Test: `tests/test_templates.py`

**Interfaces:**
- Consumes: the publisher image (Task 7) and the `snapshot-publish` flags (Task 6).
- Produces:
  - **Azure DevOps job template parameters:** `dependsOn` (string), `project` (string), `imageRepository`, `imageTag`, `imageDigest` (string; the consumer passes a runtime expression such as `$[ dependencies.Build.outputs['push.imageDigest'] ]`), `withSubmodulesOrLfs` (boolean, default false), `multiArch` (boolean, default false), `publisherImage` (string) and `variableGroup` (string, default `snapshot-store`).
  - **GitLab hidden job:** `.snapshot-publish`. It reads `IMAGE_REPOSITORY`, `IMAGE_TAG` and `IMAGE_DIGEST` (from the build job's dotenv artifact via `needs`), `SNAPSHOT_MODE` (default `archive`), `SNAPSHOT_PROJECT` (default `$CI_PROJECT_NAME`), `SNAPSHOT_MULTI_ARCH` (default `"false"`) and `SNAPSHOT_PUBLISHER_IMAGE`.

- [ ] **Step 1: Write failing tests** that load the YAML with PyYAML.
  - **Azure DevOps:**
    - The job has `continueOnError: true` and uses `${{ parameters.publisherImage }}` as its container.
    - Checkout uses `fetchDepth: 1`.
    - When `withSubmodulesOrLfs` is set, `submodules: recursive` and `lfs: true` are switched on.
    - The script contains `--source-platform azure-devops`, `--commit $(Build.SourceVersion)` and `--pipeline-run-url "$(System.CollectionUri)$(System.TeamProject)/_build/results?buildId=$(Build.BuildId)"`.
    - Exit code 10 produces `##vso[task.logissue type=warning]`.
    - The `multiArch` branch runs `crane manifest` into `--platform-manifest`.
  - **GitLab:**
    - `.snapshot-publish.allow_failure is True` and `variables.GIT_DEPTH == "1"`.
    - The script contains `--source-platform gitlab`, `--commit "$CI_COMMIT_SHA"`, `--repo-path "$CI_PROJECT_PATH"` and `--pipeline-run-url "$CI_PIPELINE_URL"`.
  - **Both:** neither template contains `SNAPSHOT_STORE_SECRET_KEY` as a literal value; it comes from the variable group or CI variables only.
  - **Examples:** both example files reference their template and are valid YAML.
- [ ] **Step 2: Run `pytest tests/test_templates.py -v`.** Expected: FAIL.
- [ ] **Step 3: Write the templates and the examples.** The Azure DevOps example consumes the template through `resources.repositories` and `template: snapshot-publish-job.yml@templates`. The GitLab example uses `include: project:` plus `extends: .snapshot-publish`.
- [ ] **Step 4: Run the tests and `yamllint templates/`.** Expected: PASS. Real pipeline runs are verified in Task 16.
- [ ] **Step 5: Commit** with the message `feat: ADO and GitLab snapshot-publish templates`.

---

### Task 9: Cluster config and deployed-version lookup

**Files:**
- Create: `src/snapshotkit/clusters.py`, `src/snapshotkit/lookup.py`, `deploy/agent-cluster/clusters-config.example.yaml`
- Test: `tests/test_clusters.py`, `tests/test_lookup.py`

**Interfaces:**
- Consumes: `parse_image_id` (Task 1).
- Produces:
  - `@dataclass ClusterConfig(name: str, server: str, ca_file: Path, token_file: Path, namespaces: list[str])`.
  - `load_cluster_configs(path: Path) -> list[ClusterConfig]`. The YAML top-level key is `clusters:`. It raises `ValueError` on duplicate names, empty `namespaces` or missing fields.
  - `@dataclass PodContainer(pod: str, container: str, image: str, image_id: str)`.
  - The `ClusterReader` Protocol:
    - `workload_pods(namespace: str, workload: str) -> list[PodContainer]` looks for a Deployment, then a StatefulSet, then a DaemonSet with that name, and returns `[]` if none exists.
    - `all_image_ids() -> set[str]` returns the parsed digests of all pods across the configured namespaces.
  - `KubeClusterReader(config: ClusterConfig, timeout_seconds: int = 10)`.
  - `@dataclass DeployedVersion(cluster: str, namespace: str, workload: str, container: str, image: str, digest: str | None, pods: int)`.
  - `@dataclass LookupResult(versions: list[DeployedVersion], errors: dict[str, str])`.
  - `find_deployed_version(readers: dict[str, ClusterReader], allowed_namespaces: dict[str, list[str]], namespace: str, workload: str, cluster: str | None = None) -> LookupResult`.
  - `build_readers(configs) -> tuple[dict[str, ClusterReader], dict[str, list[str]]]`.

- [ ] **Step 1: Write failing tests** using `FakeClusterReader`.
  - `test_groups_pods_by_digest`: 3 pods on digest X give one `DeployedVersion(pods=3)`.
  - `test_mid_rollout_returns_both`: 2 pods on X and 1 pod on Y give two versions.
  - `test_starting_pod_has_none_digest`: `image_id=""` gives `digest is None` and no exception.
  - `test_all_clusters_when_cluster_omitted`: matches from `prod-a` and `prod-b` are both returned.
  - `test_unreachable_cluster_reported`: the `prod-b` reader raises `TimeoutError`. The result has `prod-a`'s versions and `errors == {"prod-b": <message containing "TimeoutError">}`.
  - `test_unknown_cluster`: `cluster="nope"` gives `errors["nope"] == "unknown cluster"`.
  - `test_namespace_not_allowed`: a namespace outside the cluster's list gives `errors[<cluster>] == "namespace not allowed"`, and the reader is not called.
  - `test_load_cluster_configs_validation`: the example file loads, and duplicates or empty namespaces raise.
  - `test_kube_reader_uses_selector_and_timeout`: with `MagicMock` `AppsV1Api` and `CoreV1Api`, `list_namespaced_pod` is called with `label_selector` built from `spec.selector.match_labels` and `_request_timeout=10`.
- [ ] **Step 2: Run `pytest tests/test_clusters.py tests/test_lookup.py -v`.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `KubeClusterReader` builds a `kubernetes.client.Configuration` with `host`, `ssl_ca_cert` and `api_key={"authorization": "Bearer <token>"}`. It reads the token on each call, so rotated tokens are picked up.
  - `matchExpressions` selectors are converted to selector strings.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat: multi-cluster deployed-version lookup`.

---

### Task 10: Snapshot fetch and confined file access

**Files:**
- Create: `src/snapshotkit/snapshots.py`, `src/snapshotkit/files.py`
- Test: `tests/test_snapshots.py`, `tests/test_files.py`

**Interfaces:**
- Consumes: the store (Task 5), `safe_extract` (Task 2) and `digest_key` / `parse_image_id` (Task 1).
- Produces:
  - `class SnapshotNotFound(Exception)`; its message is `No snapshot available for this version.`
  - `@dataclass SnapshotInfo(snapshot_id: str, path: Path, project: str, manifest: dict)`. `snapshot_id` is the primary digest key.
  - `SnapshotCache(store: SnapshotStore, cache_root: Path)`, with:
    - `.get_snapshot(ref: str) -> SnapshotInfo`. `ref` is a digest, or `image_repository:tag`, where the tag is the text after the last `:` that comes after the last `/`.
    - `.path_for(snapshot_id: str) -> Path`, which raises `SnapshotNotFound` if the snapshot isn't cached.
  - `class PathOutsideSnapshot(Exception)`.
  - `resolve_inside(root: Path, rel: str) -> Path`.
  - `list_files(root: Path, subdir: str = "", limit: int = 2000) -> dict`, which returns `{"files": [...], "truncated": bool}`.
  - `read_file(root: Path, rel_path: str, max_bytes: int = 200_000) -> dict`, which returns `{"content": str, "truncated": bool}`.
  - `search_code(root: Path, pattern: str, glob: str | None = None, max_matches: int = 200) -> dict`, which returns `{"matches": [{"path", "line", "text"}], "truncated": bool}`.

- [ ] **Step 1: Write failing tests.**
  - **Snapshots:**
    - `test_resolves_alias_digest_to_primary`.
    - `test_resolves_tag_ref`.
    - `test_missing_index_raises_not_found` checks the exact message.
    - `test_index_present_tar_missing_raises_not_found`.
    - `test_second_call_uses_cache`: a store wrapper counts `get` calls on the tar key, and the count is 1 after two calls.
    - `test_concurrent_extract_is_atomic`: two threads fetch the same digest. Both succeed, and there is exactly one directory, with no `.tmp` directory left behind.
  - **Files:**
    - `test_read_file_traversal_refused`: `../../etc/passwd` raises `PathOutsideSnapshot`.
    - `test_symlink_outside_refused`: a symlink inside root pointing at `tmp_path/outside.txt` raises `PathOutsideSnapshot` on `read_file`.
    - `test_read_file_truncates`.
    - `test_binary_file_reported`: a file with a NUL byte in its first 8 KB gives `{"content": "", "truncated": False, "binary": True}`.
    - `test_search_limit_and_glob`.
    - `test_invalid_regex_message`: an invalid regex raises `ValueError`, and the message starts with `invalid pattern:`.
    - `test_list_files_sorted_and_limited`.
- [ ] **Step 2: Run `pytest tests/test_snapshots.py tests/test_files.py -v`.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Extract into `cache_root/<id>.tmp-<uuid>`, then `os.rename` to `cache_root/<id>`. If the rename loses the race, remove the temp directory.
  - `resolve_inside` uses `(root / rel).resolve()` and `is_relative_to(root.resolve())`.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat: snapshot cache and confined file access`.

---

### Task 11: MCP server, tool image and end-to-end test

**Files:**
- Create: `src/snapshotkit/mcp_server.py`, `tests/integration/test_end_to_end.py`
- Modify: `docker/Dockerfile` (add the `tool` target)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: Tasks 9 and 10.
- Produces:
  - `build_server(readers, allowed_namespaces, cache: SnapshotCache, host="0.0.0.0", port=8080) -> FastMCP`, with the server name `code-snapshots`.
  - Tools, each returning a JSON-serialisable dict:
    - `find_deployed_version(namespace: str, workload: str, cluster: str | None = None)` returns `{"versions": [...], "errors": {...}}`.
    - `get_snapshot(ref: str)` returns `{"snapshot_id", "project", "manifest"}`, with no filesystem path. If nothing matches it returns `{"error": "No snapshot available for this version."}`.
    - `list_files(snapshot_id: str, subdir: str = "")`.
    - `read_file(snapshot_id: str, path: str)`.
    - `search_code(snapshot_id: str, pattern: str, glob: str | None = None)`.
  - `main()` reads `SNAPSHOT_TOOL_CLUSTERS` (the config path), `SNAPSHOT_CACHE_DIR` (default `/cache`) and the store environment variables, then runs `transport="streamable-http"`.
  - Tool descriptions (exact copy):
    - `find_deployed_version`: "Find which image digest is running for a workload in production. If more than one digest is returned, a rollout is in progress: tell the user. If a cluster is listed under errors, say its version could not be checked."
    - `get_snapshot`: "Fetch the source code snapshot for an image digest returned by find_deployed_version. If there is no snapshot, tell the user 'No snapshot available for this version.' and do not answer from another version's code."

- [ ] **Step 1: Write failing tests.**
  - `test_tools_registered`: `asyncio.run(server.list_tools())` returns exactly those 5 names, with the descriptions above.
  - `test_get_snapshot_not_found_message`.
  - `test_get_snapshot_hides_path`: the result has no value containing `str(cache_root)`.
  - `test_file_tools_require_fetched_snapshot`: `read_file` on an unknown ID returns `{"error": "No snapshot available for this version."}`.
  - `test_errors_are_returned_not_raised`: `PathOutsideSnapshot` and `ValueError` come back as `{"error": <message>}`.
- [ ] **Step 2: Write the failing e2e test.** It is marked `integration` and runs against MinIO.
  1. Run `publish.main([...])` on a `make_repo` repo, using the real `S3Store` and a fake scanner that is always clean.
  2. Build a `FakeClusterReader` returning that digest as an `imageID` of the form `docker-pullable://…@sha256:…`.
  3. Call the tool handlers `find_deployed_version`, then `get_snapshot`, then `read_file("src/app.py")`.
  4. The content matches the repo file, and a `.git` path is not listed.
- [ ] **Step 3: Run `pytest tests/test_mcp_server.py -v`.** Expected: FAIL.
- [ ] **Step 4: Implement.** Register the tools with `@server.tool(description=...)`. Add the Dockerfile `tool` stage: base `${BASE_IMAGE}`, Python only, no git or gitleaks, uid 10001, `ENTRYPOINT ["snapshot-mcp"]`, `EXPOSE 8080`.
- [ ] **Step 5: Verify.**
  - Run `pytest tests/test_mcp_server.py -v`. Expected: PASS.
  - Run `pytest -m integration tests/integration/test_end_to_end.py -v`. Expected: PASS.
  - Run `docker build --target tool …`, then `docker run --rm --entrypoint sh snapshot-tool:dev -c 'command -v git || echo no-git'`. Expected: `no-git`.
- [ ] **Step 6: Commit** with the message `feat: code-snapshots MCP server and tool image`.

---

### Task 12: Retention job

**Files:**
- Create: `src/snapshotkit/retention.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: the store (Task 5), `Manifest` (Task 6) and `ClusterReader.all_image_ids` / `build_readers` (Task 9).
- Produces:
  - `plan_retention(store, deployed: set[str], now: datetime, keep_last: int = 20, min_age_days: int = 90) -> list[tuple[str, str]]`, returning `(project, primary_digest)` pairs.
  - `apply_retention(store, doomed: list[tuple[str, str]]) -> None`.
  - `main(argv) -> int`, with `--dry-run`. Exit codes: `0` ok, `2` a cluster was unreachable (nothing deleted), `1` other error.

- [ ] **Step 1: Write failing tests** using `InMemoryStore` and manifests with chosen `published_at` values.
  - `test_keeps_deployed_even_if_old`: a 200-day-old snapshot whose **alias** digest is deployed is kept.
  - `test_keeps_newest_20_per_project`: of 25 old snapshots in project A, the 5 oldest are doomed. A project B with 3 old snapshots has none doomed.
  - `test_keeps_younger_than_90_days`: of 25 snapshots, if the 5 oldest are 30 days old, none are doomed.
  - `test_apply_deletes_index_before_objects`: the write-order log shows every `index/` delete (digest aliases and tag entries pointing to the snapshot) before the `.json` and `.tar.gz` deletes.
  - `test_unreachable_cluster_deletes_nothing`: one fake reader raises. `main` returns 2, and the store is unchanged.
  - `test_dry_run_deletes_nothing`: it prints the doomed keys and exits 0.
- [ ] **Step 2: Run `pytest tests/test_retention.py -v`.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Build an alias-to-primary map from `index/digest/*` bodies, and a tag-entry-to-primary map from `index/tag/**`.
  - A snapshot counts as deployed if its primary digest or any of its aliases is in `deployed`.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat: snapshot retention that aborts on any unreachable cluster`.

---

### Task 13: Store policies and Kubernetes manifests

**Files:**
- Create:
  - `deploy/store/policies/pipeline-writer.template.json`
  - `deploy/store/policies/tool-reader.json`
  - `deploy/store/policies/retention-admin.json`
  - `deploy/store/make-writer-policy.sh`
  - `deploy/prod-cluster-rbac/base/{kustomization,namespace,serviceaccount,token-secret,clusterrole}.yaml`
  - `deploy/prod-cluster-rbac/overlays/example/{kustomization,rolebindings}.yaml`
  - `deploy/agent-cluster/base/{kustomization,namespace,tool-deployment,tool-service,networkpolicy-tool,networkpolicy-agent,retention-cronjob}.yaml`
  - `deploy/agent-cluster/overlays/example/kustomization.yaml`
- Test: `tests/test_deploy.py`

**Interfaces:**
- Consumes:
  - the images from Tasks 7 and 11;
  - the environment variable names from Tasks 5, 9 and 11;
  - the Secret names `snapshot-store-reader` (tool), `snapshot-store-admin` (retention) and `snapshot-clusters` (cluster config plus each cluster's `ca.crt` and `token`, mounted at `/etc/snapshotkit/clusters`).
- Produces:
  - in each production cluster: the namespace `snapshot-reader`, the ServiceAccount `snapshot-reader`, the token Secret `snapshot-reader-token` and the ClusterRole `snapshot-reader`, bound by a RoleBinding in each production namespace;
  - in the agent cluster: the namespace `code-snapshots`, the Deployment and Service `snapshot-mcp` (port 8080) and the CronJob `snapshot-retention` (`0 3 * * *`, `concurrencyPolicy: Forbid`).

- [ ] **Step 1: Write failing tests.** They render with `subprocess.run(["kubectl", "kustomize", <overlay>])` and parse with PyYAML.
  - **Production RBAC:**
    - The ClusterRole has exactly two rules: core `pods` and `apps` `deployments`, `statefulsets`, `daemonsets` and `replicasets`. Both have verbs `["get", "list"]`.
    - No rule mentions `secrets` or `configmaps`.
    - There is no `ClusterRoleBinding`.
    - Every `RoleBinding` references the ClusterRole `snapshot-reader`.
  - **Agent cluster:**
    - The tool Deployment has `automountServiceAccountToken: false`, `readOnlyRootFilesystem: true` and `runAsNonRoot: true`.
    - The cache volume is an `emptyDir` with `sizeLimit: 5Gi`.
    - No env var name contains `AZURE`, `ADO`, `GITLAB` or `GIT_`.
    - The NetworkPolicy egress has no `0.0.0.0/0`.
    - The tool's ingress only allows pods matching the agent label from the overlay.
    - The CronJob command is `["snapshot-retention"]` and it uses `snapshot-store-admin`.
  - **Store policies:**
    - `pipeline-writer.template.json` allows only `s3:PutObject`, on `code-snapshots/snapshots/PROJECT/*` and `code-snapshots/index/*`.
    - `tool-reader.json` allows only `s3:GetObject` and `s3:ListBucket`.
    - `make-writer-policy.sh orders-api` substitutes `PROJECT` and rejects names that fail `^[a-z0-9][a-z0-9._-]{0,99}$`.
- [ ] **Step 2: Run `pytest tests/test_deploy.py -v`.** Expected: FAIL.
- [ ] **Step 3: Write the manifests.** The overlays carry the environment-specific values, with obviously example data: API server CIDRs, store CIDR and port, the model endpoint CIDR, the agent namespace and pod labels, and the image references.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat: least-privilege store policies and k8s manifests`.

---

### Task 14: Runbooks (backfill, republish after a false positive, onboarding)

**Files:**
- Create: `docs/runbooks/backfill-and-republish.md`, `docs/runbooks/onboarding.md`, `README.md`

**Interfaces:**
- Consumes: the CLI flags (Task 6), the template parameters (Task 8), the tools (Task 11) and the manifests (Task 13).

- [ ] **Step 1: Write `backfill-and-republish.md`.**
  - **Backfill**, done by a person:
    1. Get the running digest with `find_deployed_version` or `kubectl`.
    2. Find the pipeline run that pushed it, and that run's commit.
    3. Make a fresh clone into a temp directory.
    4. Run `docker run snapshot-publisher … snapshot-publish --commit <sha> --image-digest <running digest> …` with admin-issued writer credentials.
    5. Delete the clone.
  - **False positive:**
    1. A reviewer confirms the finding.
    2. The project adds it to `.gitleaksignore` for future builds.
    3. A store admin re-runs the same command for the deployed digest with `--gitleaks-ignore <reviewed file>`, which records `clean-with-reviewed-allowlist`.
- [ ] **Step 2: Write `onboarding.md`.**
  - **New project:**
    1. Create the writer policy and user with `make-writer-policy.sh`.
    2. Add the variable group or CI variables.
    3. Add the template, as in the example files.
    4. Set `withSubmodulesOrLfs` and `multiArch`.
    5. Run the pipeline and check exit 0.
  - **New cluster:**
    1. Apply the `prod-cluster-rbac` overlay.
    2. Extract the token and CA into the `snapshot-clusters` Secret, and add the cluster to the config.
    3. Open the firewall to that cluster's API server.
    4. Add its CIDR to the tool's NetworkPolicy overlay.
- [ ] **Step 3: Verify.** Follow the backfill section against the dev MinIO (`tests/integration/compose.yaml`) and a sample repo, then check that `get_snapshot` returns it in a local `snapshot-mcp` run. Fix the runbook wherever a command failed as written.
- [ ] **Step 4: Commit** with the message `docs: backfill, republish and onboarding runbooks`.

---

### Task 15 (only if Task 0 found a network gap): One-way transfer

**Files:**
- Create: `src/snapshotkit/transfer.py`
- Test: `tests/test_transfer.py`

**Interfaces:**
- Consumes: the store (Task 5).
- Produces:
  - `export_bundle(src: SnapshotStore, out_dir: Path, state_file: Path) -> int` writes the keys not yet in the state file to `out_dir/<key>` and writes `out_dir/bundle.json`, which lists `{key: sha256}`.
  - `import_bundle(dst: SnapshotStore, in_dir: Path) -> int`.
  - `main(argv)`, with the subcommands `export` and `import`.

- [ ] **Step 1: Write failing tests.**
  - `test_roundtrip_between_stores`.
  - `test_second_export_only_new_keys`.
  - `test_tampered_file_aborts_before_any_write`: one byte is changed, the import raises, and `dst` is empty.
  - `test_import_order_snapshots_before_index`.
  - `test_import_existing_snapshot_not_overwritten`: `put_new` is used for `snapshots/` and `index/digest/`, and `put` for `index/tag/`.
- [ ] **Step 2: Run `pytest tests/test_transfer.py -v`.** Expected: FAIL.
- [ ] **Step 3: Implement.** Verify every sha256 before the first write.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat: one-way snapshot transfer bundle`.

---

### Task 16 (manual, by the team): Pilot — Rollout steps 1–4

- [ ] Stand up the store: reuse an internal S3-compatible service or artifact repo that meets the policies in Task 13, or deploy MinIO. Then run `pytest -m integration tests/integration/test_s3_store.py` against it, to confirm `If-None-Match` works.
- [ ] Push both images to the internal registry.
- [ ] Wire one Azure DevOps project and one GitLab project. Run each pipeline. The publish job logs `PUBLISHED`, and the objects appear in the store.
- [ ] Commit a fake secret on a branch build. The job shows a warning (exit 10), the build still succeeds, nothing is stored, and the webhook fires if it is configured.
- [ ] Apply the RBAC to the pilot clusters and deploy `snapshot-mcp`. Connect the existing agent to `http://snapshot-mcp.code-snapshots:8080/mcp`.
- [ ] Backfill the pilot services (Task 14 runbook). As an L1 user, ask the agent about a pilot service and confirm that it names the deployed digest and quotes the matching code.
- [ ] From the tool pod, `git ls-remote` against Azure DevOps or GitLab, and `curl` to their hosts, both fail.
