# Runbook: onboarding a project, a cluster, and the agent

## A. New project (pipeline publishes snapshots)

1. **Create the writer policy and user** (store admin). The script prints a policy that
   allows `s3:PutObject` only on `snapshots/<project>/*` and `index/*` (no read, no list,
   no delete). Project names must match `^[a-z0-9][a-z0-9._-]{0,99}$`.

   ```sh
   deploy/store/make-writer-policy.sh my-service > /tmp/my-service-writer.json
   ```

   Create the policy in your store from that JSON, create a user for the project, attach
   the policy, and issue an access key and secret key. (MinIO: `mc admin policy create`,
   `mc admin user add`, `mc admin policy attach`; other S3 stores have equivalents.)
   The bucket is `code-snapshots`. The tool-reader and retention-admin policies are in
   `deploy/store/policies/` and are used by section C, not by pipelines.

2. **Add the variable group (Azure DevOps) or CI/CD variables (GitLab).**
   - Azure DevOps: a variable group (default name `snapshot-store`, override with the
     `variableGroup` parameter) holding `SNAPSHOT_STORE_ENDPOINT`, `SNAPSHOT_STORE_BUCKET`,
     `SNAPSHOT_STORE_ACCESS_KEY`, `SNAPSHOT_STORE_SECRET_KEY`, and for `multiArch: true`
     `REGISTRY_USER` and `REGISTRY_PASSWORD`. Mark the store and registry credentials **secret**.
     **Do NOT mark `SNAPSHOT_NOTIFY_WEBHOOK` secret** (optional): the template resolves it
     with a runtime expression (`$[ coalesce(variables['SNAPSHOT_NOTIFY_WEBHOOK'], '') ]`),
     and secret variables do not resolve in `$[ ]` expressions, so a secret webhook is
     silently empty and no notifications are sent.
   - GitLab: masked CI/CD variables with the same names (and `REGISTRY_USER` /
     `REGISTRY_PASSWORD` for multi-arch). GitLab sends no mail for jobs allowed to fail,
     so set `SNAPSHOT_NOTIFY_WEBHOOK` if owners should hear about failures.

3. **Add the template to the pipeline**, as in `templates/examples/`.
   - Azure DevOps (`azure-pipelines.example.yml`): add the `templates` repository resource and
     `- template: azure-devops/snapshot-publish-job.yml@templates` with parameters
     `dependsOn`, `project`, `imageRepository`, `imageTag`, `imageDigest`
     (e.g. `$[ dependencies.Build.outputs['push.imageDigest'] ]`), `publisherImage`, and optionally
     `withSubmodulesOrLfs`, `multiArch`, `variableGroup`.
   - GitLab (`gitlab-ci.example.yml`): `include` the file
     `gitlab/snapshot-publish.gitlab-ci.yml` and add a job `extends: .snapshot-publish`
     with `needs` on the build job. The build job must export `IMAGE_REPOSITORY`,
     `IMAGE_TAG` and `IMAGE_DIGEST` (dotenv artifact). Set `SNAPSHOT_PUBLISHER_IMAGE`;
     optionally `SNAPSHOT_PROJECT` (default `$CI_PROJECT_NAME`).
   - The step is non-blocking: `continueOnError: true` (ADO) / `allow_failure: true` (GitLab).

4. **Set the repo-shape options.**
   - Repo uses submodules or Git LFS: ADO `withSubmodulesOrLfs: true`; GitLab
     `SNAPSHOT_MODE: checkout` **and** `GIT_SUBMODULE_STRATEGY: recursive`. Otherwise the
     step exits `11` and publishes nothing.
   - The image is a multi-arch index: ADO `multiArch: true`; GitLab `SNAPSHOT_MULTI_ARCH: "true"`.
     The step fetches the index manifest with `crane` so each platform digest resolves to the
     snapshot.

5. **Run the pipeline and check exit 0.** The publish step logs
   `snapshot-publish: published project=... digest=sha256:...`. Exit `10` = secrets found,
   `11` = export refused (step 4), anything else = read the log. Then ask the agent for
   `get_snapshot` on the new digest.

## B. New production cluster

1. **Apply the `prod-cluster-rbac` overlay on that cluster.** Copy
   `deploy/prod-cluster-rbac/overlays/example/`, edit `rolebindings.yaml` to one
   RoleBinding per production namespace, then:

   ```sh
   kubectl apply -k deploy/prod-cluster-rbac/overlays/<your-cluster>
   ```

   This creates namespace `snapshot-reader`, ServiceAccount `snapshot-reader`, a token
   Secret `snapshot-reader-token`, and a ClusterRole bound per namespace with `get` and `list` on
   pods, deployments, statefulsets, daemonsets and replicasets only (no secrets, no configmaps).

2. **Extract the token and CA into the `snapshot-clusters` Secret** (in namespace
   `code-snapshots` on the agent cluster) and add the cluster to the config.

   ```sh
   # on the production cluster
   kubectl -n snapshot-reader get secret snapshot-reader-token -o jsonpath='{.data.token}' | base64 -d > prod-c-token
   kubectl -n snapshot-reader get secret snapshot-reader-token -o jsonpath='{.data.ca\.crt}' | base64 -d > prod-c-ca.crt
   ```

   Add an entry to `clusters.yaml` (see `deploy/agent-cluster/clusters-config.example.yaml`):

   ```yaml
   - name: prod-c
     server: https://prod-c.k8s.internal:6443
     ca_file: /etc/snapshotkit/clusters/prod-c/ca.crt
     token_file: /etc/snapshotkit/clusters/prod-c/token
     namespaces: [shop, payments]
   ```

   Secret keys cannot contain `/`, so the Secret uses flat keys `prod-c-ca.crt` and
   `prod-c-token`, which the overlay maps to `prod-c/ca.crt` and `prod-c/token`
   (add two `secret.items` JSON patches each to the Deployment `snapshot-mcp` and the CronJob
   `snapshot-retention`, as the example overlay does for `prod-a`). Then on the agent cluster:

   ```sh
   kubectl -n code-snapshots create secret generic snapshot-clusters \
     --from-file=clusters.yaml \
     --from-file=prod-c-ca.crt --from-file=prod-c-token \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

   (Include the files of every existing cluster in that one command; `apply` replaces
   the whole Secret.) Delete the local token and CA files afterwards. Pods pick up
   changed Secret volumes, but changed `items:` mappings need `kubectl -n code-snapshots rollout restart deploy/snapshot-mcp`.

3. **Open the firewall** from the agent cluster's nodes to that cluster's API server
   (TCP 6443, or 443 on some distributions).

4. **Add its address to the tool's NetworkPolicy overlay.** In your overlay's `snapshot-tool`
   patch, add an `ipBlock` (`/32` for the API server VIP, or its CIDR) under the API-server
   egress rule, then apply the overlay.

5. **Dry-run retention before the next scheduled run.** Retention aborts with exit `2`
   and deletes nothing if any configured cluster is unreachable, so a mistake in steps 1-4
   would stop all cleanup:

   ```sh
   kubectl -n code-snapshots create job --from=cronjob/snapshot-retention ret-test \
     --dry-run=client -o json \
     | jq '.spec.template.spec.containers[0].command += ["--dry-run"]' \
     | kubectl apply -f -
   kubectl -n code-snapshots logs job/ret-test
   kubectl -n code-snapshots delete job ret-test
   ```

   Expect
   `dry run: kept N, would delete M` and no `cluster ... unreachable` line.
   Also ask the agent to `find_deployed_version` for a workload in the new cluster and
   check the `errors` list is empty.

### Rotating `snapshot-reader-token`

`snapshot-reader-token` is a long-lived ServiceAccount token: rotate it on your
credential schedule and whenever someone with access leaves.

1. On the production cluster: `kubectl -n snapshot-reader delete secret snapshot-reader-token`,
   then `kubectl apply -k deploy/prod-cluster-rbac/overlays/<your-cluster>` to recreate it
   (the controller issues a new token). Old token is invalid immediately.
2. Extract the new token as in B.2 and update `snapshot-clusters` (`prod-c-token` key).
3. No restart is needed: the tool re-reads the token file on every API call, and Secret volume
   updates reach the pods within about a minute. Until then calls to that cluster fail and are
   reported as a named error for it (and retention, which needs every cluster, aborts with
   exit `2` and deletes nothing).

### Network notes

- The DNS egress rule in the NetworkPolicies selects `k8s-app: kube-dns` in `kube-system`.
  Adjust it for NodeLocal DNSCache (allow the node-local address) or OpenShift
  (`openshift-dns` namespace, `dns.operator.openshift.io/daemonset-dns: default`).
- The API-server port in the base is `6443`; use `443` where your clusters expose it there.

## C. Agent cluster (tool, retention) and the agent itself

1. **Images.** Build and push with `scripts/build-images.sh` (see the README). In the air
   gap, override every default through environment variables:
   - `PIP_INDEX_URL` must be **credential-free** (no `user:pass@`): build args are recorded in
     image history. The internal index must also carry `setuptools` and `wheel` (pip build
     isolation downloads them).
   - Mirrors for gitleaks and crane must **keep the upstream archive file names**, because the
     `{arch}` URL templates (`GITLEAKS_URL`, `CRANE_URL`) are substituted with the upstream
     names (gitleaks `x64`/`arm64`, crane `x86_64`/`arm64`). Set the matching
     `*_SHA256_AMD64` / `*_SHA256_ARM64` values; the build fails if one is empty.
   - `BASE_IMAGE` points at the internal registry's Python image.
2. **Store users.** Create a read-only user with `deploy/store/policies/tool-reader.json`
   and an admin user with `retention-admin.json`. Create Secrets in `code-snapshots` with
   the keys `SNAPSHOT_STORE_ENDPOINT`, `SNAPSHOT_STORE_BUCKET`, `SNAPSHOT_STORE_ACCESS_KEY`,
   `SNAPSHOT_STORE_SECRET_KEY`: `snapshot-store-reader` (used by `snapshot-mcp`) and
   `snapshot-store-admin` (used by `snapshot-retention`).
3. **Deploy.** Copy `deploy/agent-cluster/overlays/example`, set the image name and tag, the
   cluster Secret items, and the NetworkPolicy addresses (object store, API servers, the
   agent's namespace and pod labels, model endpoint), then `kubectl apply -k` it. The tool
   serves streamable HTTP on port 8080 at `http://snapshot-mcp.code-snapshots.svc:8080/mcp`.
   The `agent-egress` policy goes in the agent's namespace; everything not listed there
   (including Azure DevOps and GitLab) is unreachable from the agent.
4. **Register the tool with the agent** as an MCP server at that URL. Tools:
   `find_deployed_version(namespace, workload, cluster?)`, `get_snapshot(ref)`,
   `list_files(snapshot_id, subdir?)`, `read_file(snapshot_id, path)`,
   `search_code(snapshot_id, pattern, glob?)`.
5. **System prompt.** Tell the agent, in its system prompt:

   > To answer questions about the code that is running in production, first call
   > `find_deployed_version` for the namespace and workload, then call `get_snapshot` with
   > the digest it returns, then browse with `list_files`, `read_file` and `search_code`.
   > If more than one digest is returned, a rollout is in progress: say so. If a cluster
   > is listed under errors, say its version could not be checked. If there is no snapshot,
   > reply exactly "No snapshot available for this version." Never guess, and never answer
   > from the code of a different version.

6. **Retention** runs daily at 03:00 (`snapshot-retention` CronJob). It keeps a snapshot if it
   is deployed anywhere, among the 20 newest for its project, or younger than 90 days. Exit
   `2` means a cluster was unreachable and nothing was deleted. Use
   `snapshot-retention --dry-run` (same env) to preview.

## Two stores with a one-way gap

If the CI zone and production cannot share a bucket, move snapshots across with a bundle.
Both commands read the store from the `SNAPSHOT_STORE_*` env vars of the zone they run in.

```
# CI zone: write new or changed keys (e.g. a re-tagged image), then record their checksums in the state file
snapshot-transfer export --out /media/bundle --state /var/lib/snapshots/exported.json

# production zone, after carrying /media/bundle across
snapshot-transfer import --in /media/bundle
```

Import verifies every checksum before writing anything, writes snapshots before the index,
never overwrites an existing snapshot or digest entry, and overwrites tag entries. Exit `0`
is success, `1` is an error (checksum mismatch, bad key). Keep the state file; deleting it
re-exports everything.
