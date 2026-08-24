#!/usr/bin/env bash
set -euo pipefail
unset PYTHONHOME PYTHONPATH
umask 022

usage() {
  cat <<'EOF'
Usage: scripts/build-preview-bundle.sh [--output-dir DIR] [--ref REF]

Build an Ubuntu 24.04 x86_64 / CPython 3.12 offline preview archive from a
clean Git revision. PREVIEW_PYTHON may name the matching interpreter used to
build the wheelhouse.
EOF
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
manifest_helper="$script_dir/install_manifest.py"
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
  printf '%s\n' "Preview bundles require Linux x86_64" >&2
  exit 2
fi
if [[ ! -r /etc/os-release ]]; then
  printf '%s\n' "Cannot identify the Ubuntu build host" >&2
  exit 2
fi
# shellcheck disable=SC1091 -- this is the standard build-host identity file.
source /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 ]]; then
  printf '%s\n' "Preview bundles require Ubuntu 24.04" >&2
  exit 2
fi
mapfile -t python_identity < <(
  "$build_python" -I - <<'PY'
import platform
import struct
import sys

print(platform.python_implementation())
print(f"{sys.version_info.major}.{sys.version_info.minor}")
print(platform.machine())
print(struct.calcsize("P") * 8)
PY
)
if ((${#python_identity[@]} != 4)) \
  || [[ ${python_identity[0]} != CPython \
    || ${python_identity[1]} != 3.12 \
    || ${python_identity[2]} != x86_64 \
    || ${python_identity[3]} != 64 ]]; then
  printf '%s\n' \
    "Preview bundles require 64-bit CPython 3.12 on x86_64" >&2
  exit 2
fi
ubuntu_version=24.04
python_tag=py3.12

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
if [[ ! -f $manifest_helper || -L $manifest_helper ]]; then
  printf '%s\n' "The no-clobber artifact publisher is unavailable" >&2
  exit 2
fi
"$build_python" -I "$manifest_helper" secure-dir \
  --path "$output_dir" --kind "preview output"
archive_path="$output_dir/$bundle_name.tar.gz"
archive_checksum="$archive_path.sha256"
if [[ -e $archive_path || -L $archive_path \
  || -e $archive_checksum || -L $archive_checksum ]]; then
  printf 'Refusing to overwrite an existing preview artifact: %s\n' \
    "$archive_path" >&2
  exit 2
fi

staging_dir=$(mktemp -d)
archive_temporary=""
checksum_temporary=""
cleanup() {
  rm -rf --one-file-system -- "$staging_dir"
  if [[ -n ${archive_temporary:-} ]]; then
    rm -f -- "$archive_temporary"
  fi
  if [[ -n ${checksum_temporary:-} ]]; then
    rm -f -- "$checksum_temporary"
  fi
}
trap cleanup EXIT
archive_temporary=$(mktemp "$output_dir/.${bundle_name}.tar.gz.tmp.XXXXXX")
checksum_temporary=$(mktemp "$output_dir/.${bundle_name}.tar.gz.sha256.tmp.XXXXXX")
chmod 0600 "$archive_temporary" "$checksum_temporary"
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
runtime_lock="$bundle_dir/packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt"
build_lock="$bundle_dir/packaging/requirements-preview-build-cp312.txt"
for lock in "$runtime_lock" "$build_lock"; do
  if [[ ! -f $lock || -L $lock ]]; then
    printf 'Required preview lock is missing or unsafe: %s\n' "$lock" >&2
    exit 2
  fi
done

# Runtime packages are downloaded as wheels only, with exact versions and
# hashes from the target-specific lock. Let pip traverse dependencies so an
# unlisted or unpinned transitive dependency makes --require-hashes fail.
PIP_CONFIG_FILE=/dev/null \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  PIP_NO_INPUT=1 \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH= \
  "$build_python" -I -m pip --isolated download \
    --no-cache-dir \
    --require-hashes \
    --only-binary=:all: \
    --dest "$bundle_dir/wheelhouse" \
    --requirement "$runtime_lock"

# Prove that pip produced exactly one wheel for every locked runtime entry and
# no additional runtime wheel. Reading package metadata avoids relying on
# ambiguous filename normalization.
PYTHONNOUSERSITE=1 PYTHONPATH= \
  "$build_python" -I - "$runtime_lock" "$bundle_dir/wheelhouse" <<'PY'
from __future__ import annotations

import hashlib
import re
import sys
from email.parser import BytesParser
from pathlib import Path
from zipfile import BadZipFile, ZipFile

lock_path = Path(sys.argv[1])
wheelhouse = Path(sys.argv[2])
requirement = re.compile(r"^([A-Za-z0-9_.-]+)==([^\\\s]+) \\$")
hash_option = re.compile(r"^\s+--hash=sha256:([0-9a-f]{64})$")
normalize = lambda value: re.sub(r"[-_.]+", "-", value).lower()

lines = lock_path.read_text(encoding="utf-8").splitlines()
locked: dict[str, tuple[str, str]] = {}
line_number = 0
while line_number < len(lines):
    line = lines[line_number]
    line_number += 1
    if not line or line.startswith("#"):
        continue
    match = requirement.fullmatch(line)
    if match is None or line_number >= len(lines):
        raise SystemExit(f"invalid locked requirement at line {line_number}")
    hash_match = hash_option.fullmatch(lines[line_number])
    line_number += 1
    if hash_match is None:
        raise SystemExit(f"missing wheel hash at line {line_number}")
    name = normalize(match.group(1))
    if name in locked:
        raise SystemExit(f"duplicate locked requirement: {name}")
    locked[name] = (match.group(2), hash_match.group(1))

observed: dict[str, tuple[str, str]] = {}
for wheel in sorted(wheelhouse.glob("*.whl")):
    try:
        with ZipFile(wheel) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise SystemExit(f"wheel has ambiguous metadata: {wheel.name}")
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    except BadZipFile as error:
        raise SystemExit(f"invalid wheel archive: {wheel.name}: {error}") from error
    raw_name = metadata.get("Name")
    version = metadata.get("Version")
    if not raw_name or not version:
        raise SystemExit(f"wheel metadata is incomplete: {wheel.name}")
    name = normalize(raw_name)
    if name in observed:
        raise SystemExit(f"duplicate runtime wheel: {name}")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    observed[name] = (version, digest)

if observed != locked:
    raise SystemExit(
        f"runtime wheelhouse does not exactly match lock: "
        f"locked={locked!r}, observed={observed!r}"
    )
PY

# setuptools may create build/ and *.egg-info beside a local source tree.
# Build from a disposable copy so the exported source remains byte-for-byte
# identical to git archive.
voice_build_dir="$staging_dir/voice-build"
mkdir -m 0755 -- "$voice_build_dir"
cp -a -- "$bundle_dir/voice/." "$voice_build_dir/"
build_dependency_dir="$staging_dir/build-dependencies"
build_environment="$staging_dir/build-environment"
mkdir -m 0755 -- "$build_dependency_dir"
PIP_CONFIG_FILE=/dev/null \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  PIP_NO_INPUT=1 \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH= \
  "$build_python" -I -m pip --isolated download \
    --no-cache-dir \
    --require-hashes \
    --only-binary=:all: \
    --no-deps \
    --dest "$build_dependency_dir" \
    --requirement "$build_lock"
"$build_python" -I -m venv "$build_environment"
if PYTHONNOUSERSITE=1 PYTHONPATH= \
  "$build_environment/bin/python" -I -c 'import setuptools' 2>/dev/null; then
  printf '%s\n' \
    "The disposable build environment unexpectedly contains setuptools" >&2
  exit 2
fi
PIP_CONFIG_FILE=/dev/null \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  PIP_NO_INPUT=1 \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH= \
  "$build_environment/bin/python" -I -m pip --isolated install \
    --no-cache-dir \
    --no-index \
    --require-hashes \
    --only-binary=:all: \
    --find-links "$build_dependency_dir" \
    --requirement "$build_lock"
PIP_CONFIG_FILE=/dev/null \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  PIP_NO_INPUT=1 \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH= \
  SOURCE_DATE_EPOCH="$source_epoch" \
  "$build_environment/bin/python" -I -m pip --isolated wheel \
    --no-cache-dir \
    --no-build-isolation \
    --no-deps \
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
  "python=$("$build_python" -I --version 2>&1)" \
  "runtime_lock=packaging/$(basename -- "$runtime_lock")" \
  "runtime_lock_sha256=$(sha256sum "$runtime_lock" | cut -d ' ' -f 1)" \
  "build_backend_lock=packaging/$(basename -- "$build_lock")" \
  "build_backend_lock_sha256=$(sha256sum "$build_lock" | cut -d ' ' -f 1)" \
  >"$bundle_dir/BUNDLE-INFO"

"$build_python" -I "$bundle_dir/scripts/generate_preview_sbom.py" \
  --bundle-root "$bundle_dir" \
  --output "$bundle_dir/SBOM.cdx.json"

"$build_python" -I - "$bundle_dir" <<'PY'
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
    "$bundle_name" | gzip -n -9 >"$archive_temporary"
)
archive_digest=$(sha256sum "$archive_temporary")
archive_digest=${archive_digest%% *}
if [[ ! $archive_digest =~ ^[0-9a-f]{64}$ ]]; then
  printf '%s\n' "The preview archive SHA256 could not be computed" >&2
  exit 2
fi
printf '%s  %s\n' "$archive_digest" "$(basename -- "$archive_path")" \
  >"$checksum_temporary"
chmod 0644 "$archive_temporary" "$checksum_temporary"

# Flush both complete files before atomically publishing either fixed name.
# renameat2(RENAME_NOREPLACE) in the helper closes the long build-time race:
# a file created after the early check is retained, never truncated.
"$build_python" -I - "$archive_temporary" "$checksum_temporary" <<'PY'
import os
import sys

flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
for raw_path in sys.argv[1:]:
    descriptor = os.open(raw_path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY

"$build_python" -I "$manifest_helper" move-no-clobber \
  --source "$archive_temporary" --destination "$archive_path" >/dev/null
archive_temporary=""
if ! "$build_python" -I "$manifest_helper" move-no-clobber \
  --source "$checksum_temporary" --destination "$archive_checksum" >/dev/null; then
  printf 'Archive published, but the checksum destination already exists: %s\n' \
    "$archive_checksum" >&2
  exit 1
fi
checksum_temporary=""
"$build_python" -I - "$output_dir" <<'PY'
import os
import sys

flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
flags |= getattr(os, "O_DIRECTORY", 0)
descriptor = os.open(sys.argv[1], flags)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY

printf '%s\n' "$archive_path"
