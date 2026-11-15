# snapshotkit

History-free, secret-scanned source snapshots for an AI agent in an air-gapped
Kubernetes environment. A CI step (Azure DevOps or GitLab) exports the exact tree that
produced a container image, with no `.git`, commit SHA, author data or timestamps, scans
those exact bytes with gitleaks, and only then uploads it to an S3-compatible store keyed
by the image digest. An MCP tool lets the existing agent look up which digest runs in
production and browse that snapshot, without the agent ever holding source-control
credentials or network access to them.

## Components

| Component | Where | What it does |
| --- | --- | --- |
| Publisher CLI and image | `src/snapshotkit/publish.py`, `alias.py`, `docker/Dockerfile` (`publisher` target) | `snapshot-publish` exports, scans and uploads; `snapshot-alias` adds a digest alias. Exit codes: 0 ok, 10 secrets, 11 export refused, 1 error |
| Pipeline templates | `templates/azure-devops/`, `templates/gitlab/`, `templates/examples/` | Non-blocking publish job/step for each CI system |
| Store | `deploy/store/` | Bucket layout, writer / reader / retention-admin policies (`make-writer-policy.sh`) |
| MCP tool | `src/snapshotkit/mcp_server.py`, `docker/Dockerfile` (`tool` target) | `find_deployed_version`, `get_snapshot`, `list_files`, `read_file`, `search_code` over streamable HTTP on port 8080 |
| Retention | `src/snapshotkit/retention.py` | Daily job: keep if deployed, among the 20 newest, or under 90 days old |
| Manifests | `deploy/agent-cluster/`, `deploy/prod-cluster-rbac/` | Kustomize for the agent cluster (tool, retention, NetworkPolicies) and read-only RBAC on production clusters |

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

## Building images

```sh
scripts/build-images.sh --target publisher     # -> snapshot-publisher:dev
scripts/build-images.sh --target tool          # -> snapshot-tool:dev
scripts/build-images.sh --target publisher --platform linux/amd64,linux/arm64 --tag registry.internal/snapshotkit/publisher:1 --push
```

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
