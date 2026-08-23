#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/build-preview-bundle.sh [--output-dir DIR] [--ref REF]

Build an Ubuntu x86_64 offline preview archive from a clean Git revision.
PREVIEW_PYTHON may name the Python interpreter used to build the wheelhouse.
EOF
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
output_dir="$repo_dir/dist"
source_ref=HEAD
while (($#)); do
  case "$1" in
    --output-dir)
      if (($# < 2)); then
        printf '%s\n' "--output-dir requires a directory" >&2
        exit 2
      fi
      output_dir=$2
      shift 2
      ;;
    --ref)
      if (($# < 2)); then
        printf '%s\n' "--ref requires a Git revision" >&2
        exit 2
      fi
      source_ref=$2
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

for tool in git tar gzip sha256sum uname; do
  if ! command -v "$tool" >/dev/null; then
    printf 'Required build tool is missing: %s\n' "$tool" >&2
    exit 2
  fi
done
build_python=${PREVIEW_PYTHON:-python3}
if ! command -v "$build_python" >/dev/null; then
  printf 'Preview Python is unavailable: %s\n' "$build_python" >&2
  exit 2
fi
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  printf '%s\n' "Preview bundles must be built on Linux x86_64" >&2
  exit 2
fi
if [[ ! -r /etc/os-release ]]; then
  printf '%s\n' "Cannot identify the Ubuntu build host" >&2
  exit 2
fi
# shellcheck disable=SC1091 -- this is the standard build-host identity file.
source /etc/os-release
if [[ ${ID:-} != ubuntu || -z ${VERSION_ID:-} ]]; then
  printf '%s\n' "Preview bundles must be built on an Ubuntu host" >&2
  exit 2
fi
ubuntu_version=${VERSION_ID//[^0-9.]/}
if [[ -z $ubuntu_version ]]; then
  printf '%s\n' "The Ubuntu version could not be normalized" >&2
  exit 2
fi
python_tag=$(
  "$build_python" -c \
    'import sys; print(f"py{sys.version_info.major}.{sys.version_info.minor}")'
)

if [[ -n $(git -C "$repo_dir" status --porcelain=v1 --untracked-files=normal) ]]; then
  printf '%s\n' \
    "Refusing to build from a dirty tree." \
    "Commit or remove tracked and non-ignored untracked changes first." >&2
  exit 2
fi
commit=$(git -C "$repo_dir" rev-parse --verify "$source_ref^{commit}")
short_commit=$(git -C "$repo_dir" rev-parse --short=12 "$commit")
source_epoch=$(git -C "$repo_dir" show -s --format=%ct "$commit")
target="ubuntu-${ubuntu_version}-x86_64-${python_tag}"
bundle_name="openVoiceInput_linux-preview-${short_commit}-${target}"

mkdir -p -- "$output_dir"
output_dir=$(CDPATH= cd -- "$output_dir" && pwd)
archive_path="$output_dir/$bundle_name.tar.gz"
archive_checksum="$archive_path.sha256"
if [[ -e $archive_path || -e $archive_checksum ]]; then
  printf 'Refusing to overwrite an existing preview artifact: %s\n' \
    "$archive_path" >&2
  exit 2
fi

staging_dir=$(mktemp -d)
cleanup() {
  rm -rf --one-file-system -- "$staging_dir"
}
trap cleanup EXIT
bundle_dir="$staging_dir/$bundle_name"

# The source payload comes only from the selected committed tree. Local
# credentials, ignored caches, build outputs, and .git metadata cannot enter.
git -C "$repo_dir" archive \
  --format=tar \
  --prefix="$bundle_name/" \
  "$commit" | tar -xf - -C "$staging_dir"
if [[ -n $(find "$bundle_dir" -type l -print -quit) ]]; then
  printf '%s\n' "Preview source archives may not contain symbolic links" >&2
  exit 2
fi

mkdir -m 0755 -- "$bundle_dir/wheelhouse"
# setuptools may create build/ and *.egg-info beside a local source tree.
# Build from a disposable copy so the exported source remains byte-for-byte
# identical to git archive.
voice_build_dir="$staging_dir/voice-build"
mkdir -m 0755 -- "$voice_build_dir"
cp -a -- "$bundle_dir/voice/." "$voice_build_dir/"
PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 \
  "$build_python" -m pip wheel \
    --no-cache-dir \
    --only-binary=:all: \
    --wheel-dir "$bundle_dir/wheelhouse" \
    "$voice_build_dir"

mapfile -t project_wheels < <(
  find "$bundle_dir/wheelhouse" -maxdepth 1 -type f \
    -name 'murmur_ime_voice-*.whl' -print | sort
)
if ((${#project_wheels[@]} != 1)); then
  printf '%s\n' \
    "The preview wheelhouse must contain exactly one project wheel" >&2
  exit 2
fi
if [[ -n $(find "$bundle_dir/wheelhouse" -maxdepth 1 -type f \
  ! -name '*.whl' -print -quit) ]]; then
  printf '%s\n' "The preview wheelhouse may contain only wheel files" >&2
  exit 2
fi

printf '%s\n' \
  "source_commit=$commit" \
  "target=$target" \
  "python=$("$build_python" --version 2>&1)" \
  >"$bundle_dir/BUNDLE-INFO"

"$build_python" - "$bundle_dir" <<'PY'
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1])
entries: list[str] = []
for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
    relative = path.relative_to(root).as_posix()
    if relative == "SHA256SUMS":
        continue
    if "\n" in relative or "\r" in relative:
        raise SystemExit(f"manifest cannot represent filename: {relative!r}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    entries.append(f"{digest}  {relative}\n")
(root / "SHA256SUMS").write_text("".join(entries), encoding="utf-8")
PY

(
  cd -- "$staging_dir"
  tar \
    --sort=name \
    --mtime="@$source_epoch" \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    -cf - \
    "$bundle_name" | gzip -n -9 >"$archive_path"
)
(
  cd -- "$output_dir"
  sha256sum "$(basename -- "$archive_path")" \
    >"$(basename -- "$archive_checksum")"
)

printf '%s\n' "$archive_path"
