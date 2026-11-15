#!/usr/bin/env bash
# Build snapshotkit images. Carries the PUBLIC-internet defaults for dev/CI;
# in the air gap override every default via environment (see docker/Dockerfile).
#
# Usage: scripts/build-images.sh [--target publisher|tool] [--platform P] [--tag T] [--push]
#   --platform  default: local arch (linux/amd64 or linux/arm64); comma list for multi-arch
#   --push      push to registry instead of --load (required for multi-platform)
set -euo pipefail

TARGET=publisher
PLATFORM=""
TAG=""
PUSH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:?--target needs a value}"; shift 2 ;;
    --platform) PLATFORM="${2:?--platform needs a value}"; shift 2 ;;
    --tag) TAG="${2:?--tag needs a value}"; shift 2 ;;
    --push) PUSH=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$TARGET" in publisher | tool) ;; *) echo "--target must be publisher or tool" >&2; exit 2 ;; esac

if [ -z "$PLATFORM" ]; then
  case "$(uname -m)" in
    x86_64 | amd64) PLATFORM=linux/amd64 ;;
    aarch64 | arm64) PLATFORM=linux/arm64 ;;
    *) echo "unsupported local arch: $(uname -m)" >&2; exit 2 ;;
  esac
fi
[ -n "$TAG" ] || TAG="snapshot-${TARGET}:dev"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Public defaults. Checksums come from the official release checksum files:
#   gitleaks: gitleaks_8.21.2_checksums.txt in the gitleaks v8.21.2 release
#   crane:    checksums.txt in the go-containerregistry v0.20.2 release
BASE_IMAGE="${BASE_IMAGE:-python:3.12-slim}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
# Defaults sit in single-quoted vars: a literal `}` ({arch}) would end a ${VAR:-...} early.
DEFAULT_GITLEAKS_URL='https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_{arch}.tar.gz'
DEFAULT_CRANE_URL='https://github.com/google/go-containerregistry/releases/download/v0.20.2/go-containerregistry_Linux_{arch}.tar.gz'
GITLEAKS_URL="${GITLEAKS_URL:-$DEFAULT_GITLEAKS_URL}"
GITLEAKS_SHA256_AMD64="${GITLEAKS_SHA256_AMD64:-5bc41815076e6ed6ef8fbecc9d9b75bcae31f39029ceb55da08086315316e3ba}"
GITLEAKS_SHA256_ARM64="${GITLEAKS_SHA256_ARM64:-654c935542c89f565aabe7bf7c6c500830f116c114f0aeb509d2460c1ac2e6da}"
CRANE_URL="${CRANE_URL:-$DEFAULT_CRANE_URL}"
CRANE_SHA256_AMD64="${CRANE_SHA256_AMD64:-c14340087103ba9dadf61d45acd20675490fd0ccbd56ac7901fc1b502137f44b}"
CRANE_SHA256_ARM64="${CRANE_SHA256_ARM64:-aff0db48825124c9331ea310057214bd4e92c01aa2e414d539e9659841d9422a}"

if [ "$PUSH" = 1 ]; then
  OUTPUT=(--push)
elif [[ "$PLATFORM" == *,* ]]; then
  echo "multi-platform builds need --push" >&2; exit 2
else
  OUTPUT=(--load)
fi

exec docker buildx build \
  -f "$ROOT/docker/Dockerfile" \
  --target "$TARGET" \
  --platform "$PLATFORM" \
  --build-arg BASE_IMAGE="$BASE_IMAGE" \
  --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
  --build-arg GITLEAKS_URL="$GITLEAKS_URL" \
  --build-arg GITLEAKS_SHA256_AMD64="$GITLEAKS_SHA256_AMD64" \
  --build-arg GITLEAKS_SHA256_ARM64="$GITLEAKS_SHA256_ARM64" \
  --build-arg CRANE_URL="$CRANE_URL" \
  --build-arg CRANE_SHA256_AMD64="$CRANE_SHA256_AMD64" \
  --build-arg CRANE_SHA256_ARM64="$CRANE_SHA256_ARM64" \
  -t "$TAG" \
  "${OUTPUT[@]}" \
  "$ROOT"
