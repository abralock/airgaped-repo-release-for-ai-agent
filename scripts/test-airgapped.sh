#!/usr/bin/env bash
# Air-gapped test run: unit tests with --network none, integration tests on a
# docker --internal network (only MinIO reachable). Network is used only to
# build images / pull MinIO beforehand. Public defaults mirror build-images.sh;
# override any of them via environment for the real air gap.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMG=snapshotkit-test:local
NET=snapshotkit-airgap
MINIO=snapshotkit-airgap-minio
MINIO_IMAGE="${MINIO_IMAGE:-cgr.dev/chainguard/minio@sha256:634c989a2f1029162fc2676c263caa181213b9410449968532f750a6fb77532d}"

DEFAULT_GITLEAKS_URL='https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_{arch}.tar.gz'
DEFAULT_KUBECTL_URL='https://dl.k8s.io/release/v1.31.4/bin/linux/{arch}/kubectl'
GITLEAKS_URL="${GITLEAKS_URL:-$DEFAULT_GITLEAKS_URL}"
GITLEAKS_SHA256_AMD64="${GITLEAKS_SHA256_AMD64:-5bc41815076e6ed6ef8fbecc9d9b75bcae31f39029ceb55da08086315316e3ba}"
GITLEAKS_SHA256_ARM64="${GITLEAKS_SHA256_ARM64:-654c935542c89f565aabe7bf7c6c500830f116c114f0aeb509d2460c1ac2e6da}"
# kubectl v1.31.4 checksums from https://dl.k8s.io/release/v1.31.4/bin/linux/<arch>/kubectl.sha256
KUBECTL_URL="${KUBECTL_URL:-$DEFAULT_KUBECTL_URL}"
KUBECTL_SHA256_AMD64="${KUBECTL_SHA256_AMD64:-298e19e9c6c17199011404278f0ff8168a7eca4217edad9097af577023a5620f}"
KUBECTL_SHA256_ARM64="${KUBECTL_SHA256_ARM64:-b97e93c20e3be4b8c8fa1235a41b4d77d4f2022ed3d899230dbbbbd43d26f872}"
DEFAULT_CRANE_URL='https://github.com/google/go-containerregistry/releases/download/v0.20.2/go-containerregistry_Linux_{arch}.tar.gz'
CRANE_URL="${CRANE_URL:-$DEFAULT_CRANE_URL}"
CRANE_SHA256_AMD64="${CRANE_SHA256_AMD64:-c14340087103ba9dadf61d45acd20675490fd0ccbd56ac7901fc1b502137f44b}"
CRANE_SHA256_ARM64="${CRANE_SHA256_ARM64:-aff0db48825124c9331ea310057214bd4e92c01aa2e414d539e9659841d9422a}"
BASE_IMAGE="${BASE_IMAGE:-python:3.12-slim}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"

cleanup() {
  docker rm -f "$MINIO" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

step() { printf '\n==== %s ====\n' "$*"; }

step "1/5 build test image"
docker build -f docker/test.Dockerfile -t "$IMG" \
  --build-arg BASE_IMAGE="$BASE_IMAGE" \
  --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
  --build-arg GITLEAKS_URL="$GITLEAKS_URL" \
  --build-arg GITLEAKS_SHA256_AMD64="$GITLEAKS_SHA256_AMD64" \
  --build-arg GITLEAKS_SHA256_ARM64="$GITLEAKS_SHA256_ARM64" \
  --build-arg KUBECTL_URL="$KUBECTL_URL" \
  --build-arg KUBECTL_SHA256_AMD64="$KUBECTL_SHA256_AMD64" \
  --build-arg KUBECTL_SHA256_ARM64="$KUBECTL_SHA256_ARM64" \
  .

step "2/5 unit tests, --network none"
docker run --rm --network none \
  -e SNAPSHOT_TEST_REQUIRE_KUBECTL=1 \
  "$IMG" pytest -q -m "not integration"

step "3/5 prove the sandbox has no route out"
if docker run --rm --network none "$IMG" python -c '
import socket, sys
s = socket.socket(); s.settimeout(3)
try:
    s.connect(("1.1.1.1", 443))
except OSError as e:
    print("connect failed as expected:", e); sys.exit(0)
print("UNEXPECTED: connected to 1.1.1.1:443"); sys.exit(1)'; then :; else
  echo "air-gap proof failed (--network none)" >&2; exit 1
fi

step "4/5 integration tests on an --internal network with MinIO"
docker pull "$MINIO_IMAGE" >/dev/null 2>&1 || docker image inspect "$MINIO_IMAGE" >/dev/null \
  || { echo "cannot pull or find $MINIO_IMAGE" >&2; exit 1; }
cleanup
docker network create --internal "$NET" >/dev/null
docker run -d --name "$MINIO" --network "$NET" --network-alias minio \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  "$MINIO_IMAGE" server /data >/dev/null
# Internal network: prove no route out from it too, then run the tests
# (the s3_bucket fixture polls until MinIO is ready).
docker run --rm --network "$NET" "$IMG" python -c '
import socket, sys
s = socket.socket(); s.settimeout(3)
try:
    s.connect(("1.1.1.1", 443))
except OSError:
    sys.exit(0)
sys.exit(1)'
docker run --rm --network "$NET" \
  -e SNAPSHOT_TEST_S3_ENDPOINT=http://minio:9000 \
  -e SNAPSHOT_TEST_S3_ACCESS_KEY=minioadmin \
  -e SNAPSHOT_TEST_S3_SECRET_KEY=minioadmin \
  -e SNAPSHOT_TEST_REQUIRE_S3=1 \
  "$IMG" pytest -q -m integration

step "5/5 product images + smoke checks, --network none"
for target in publisher tool; do
  docker build -f docker/Dockerfile --target "$target" -t "snapshot-${target}:airgap-test" \
    --build-arg BASE_IMAGE="$BASE_IMAGE" \
    --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
    --build-arg GITLEAKS_URL="$GITLEAKS_URL" \
    --build-arg GITLEAKS_SHA256_AMD64="$GITLEAKS_SHA256_AMD64" \
    --build-arg GITLEAKS_SHA256_ARM64="$GITLEAKS_SHA256_ARM64" \
    --build-arg CRANE_URL="$CRANE_URL" \
    --build-arg CRANE_SHA256_AMD64="$CRANE_SHA256_AMD64" \
    --build-arg CRANE_SHA256_ARM64="$CRANE_SHA256_ARM64" \
    .
done
docker run --rm --network none snapshot-publisher:airgap-test \
  sh -c 'gitleaks version && git lfs version && crane version && snapshot-publish --help >/dev/null'
out="$(docker run --rm --network none --entrypoint sh snapshot-tool:airgap-test -c 'command -v git || echo no-git')"
[ "$out" = "no-git" ] || { echo "tool image unexpectedly contains git: $out" >&2; exit 1; }

echo
echo "AIRGAPPED TESTS PASSED"
