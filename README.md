# snapshotkit

History-free, secret-scanned source snapshots for an AI agent in an air-gapped
Kubernetes environment. A CI step (Azure DevOps or GitLab) exports the exact tree that
produced a container image, with no `.git`, commit SHA, author data or timestamps, scans
those exact bytes with gitleaks, and only then uploads it to an S3-compatible store keyed
by the image digest. An MCP tool lets the existing agent look up which digest runs in
production and browse that snapshot, without the agent ever holding source-control
credentials or network access to them.

## Architecture

[Architecture diagram](docs/architecture/snapshotkit-architecture.html) (download and open in a browser):
the CI publish path, the snapshot store, and how the agent reads snapshots through `snapshot-mcp`.
Its source is `docs/architecture/snapshotkit.architecture.json`; see `CLAUDE.md` for how it is kept up to date.

## Components

| Component | Where | What it does |
| --- | --- | --- |
| Publisher CLI and image | `src/snapshotkit/publish.py`, `alias.py`, `docker/Dockerfile` (`publisher` target) | `snapshot-publish` exports, scans and uploads; `snapshot-alias` adds a digest alias. Exit codes: 0 ok, 10 secrets, 11 export refused, 1 error |
| Pipeline templates | `templates/azure-devops/`, `templates/gitlab/`, `templates/examples/` | Non-blocking publish job/step for each CI system |
| Store | `deploy/store/` | Bucket layout, writer / reader / retention-admin policies (`make-writer-policy.sh`) |
| MCP tool | `src/snapshotkit/mcp_server.py`, `docker/Dockerfile` (`tool` target) | `find_deployed_version`, `get_snapshot`, `list_files`, `read_file`, `search_code` over streamable HTTP on port 8080 |
| Retention | `src/snapshotkit/retention.py` | Daily job: keep if deployed, among the 20 newest, or under 90 days old |
| Manifests | `deploy/agent-cluster/`, `deploy/prod-cluster-rbac/` | Kustomize for the agent cluster (tool, retention, NetworkPolicies) and read-only RBAC on production clusters |

## Image tags and digests

Every pushed image has both a tag and a digest:

```
registry.internal/payment-api:1.4.2@sha256:ab12…
                              └─tag─┘ └─digest──┘
```

| | Tag (`1.4.2`) | Digest (`sha256:ab12…`) |
|---|---|---|
| What it is | A version label | A fingerprint of the exact image contents |
| Who chooses it | Your pipeline or team | Computed by the registry on push |
| Can it change? | Yes, it can be moved to another image | Never: always this exact image |
| Reported by Kubernetes | Whatever the deployment asked for | The exact image running in each pod (`imageID`) |

On push the registry stores the image under its digest, and the tag is a name pointing
at that digest (`1.4.2 → sha256:ab12… → image`). If someone re-pushes `1.4.2` with
different code, the tag moves but the old digest still means the old image.

snapshotkit uses both:

- **CI:** after the image push, the pipeline passes the digest (`--image-digest`) and the
  tag (`--image-tag`) to `snapshot-publish`. The snapshot is filed under the digest; the
  tag goes into an index and the snapshot manifest, so you can still find it by version.
- **Cluster:** `find_deployed_version` reads the digest from each pod's `imageID` and
  `get_snapshot` fetches the snapshot for that digest, so the agent always reads the code
  that is really running. The returned manifest shows both the tag and the digest.
- **Multi-arch images:** a push creates one index digest plus one digest per platform
  (amd64, arm64), and a pod may report either depending on the container runtime. The
  publisher records the per-platform digests as aliases (`--alias-digest`,
  `--platform-manifest`), so any of them resolves to the same snapshot.

Tag images with plain versions such as `1.4.2`, not commit SHAs: the agent can see tags,
and a SHA would point it back to git history. `snapshot-publish` warns if the tag
contains the commit SHA.

The digest is not hidden, just long. To see it:

| Where | How |
|---|---|
| When you push | The last line of `docker push` prints `1.4.2: digest: sha256:…` |
| From the registry | `docker buildx imagetools inspect <image>:<tag>` or `crane digest <image>:<tag>` |
| Registry web page | On ghcr.io, each version on the package page lists its `sha256:…` |
| Local image | `docker inspect --format '{{index .RepoDigests 0}}' <image>:<tag>` |
| In Kubernetes | `kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[*].imageID}'` |

### Capturing the digest in your build

The build has to hand the snapshot job the tag and the digest as two separate values. The
digest exists only after the push, so read it from the push itself: `--metadata-file`
records the digest the registry returned (the index digest for a multi-arch build). Do not
use the image ID from `docker images`; that is a different hash and does not match what pods
report.

GitLab, from `templates/examples/gitlab-ci.example.yml` (`IMAGE_REPOSITORY` is set in its top-level `variables`):

```yaml
build:
  stage: build
  image: docker:27
  services:
    - docker:27-dind
  rules:
    - if: $CI_COMMIT_TAG          # release tags only: the image tag is a plain version
  variables:
    IMAGE_TAG: $CI_COMMIT_TAG
  script:
    - echo "$REGISTRY_PASSWORD" | docker login "${IMAGE_REPOSITORY%%/*}" -u "$REGISTRY_USER" --password-stdin
    - docker buildx build --push -t "$IMAGE_REPOSITORY:$IMAGE_TAG" --metadata-file meta.json .
    - |
      IMAGE_DIGEST=$(grep -o '"containerimage.digest": *"sha256:[0-9a-f]*"' meta.json | grep -o 'sha256:[0-9a-f]*')
      test -n "$IMAGE_DIGEST"
      echo "IMAGE_REPOSITORY=$IMAGE_REPOSITORY" > build.env
      echo "IMAGE_TAG=$IMAGE_TAG" >> build.env
      echo "IMAGE_DIGEST=$IMAGE_DIGEST" >> build.env
  artifacts:
    reports:
      dotenv: build.env           # the snapshot job reads these through `needs`
```

Azure DevOps (`templates/examples/azure-pipelines.example.yml`) runs the same push and
`grep`, then publishes the digest as an output variable that the snapshot job template reads:

```sh
echo "##vso[task.setvariable variable=imageDigest;isOutput=true]$digest"
# snapshot job: imageDigest: $[ dependencies.Build.outputs['push.imageDigest'] ]
```

For a multi-arch build (`--platform linux/amd64,linux/arm64`), also set
`SNAPSHOT_MULTI_ARCH: "true"` (GitLab) or `multiArch: true` (Azure DevOps) so the
per-platform digests are recorded as aliases. Both examples use Docker-in-Docker style
`buildx`; kaniko and buildah can also write the pushed digest to a file.

## Development quickstart

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q -m "not integration"          # unit tests

# integration tests against a local MinIO
docker compose -f tests/integration/compose.yaml up -d
.venv/bin/pytest -q -m integration
docker compose -f tests/integration/compose.yaml down
```

Some tests need the `gitleaks` binary on `PATH`.

The scanner config `config/gitleaks.toml` is a vendored copy of gitleaks v8.21.2's default
rules with the global path allowlist removed, so every published file is scanned; files
gitleaks cannot inspect (PDF, office documents, archives) are kept out of snapshots by the
denylist in `src/snapshotkit/denylist.py` (full list in
[onboarding, section A step 4](docs/runbooks/onboarding.md)).

## Building images

```sh
scripts/build-images.sh --target publisher     # -> snapshot-publisher:dev
scripts/build-images.sh --target tool          # -> snapshot-tool:dev
scripts/build-images.sh --target publisher --platform linux/amd64,linux/arm64 --tag registry.internal/snapshotkit/publisher:1 --push
```

The agent-cluster overlay's image `snapshotkit` is the **`tool`** target
(`scripts/build-images.sh --target tool`); the MCP Deployment and the retention CronJob
share it. The `publisher` image is only for pipelines and manual backfills.

### Why two images

Published as `ghcr.io/abralock/snapshotkit-publisher` (`publisher` target) and
`ghcr.io/abralock/snapshotkit` (`tool` target). They run in different places and do
different jobs:

| | `snapshotkit-publisher` | `snapshotkit` |
|---|---|---|
| Runs where | CI pipelines (Azure DevOps and GitLab), as the job container | The agent's Kubernetes cluster: MCP Deployment and retention CronJob |
| Job | Exports the repo with no history, scans for secrets, uploads the snapshot | Serves snapshots to the agent over MCP; deletes old snapshots |
| Contains | git, git-lfs, gitleaks, crane | Only the Python app: no git, scanner or crane |
| Starts with | a shell (CI runs its own script steps) | `snapshot-mcp` on port 8080 |

They are kept apart on purpose:

1. **No git where the agent runs.** The agent must never reach git history. With no
   `git` binary in the tool image, even a bug or a prompt-injected agent cannot run
   `git log` there.
2. **Fewer components next to the agent.** Every extra binary in the tool image is more
   for security scans to flag and more to patch.
3. **CI and Kubernetes need different start-up behavior.** ADO and GitLab container jobs
   break if the image has a fixed entrypoint, so the publisher starts a shell; the tool
   image must start the server.

Both come from the same `docker/Dockerfile` and Python package, so their code stays in
sync.

The store policies in `deploy/store/` hard-code the bucket `code-snapshots`, so
`SNAPSHOT_STORE_BUCKET` must be `code-snapshots` (or edit the policies to match).

The script carries public-internet defaults for dev; in the air gap override them through
environment variables (see the header of `docker/Dockerfile` and
[onboarding, section C](docs/runbooks/onboarding.md)).

## Runbooks

- [Backfill a snapshot, and republish after a false positive](docs/runbooks/backfill-and-republish.md)
- [Onboarding a project, a production cluster, and the agent](docs/runbooks/onboarding.md)

Design: [docs/superpowers/specs/2026-11-14-history-free-code-snapshots-design.md](docs/superpowers/specs/2026-11-14-history-free-code-snapshots-design.md)

## Air-gapped test run

```bash
bash scripts/test-airgapped.sh
```

Builds a test image (`docker/test.Dockerfile`, network used only at build time), then:

1. runs the unit tests with `--network none` (gitleaks and kubectl are present, so the
   gitleaks and kustomize tests must run, not skip);
2. asserts a socket connect to `1.1.1.1:443` fails inside that sandbox;
3. runs the integration tests on a `docker network create --internal` network where the
   only reachable peer is a MinIO container;
4. builds the `publisher` and `tool` images and smoke-tests them with `--network none`.

It prints `AIRGAPPED TESTS PASSED` only if every step succeeds, and always removes the
MinIO container and network. Override `BASE_IMAGE`, `PIP_INDEX_URL`, `GITLEAKS_URL`,
`KUBECTL_URL`, their `*_SHA256_*` values and `MINIO_IMAGE` to point at internal mirrors.
