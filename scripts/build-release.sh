#!/usr/bin/env bash
# Build the Linux release archive from a committed revision or release tag.
set -euo pipefail

if [[ $# -gt 1 ]]; then
    echo 'Usage: bash scripts/build-release.sh [vX.Y.Z]' >&2
    exit 2
fi

ROOT="$(git -C "$(dirname -- "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel)"
cd "${ROOT}"
REF="${1:-HEAD}"

if [[ "${REF}" != 'HEAD' && ! "${REF}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo 'Error: Expected a release tag such as v0.5.0 (or no argument for HEAD).' >&2
    exit 2
fi

if ! git rev-parse --verify --quiet "${REF}^{commit}" >/dev/null; then
    printf 'Error: Ref not found: %s\n' "${REF}" >&2
    exit 1
fi

VERSION="$(git show "${REF}:netbox_ops/__init__.py" | sed -n 's/^__version__ = "\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)"$/\1/p')"
if [[ -z "${VERSION}" ]]; then
    echo 'Error: Could not read the committed version.' >&2
    exit 1
fi
if [[ "${REF}" != 'HEAD' && "${REF}" != "v${VERSION}" ]]; then
    printf 'Error: Tag %s does not match package version %s.\n' "${REF}" "${VERSION}" >&2
    exit 1
fi

mkdir -p dist
ARCHIVE="dist/netbox-ops-v${VERSION}.tar.gz"
TEMP="$(mktemp "${ARCHIVE}.XXXXXXXX")"
trap 'rm -f -- "${TEMP}"' EXIT

# Only commit-tracked installation files; never include local secrets or runtime data.
git archive --format=tar --prefix=netbox-ops/ "${REF}" \
    LICENSE README.md CHANGELOG.md config.example.json \
    install.sh upgrade.sh netbox_ops.py netbox_ops \
    | gzip -n > "${TEMP}"

tar -tzf "${TEMP}" >/dev/null
mv -- "${TEMP}" "${ARCHIVE}"
printf 'Created %s\n' "${ARCHIVE}"
