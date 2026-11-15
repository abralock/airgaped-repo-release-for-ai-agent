# History-free code snapshots for the production-support AI agent

**Date:** 2026-11-14
**Status:** Provisional. The team made this call to get moving: Option A below. It may be changed or updated later, once we know more about the past incident and how each project versions its releases.

## Intent

L1 operators will ask an AI agent about production issues ("why is the order service returning 500 on checkout?"). The agent answers by reading the source code of the affected service.

The agent runs inside the air-gapped corporate Kubernetes cluster. The team does **not** want it reading Azure DevOps or GitLab directly. A full clone gives it the whole git history: old commits, deleted files, authors, and anything that was once committed and later removed.

There was a serious incident in the past that drives this caution. Its details are not known to the author of this design. Until they are, the design defends against the most likely serious cause: sensitive data (secrets, credentials, internal data) that survives in git history. It also covers sensitive data still present in the current code, and authorship metadata.

## Goals

1. The agent can read the source code of the **exact version running in production** for any covered service.
2. The agent never has git history, a `.git` directory, commit messages, author information, or credentials to Azure DevOps or GitLab.
3. No snapshot reaches the agent unless it has passed a secret scan.
4. Works the same for projects on Azure DevOps and on GitLab.
5. Works whatever tagging or versioning convention each project uses.

## Non-goals

- Code on in-progress branches or unreleased work. The agent explains production only.
- The agent changing code, opening PRs or writing anywhere.
- A human-facing download/release page. It can be added later on the same store if wanted.
- Cleaning up existing git history in Azure DevOps or GitLab. That is a separate effort if the incident calls for it.

## Anti-goals (failure even if it "works")

- The agent explains a different version from the one in production. Wrong answers to L1 in the middle of an incident are worse than no answer.
- Any path by which the agent, or the service it uses, gets source control credentials or a real clone.
- A snapshot that contains a secret gets published.
- One central component ends up holding read access to every repo. That would be a high-value target, which is what the team is trying to avoid.

## Scope

The agent already exists: the model endpoint, the agent itself and the L1 interface are in place. This project delivers only:
1. the pipeline templates,
2. the snapshot store,
3. the agent-side lookup tool (plus its RBAC and network access),
4. the one-off backfill.

## Constraints

- Air-gapped: no internet, so every tool (scanner image, storage) must be available inside the network.
- Source control: Azure DevOps and GitLab, both in use.
- Agent runtime: corporate Kubernetes cluster.
- Production runs on **several Kubernetes clusters**. The agent's network can reach them, though firewall rules may need opening per cluster.
- Network path from the CI build agents/runners to production: **not yet known** (see section 2 and Rollout step 0).
- Project teams and their pipelines are **trusted** to run the scan honestly. Enforcement outside the teams' control is not required.
- Users: L1 operators, who may not normally have source code access (see Open items).

## Approaches considered

**A. Each build pipeline publishes its own snapshot. CHOSEN.**
A shared pipeline step (one Azure DevOps template, one GitLab CI template) runs when the container image is built. It exports the code at that exact commit without history, scans it, and uploads it to a snapshot store keyed by the image. The agent looks up which image is deployed and fetches the matching snapshot.
- Gains: exact match with production regardless of tagging convention, and no central holder of repo credentials.
- Costs: each project must adopt the template, and builds from before adoption have no snapshot (handled by a one-off backfill).

**B. A central exporter.** One job with read access to both platforms watches for release tags and exports, scans and publishes them.
- Gains: no project pipeline changes, and it can backfill.
- Costs: a per-project map from deployed version to git tag is needed, and the team doesn't yet know how each project tags. It also means one service holds access to every repo.
- Rejected as the main path. Its export logic is reused once, by hand, for the backfill (see below).

**C. A release webapp where people publish snapshots manually** (the original idea).
- Gains: simplest, and a person approves each release.
- Costs: people forget or lag, production drifts from what the agent sees, and L1 gets wrong answers.
- Rejected.

**Why A:** the goal is "explain what is running in production". Only A ties a snapshot to the deployed artifact itself, so it doesn't depend on per-project tagging conventions the team hasn't confirmed.

## Design

### 1. Pipeline step: export, scan, publish

Runs in each project's build pipeline, **after** the image is pushed (so its digest is known). It ships as:
- an Azure DevOps YAML template (in a shared templates repo, consumed via `resources.repositories` + `template:`), and
- a GitLab CI include (`include: project: ... file: ...`).

Both templates call the **same script/container image** so behaviour is identical on both platforms.

Steps:

1. **Export without history.** `git archive --format=tar <commit>^{tree}`. Archiving the *tree* rather than the commit keeps git from embedding the commit ID in the tar header. Then gzip it.
   - Disable `export-subst`: archive with `--worktree-attributes` off and fail if any `.gitattributes` in the tree uses `export-subst`. `$Format:%an$`-style substitution would otherwise inject author and log data into files.
   - Honour `export-ignore` so projects can exclude paths.
   - Also apply a global denylist (my call, initial list): `.env*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `*.kdbx`, `secrets.*`, `**/.git*` except `.gitignore`/`.gitattributes`.
   - **Submodules / Git LFS** (my call): `git archive` drops submodule content and keeps only LFS pointer files. For repos that use either, the step instead builds the tar from a clean checkout (`git ls-files --recurse-submodules` after `git lfs pull`) and applies the same denylist and `export-ignore` rules. No `.git` directories go into the output.
   - **Images built from several repos** are out of scope for v1 (my call). The template covers one repo, one commit, one image.
2. **Scan.** Run gitleaks in no-git/directory mode (`gitleaks dir`) on the extracted tree, using an org-wide config. The gitleaks image is mirrored into the internal registry.
3. **On scan failure:** do **not** publish the snapshot. Post a warning to the pipeline run and notify the project's owners. **The build/deploy itself is not failed** (my call: this feature must not block releases). The agent will report "no snapshot available for this version" instead of guessing.
   - **False positives** (my call): the project adds the finding to its `.gitleaksignore` for future builds. For the version already deployed, a store admin re-runs the publish container for that digest with the reviewed allowlist. This is the same manual path as the backfill (section 4).
4. **Publish** to the snapshot store:
   - `snapshots/<project>/<image-digest>.tar.gz`
   - `snapshots/<project>/<image-digest>.json`, the manifest (below)
   - Index entries so a digest or `image:tag` can be resolved to the project and snapshot.

The pipeline identity has **write-only** access to the store (no read and no delete), scoped per project where the store supports it. Where the store supports it, existing objects cannot be overwritten (write-once per digest; my call). This is a cheap guard against accidental clobbering even though teams are trusted.

**Digest must match what production reports** (my call; verify in the pilot). If images are re-pushed, copied across a network gap, or promoted into a separate production registry, the digest the runtime reports in `imageID` may differ from the build digest. In that case the publish step also writes an alias index entry for the production digest at promotion time. Multi-arch images: index both the manifest-list digest and each per-platform digest.

**Manifest fields:** `project`, `source_platform` (azure-devops | gitlab), `repo_path`, `image_repository`, `image_tag`, `image_digest`, `pipeline_run_url`, `published_at`, `scanner` + `scanner_version`, `scan_result`.
The commit SHA is **deliberately left out** of anything the agent can read. It is kept only in the pipeline run's own logs, for audits.

### 2. Snapshot store

My call: an **S3-compatible object store**. If the organisation already runs an internal artifact repository (Nexus/Artifactory "generic" repo) that supports per-path write-only and read-only credentials, use that instead. The builder should check what exists before standing up something new.

**Location** (my call; depends on Rollout step 0):
- If the CI build agents/runners and the agent's cluster can both reach one network location, use a single store there.
- If they can't (there's a gap between the CI zone and production), run a store on each side with a **one-way sync** from the CI-side store to the production-side store, using whatever approved transfer mechanism crosses that gap. Only the production-side store is readable by the agent. The CI side is write-only for pipelines.

Access:
- Pipelines: write-only (per project, if supported).
- Agent: read-only, whole bucket.
- Humans: admin only, for audits and deletions.

Retention (my call): keep every snapshot whose digest is still deployed anywhere, plus the most recent 20 per project. Delete the rest after 90 days.

### 3. Agent-side tool: resolve and read

A small tool the agent calls. It is packaged in whatever form the agent framework uses (MCP server, function tool, etc. — the builder's choice to match the agent). Capabilities:

1. **`find_deployed_version(cluster, namespace/workload)`**: reads the workload from Kubernetes and returns each container's running image and digest, taken from `pods[].status.containerStatuses[].imageID`. This is the digest actually running, not the possibly mutable tag in the spec. If `cluster` is omitted, the tool searches all configured clusters and returns every match.
   - **Multiple clusters:** the tool is configured with a list of production clusters (name, API server URL, CA, token). Each cluster gets a dedicated read-only ServiceAccount whose token is mounted into the agent's pod as a Kubernetes Secret. That Secret is in the *agent's* cluster; the agent itself still has no RBAC to read secrets anywhere.
   - In each cluster, that ServiceAccount gets a ClusterRole/Role with `get`/`list` on `pods`, `deployments`, `statefulsets`, `daemonsets`, `replicasets` in the production namespaces only. It gets **no** access to `secrets` or `configmaps`, and nothing beyond `get`/`list`.
   - Firewall rules from the agent's cluster to each production API server are opened as part of onboarding each cluster.
2. **`get_snapshot(digest)`**: downloads and extracts the snapshot to a local, per-request, ephemeral directory (an `emptyDir` volume) and returns its path. It caches by digest.
3. Reading and searching files inside that directory uses the agent's normal read-only file tools, confined to the snapshot directory.

If several pods of one workload run different digests (mid-rollout), the tool returns all of them and the agent says so in its answer.

The agent's pod has **no** source control credentials or network policy route to Azure DevOps or GitLab. Network policy allows only the production clusters' API servers, the snapshot store and the existing model endpoint.

Retention's "still deployed anywhere" check uses the same cluster list and read-only credentials.

### 4. One-off backfill for what's running today

Services currently in production were built before the template existed, so they have no snapshots. For each one, a person (not an automated service with standing access):
1. Identifies the commit that built the currently deployed image (from the pipeline run of that image).
2. Runs the same export, scan and publish container locally against a fresh clone, keyed by the deployed digest.
3. Discards the clone.

This uses Approach B's logic once, under human control, without creating a standing central exporter.

### 5. Rollout

0. **Check the network path first.** Can the Azure DevOps build agents and the GitLab runners reach a location the agent's cluster can also reach? The answer picks between the single store and the two stores with one-way sync (section 2). Also check, on one pilot image, that the build digest equals the `imageID` production reports (section 1).
1. Store(s) plus the shared export/scan/publish container.
2. Azure DevOps and GitLab templates, piloted on one project each.
3. Agent tool plus RBAC and network policy, starting with the cluster(s) the pilot services run on.
4. Backfill the pilot services, then onboard the remaining projects and clusters.

## Open items (not blocking the build)

- **The incident.** Ask whoever was involved for a one-line summary. If the issue was where code *went* rather than what history contained, the threat is the agent's output channel (what it can send or quote), not history. That would change this design.
- **L1 seeing code.** The agent may quote source in answers, and L1 may not normally have source access. Confirm with security whether quoting is allowed, or whether the agent should explain without quoting.
- **Per-project versioning.** No longer needed by this design, but worth recording while the team checks each project.

## Left to the builder

- How the tool plugs into the existing agent (MCP server, function tool, etc.).
- Exact storage product (subject to the access rules above).
- Script language for the export/scan/publish container.
- gitleaks rule tuning and allowlists.
- How "service" names from L1 map to Kubernetes workloads, beyond namespace/workload lookup.

## Calls made by the author (please check)

- Scan failure skips the snapshot but does not fail the build.
- Snapshots keyed by image **digest**, resolved from running pods, not tags.
- Commit SHA kept out of everything the agent can read.
- Initial denylist of file patterns.
- S3-compatible store, unless an internal artifact repository already fits.
- Retention: still deployed, plus last 20, plus 90 days.
- Backfill done by a person, not by a standing service.
- Store location: one shared store if the network allows it, otherwise two stores with one-way sync. This is decided by the network check in Rollout step 0.
- Write-once snapshots per digest, where the store supports it.
- An alias entry for the production digest if it differs from the build digest.
- Submodule/LFS repos exported from a clean checkout. Images built from several repos are out of scope for v1.
- False positives re-published by a store admin after review.
- Multiple clusters: one read-only ServiceAccount per cluster, with its token held by the agent.
