# syntax=docker/dockerfile:1
#
# snapshotkit images. Build from the repo root:
#   docker buildx build -f docker/Dockerfile --target publisher ...
# (scripts/build-images.sh carries the public-internet defaults for dev/CI.)
#
# Nothing here names a public host. All images, packages and binaries come
# from build args, which in the air gap point at the internal mirrors.
#
# URL template scheme: GITLEAKS_URL and CRANE_URL contain the single
# placeholder `{arch}`. The RUN step substitutes the tool's own architecture
# name for it, derived from TARGETARCH:
#   gitleaks: amd64 -> x64      arm64 -> arm64   (gitleaks_<ver>_linux_{arch}.tar.gz)
#   crane:    amd64 -> x86_64   arm64 -> arm64   (go-containerregistry_Linux_{arch}.tar.gz)
# An internal mirror must therefore keep the upstream archive file names.
# Each archive is verified with sha256sum -c against the *_SHA256_<ARCH> arg
# for its architecture; the build fails if that arg is empty.
#
# Targets: `publisher` (this file); Task 11 adds `tool` on top of `app`.

ARG BASE_IMAGE

# ---- fetch: download and verify the static binaries ------------------------
FROM ${BASE_IMAGE} AS fetch
ARG TARGETARCH
ARG GITLEAKS_URL
ARG GITLEAKS_SHA256_AMD64
ARG GITLEAKS_SHA256_ARM64
ARG CRANE_URL
ARG CRANE_SHA256_AMD64
ARG CRANE_SHA256_ARM64
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) gl_arch=x64;    cr_arch=x86_64; gl_sha="${GITLEAKS_SHA256_AMD64}"; cr_sha="${CRANE_SHA256_AMD64}" ;; \
      arm64) gl_arch=arm64;  cr_arch=arm64;  gl_sha="${GITLEAKS_SHA256_ARM64}"; cr_sha="${CRANE_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: '${TARGETARCH}'" >&2; exit 1 ;; \
    esac; \
    [ -n "${GITLEAKS_URL}" ] && [ -n "${CRANE_URL}" ] || { echo "GITLEAKS_URL and CRANE_URL are required" >&2; exit 1; }; \
    [ -n "${gl_sha}" ] || { echo "GITLEAKS_SHA256_* is empty for ${TARGETARCH}" >&2; exit 1; }; \
    [ -n "${cr_sha}" ] || { echo "CRANE_SHA256_* is empty for ${TARGETARCH}" >&2; exit 1; }; \
    mkdir -p /out /dl; cd /dl; \
    curl -fsSL -o gitleaks.tar.gz "$(printf '%s' "${GITLEAKS_URL}" | sed "s/{arch}/${gl_arch}/")"; \
    echo "${gl_sha}  gitleaks.tar.gz" | sha256sum -c -; \
    tar -xzf gitleaks.tar.gz -C /out gitleaks; \
    curl -fsSL -o crane.tar.gz "$(printf '%s' "${CRANE_URL}" | sed "s/{arch}/${cr_arch}/")"; \
    echo "${cr_sha}  crane.tar.gz" | sha256sum -c -; \
    tar -xzf crane.tar.gz -C /out crane; \
    chmod 0755 /out/gitleaks /out/crane

# ---- app: shared stage, installs the snapshotkit wheel ---------------------
FROM ${BASE_IMAGE} AS app
ARG PIP_INDEX_URL
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY pyproject.toml /src/pyproject.toml
COPY src /src/src
RUN pip wheel --no-deps -w /wheels ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} /src \
 && pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} /wheels/snapshotkit-*.whl \
 && rm -rf /src /wheels
RUN useradd --uid 10001 --create-home --shell /bin/sh snapshot

# ---- publisher: CI image with scanner, git, git-lfs and crane --------------
FROM app AS publisher
# git and git-lfs come from the base image's apt (the internal mirror in the air gap).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git git-lfs \
 && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /out/gitleaks /out/crane /usr/local/bin/
COPY config/gitleaks.toml /etc/snapshotkit/gitleaks.toml
USER 10001
WORKDIR /home/snapshot
CMD ["/bin/sh"]
