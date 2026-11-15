# syntax=docker/dockerfile:1
#
# Test image for scripts/test-airgapped.sh. Network is used ONLY at build time;
# the tests then run with --network none (unit) or an --internal network
# (integration). Build from the repo root:
#   docker build -f docker/test.Dockerfile -t snapshotkit-test:local .
# Uses docker/test.Dockerfile.dockerignore (tests/ must be in the context).
#
# URL templates contain the placeholder `{arch}`:
#   gitleaks: amd64 -> x64 | arm64 -> arm64
#   kubectl:  amd64 -> amd64 | arm64 -> arm64
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ARG TARGETARCH
ARG PIP_INDEX_URL
ARG GITLEAKS_URL
ARG GITLEAKS_SHA256_AMD64
ARG GITLEAKS_SHA256_ARM64
ARG KUBECTL_URL
ARG KUBECTL_SHA256_AMD64
ARG KUBECTL_SHA256_ARM64
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git git-lfs \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) gl_arch=x64;   k_arch=amd64; gl_sha="${GITLEAKS_SHA256_AMD64}"; k_sha="${KUBECTL_SHA256_AMD64}" ;; \
      arm64) gl_arch=arm64; k_arch=arm64; gl_sha="${GITLEAKS_SHA256_ARM64}"; k_sha="${KUBECTL_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: '${TARGETARCH}'" >&2; exit 1 ;; \
    esac; \
    [ -n "${GITLEAKS_URL}" ] && [ -n "${KUBECTL_URL}" ] || { echo "GITLEAKS_URL and KUBECTL_URL are required" >&2; exit 1; }; \
    [ -n "${gl_sha}" ] && [ -n "${k_sha}" ] || { echo "checksum args empty for ${TARGETARCH}" >&2; exit 1; }; \
    mkdir -p /dl; cd /dl; \
    curl -fsSL -o gitleaks.tar.gz "$(printf '%s' "${GITLEAKS_URL}" | sed "s/{arch}/${gl_arch}/")"; \
    echo "${gl_sha}  gitleaks.tar.gz" | sha256sum -c -; \
    tar -xzf gitleaks.tar.gz -C /usr/local/bin gitleaks; \
    curl -fsSL -o kubectl "$(printf '%s' "${KUBECTL_URL}" | sed "s/{arch}/${k_arch}/")"; \
    echo "${k_sha}  kubectl" | sha256sum -c -; \
    install -m 0755 kubectl /usr/local/bin/kubectl; \
    cd /; rm -rf /dl

WORKDIR /src
COPY pyproject.toml /src/pyproject.toml
COPY src /src/src
RUN pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} '.[dev]'
COPY config /src/config
COPY deploy /src/deploy
COPY templates /src/templates
COPY docker /src/docker
COPY tests /src/tests

RUN useradd --uid 10001 --create-home --shell /bin/sh tester \
 && chown -R tester /src
USER 10001
CMD ["pytest", "-q"]
