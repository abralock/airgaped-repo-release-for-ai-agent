# Runbook: backfill a snapshot, and republish after a false positive

Both procedures run `snapshot-publish` by hand for an image digest that is already
running in production but has no snapshot in the store. Both need **writer
credentials issued by a store admin** (a user with the policy printed by
`deploy/store/make-writer-policy.sh <project>`; see [onboarding](onboarding.md)).
The agent, the MCP tool and the retention job never run these steps and never hold
Azure DevOps or GitLab credentials.

Exit codes of `snapshot-publish`: `0` published or already published, `10` secrets
found (nothing published), `11` export refused (repo uses submodules or LFS; use
`--mode checkout`), `1` any other error (nothing published).

Store environment variables (read by `snapshot-publish`, `snapshot-alias`,
`snapshot-mcp`, `snapshot-retention`):

| Variable | Meaning |
| --- | --- |
| `SNAPSHOT_STORE_ENDPOINT` | S3 endpoint URL (required) |
| `SNAPSHOT_STORE_BUCKET` | bucket, default `code-snapshots` |
| `SNAPSHOT_STORE_ACCESS_KEY` / `SNAPSHOT_STORE_SECRET_KEY` | credentials (required) |
| `SNAPSHOT_NOTIFY_WEBHOOK` | optional; failures are POSTed here |

## 1. Backfill a snapshot for a running digest

Use this when production runs a digest that has no snapshot (for example it was
deployed before the project was onboarded, or the pipeline's publish step failed).

1. **Get the running digest.** Ask the agent (`find_deployed_version` with the
   namespace and workload), or on a production cluster:

   ```sh
   kubectl -n shop get pods -l app=my-service \
     -o jsonpath='{range .items[*].status.containerStatuses[*]}{.name}{"\t"}{.imageID}{"\n"}{end}'
   ```

   Take the `sha256:<64 hex>` part of `imageID` (it may be prefixed with
   `docker-pullable://` and a repository). Call it `DIGEST`. If `find_deployed_version`
   shows more than one digest, a rollout is in progress: backfill each digest you need.

2. **Find the pipeline run that pushed that digest, and its commit.** In Azure DevOps
   or GitLab, open the build/pipeline that pushed `image@DIGEST` (the registry UI or
   `crane ls`/`crane digest registry.internal/my-service:<tag>` tells you the tag; the
   pipeline run for that tag has the commit). Record the commit SHA (`SHA`), the
   pipeline run URL (`RUN_URL`), the repo path (`REPO_PATH`, e.g. `org/repo` or the GitLab
   project path) and the tag the digest was pushed with (`TAG`).
   For a multi-arch image, `DIGEST` is the index digest; the per-platform digests are
   added automatically from `--platform-manifest` (see below), so a node reporting a
   platform digest still resolves.

3. **Make a fresh clone into a temporary directory.** A full-history clone is fine;
   it is deleted afterwards and never leaves this machine.

   ```sh
   CLONE="$(mktemp -d)"
   git clone <repo-url> "$CLONE/repo"
   git -C "$CLONE/repo" cat-file -e "$SHA^{commit}"   # fails if the commit is unknown
   ```

   If the repo uses submodules or LFS, also run
   `git -C "$CLONE/repo" checkout "$SHA" && git -C "$CLONE/repo" submodule update --init --recursive && git -C "$CLONE/repo" lfs pull`
   and use `--mode checkout` below (the tree at `$SHA` is what gets published).

4. **Run the publisher** with the writer credentials. Container form (the supported
   way, since the scanner and its config ship in the image; `HOME=/tmp` and
   `safe.directory` are needed because the container user differs from the clone's owner):

   ```sh
   export SNAPSHOT_STORE_ENDPOINT=https://store.internal:9000
   export SNAPSHOT_STORE_ACCESS_KEY=<writer access key>
   export SNAPSHOT_STORE_SECRET_KEY=<writer secret key>

   docker run --rm \
     -v "$CLONE/repo:/repo:ro" \
     -e SNAPSHOT_STORE_ENDPOINT -e SNAPSHOT_STORE_ACCESS_KEY -e SNAPSHOT_STORE_SECRET_KEY \
     -e HOME=/tmp \
     registry.internal/snapshotkit/publisher:1 \
     sh -c 'git config --global --add safe.directory /repo && snapshot-publish \
       --repo-dir /repo \
       --commit "'"$SHA"'" \
       --mode archive \
       --project my-service \
       --source-platform azure-devops \
       --repo-path "'"$REPO_PATH"'" \
       --image-repository registry.internal/my-service \
       --image-tag "'"$TAG"'" \
       --image-digest "'"$DIGEST"'" \
       --pipeline-run-url "'"$RUN_URL"'" \
       --gitleaks-config /etc/snapshotkit/gitleaks.toml'
   ```

   - `--source-platform` is `azure-devops` or `gitlab`; `--project` must be the project
     the writer user is scoped to (`^[a-z0-9][a-z0-9._-]{0,99}$`).
   - Multi-arch image: first save the index manifest
     (`crane auth login <registry> ...; crane manifest registry.internal/my-service@$DIGEST > platform-manifest.json`,
     both available in the publisher image) and add `--platform-manifest platform-manifest.json`,
     or pass each platform digest with `--alias-digest sha256:...`. Mount the file into the container.
   - **The tag entry is overwritten.** `snapshot-publish` always points
     `image:tag` at this snapshot. Pass the tag the running digest actually carries;
     if that tag has since moved to a newer build, the tag entry will point at the
     backfilled (older) snapshot until the next pipeline run rewrites it. The agent
     resolves by digest, so this affects only tag-based `get_snapshot` refs.
   - Expected: stderr ends with `snapshot-publish: published project=my-service digest=sha256:...`
     and exit `0`. `already-published` (also exit `0`) means a snapshot for that digest
     already exists; snapshots are write-once per digest and are not replaced.

5. **Delete the clone.**

   ```sh
   rm -rf "$CLONE"
   ```

6. **Confirm.** Ask the agent `get_snapshot` for the digest (or `image:tag`); it
   should return a `snapshot_id` and a manifest with `scan_result: clean`.

### Running from the venv instead of the container

If you cannot use the image, `pip install` the package from the internal index and put
`gitleaks` (v8.21.x) on `PATH`, then run the same `snapshot-publish` command with
`--repo-dir "$CLONE/repo"` and `--gitleaks-config config/gitleaks.toml` from this repository.
The result is identical; the container form is preferred because it is the same binary
and config the pipelines use.

## 2. Republish after a false positive

A pipeline step that exits `10` leaves no snapshot and, if `SNAPSHOT_NOTIFY_WEBHOOK`
is set, notifies the owner. If the finding is a real secret, rotate it and fix the
code; that is not this runbook. If it is a false positive:

1. **A reviewer confirms the finding.** `snapshot-publish` only logs the finding count, so
   list the findings by scanning the same export the publisher scans. The publisher runs
   gitleaks from the snapshot root, so fingerprints are relative and independent of any
   temp path; scan the same way:

   ```sh
   docker run --rm -v "$CLONE/repo:/repo:ro" -e HOME=/tmp \
     registry.internal/snapshotkit/publisher:1 \
     sh -c 'git config --global --add safe.directory /repo && mkdir -p /tmp/wd/tree \
       && git -C /repo archive "'"$SHA"'" | tar -x -C /tmp/wd/tree \
       && cd /tmp/wd/tree && gitleaks dir . --config /etc/snapshotkit/gitleaks.toml \
            --redact --no-banner --no-color --verbose'
   ```

   Each finding prints `RuleID`, `File`, and `Fingerprint`
   (for example `cfg.py:aws-access-token:1`). A second person (not the author)
   confirms it is not a credential. (Files matched by the denylist, such as `.env`, are
   never published, so findings in them do not matter.) A real secret is rotated and
   removed instead; that is not this runbook.
2. **The project commits the fingerprint to `.gitleaksignore`** at its repo root, one
   fingerprint per line (for example `cfg.py:aws-access-token:1`), and the reviewer approves
   that change. `snapshot-publish` reads the `.gitleaksignore` committed at the exported
   commit (archive mode) or tracked in the checkout (checkout mode) and passes it to
   gitleaks, so future builds publish without further action. An untracked file is never
   used. The file itself is not part of the snapshot. The manifest records
   `scan_result: clean`. The commit that adds the ignore entry must be the one built, so
   this helps future builds, not the already-failed one.

   **Security note:** a fingerprint is `file:rule:line` and contains no hash of the secret,
   so an entry silences that rule on that line permanently. A different secret later placed
   on the same line is suppressed too. Remove the entry when the line changes, and have
   reviewers treat every `.gitleaksignore` change as security-relevant. Only a regular
   file committed at the repo root is honoured (never a symlink or directory, and never
   the working-tree copy).
3. **A store admin republishes the already-deployed digest** from that reviewed list. Write
   the approved fingerprints, one per line, to `reviewed.gitleaksignore` and run the backfill
   command with the ignore file mounted and `--gitleaks-ignore` (fingerprints are the same
   relative form; no `--workdir` is needed). If the repo also commits a `.gitleaksignore`,
   both are used (project first, then this one):

   ```sh
   docker run --rm \
     -v "$CLONE/repo:/repo:ro" \
     -v "$PWD/reviewed.gitleaksignore:/reviewed.gitleaksignore:ro" \
     -e SNAPSHOT_STORE_ENDPOINT -e SNAPSHOT_STORE_ACCESS_KEY -e SNAPSHOT_STORE_SECRET_KEY \
     -e HOME=/tmp \
     registry.internal/snapshotkit/publisher:1 \
     sh -c 'git config --global --add safe.directory /repo && snapshot-publish \
       ... same flags as the backfill ... \
       --gitleaks-config /etc/snapshotkit/gitleaks.toml \
       --gitleaks-ignore /reviewed.gitleaksignore'
   ```

   The manifest then records `scan_result: clean-with-reviewed-allowlist`. The scan still
   runs on the exact bytes being uploaded; only the reviewed fingerprints are skipped.
   Since the failed run published nothing, the digest key is free and the write succeeds.
4. **Delete the clone** and confirm with `get_snapshot` as above.

Troubleshooting:

- Exit `10` again: new findings remain, or the fingerprints do not match (they must be
  relative to the repo root, exactly as printed by the list command, e.g. `cfg.py:aws-access-token:1`).
- Exit `11`: add `--mode checkout` (submodules or LFS).
- Exit `1`: read stderr. Typical causes: store unreachable or wrong credentials,
  unknown commit in `--commit`, invalid `--project` or digest.
- `already-published`: nothing to do. A snapshot cannot be overwritten; to replace one,
  a store admin must delete the three objects for that digest (`snapshots/<project>/<digest>.tar.gz`,
  `.json` and the `index/digest/...` entries) and then republish.
