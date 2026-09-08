#!/usr/bin/env bash
# Build the ephemeral runner image. Called by deploy.sh (remote-install.sh) on
# each host, or by hand.
#
#   ./build.sh [TAG]        default TAG: current date
#
# Needs the runner release tarball in this directory. If it is missing this
# script tries to download it; on a host whose network throttles GitHub's asset
# CDN (build-farm-2 / the 10.52 net) that download will fail — in that
# case stage the file first:
#
#   scp deploy@10.0.0.10:/tmp/actions-runner-*.tar.gz containerfile/
#
# and re-run. deploy.sh handles this automatically.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

tag="${1:-$(date +%Y-%m-%d)}"
image="localhost/sp-runner"
runner_version="${RUNNER_VERSION:-2.337.0}"
runner_arch="${RUNNER_ARCH:-x64}"
tarball="actions-runner-linux-${runner_arch}-${runner_version}.tar.gz"

if [[ ! -f "$tarball" ]]; then
    url="https://github.com/actions/runner/releases/download/v${runner_version}/${tarball}"
    echo "[build] runner tarball not staged — downloading ${url}"
    if ! curl -fL --connect-timeout 15 --max-time 300 -o "${tarball}.part" "$url"; then
        echo "[build] ERROR: could not download the runner tarball." >&2
        echo "[build]        Stage it manually into $(pwd)/ and re-run:" >&2
        echo "[build]          scp <a-host-with-github-access>:/path/${tarball} ." >&2
        rm -f "${tarball}.part"
        exit 1
    fi
    mv "${tarball}.part" "$tarball"
fi
echo "[build] runner tarball: $tarball ($(du -h "$tarball" | cut -f1))"

echo "[build] ${image}:${tag}  (ubuntu:22.04 base, runner ${runner_version})"
podman build \
    --file "${here}/Containerfile" \
    --build-arg "RUNNER_VERSION=${runner_version}" \
    --build-arg "RUNNER_ARCH=${runner_arch}" \
    --tag "${image}:${tag}" \
    --tag "${image}:latest" \
    "${here}"

echo "[build] done:"
podman images "${image}"
