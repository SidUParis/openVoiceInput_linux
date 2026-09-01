#!/usr/bin/env bash
set -euo pipefail
unset PYTHONHOME PYTHONPATH
umask 022

usage() {
  cat <<'EOF'
Usage: scripts/build-deb.sh --wheelhouse DIR [--output-dir DIR] [--ref REF]

Build the Ubuntu 24.04 amd64 Debian package from one clean, exact Git revision.
The build is offline: DIR must contain every wheel in the repository's fixed
runtime hash lock. A matching preview-bundle wheelhouse may also contain its
project wheel; application code always comes from REF, so that wheel is ignored.
EOF
}

die() {
  printf '%s\n' "$1" >&2
  exit "${2:-1}"
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
source_ref=HEAD
output_dir="$repo_dir/dist"
wheelhouse=""
while (($#)); do
  case "$1" in
    --wheelhouse)
      (($# >= 2)) || die "--wheelhouse requires a directory" 2
      wheelhouse=$2
      shift 2
      ;;
    --output-dir)
      (($# >= 2)) || die "--output-dir requires a directory" 2
      output_dir=$2
      shift 2
      ;;
    --ref)
      (($# >= 2)) || die "--ref requires a Git revision" 2
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
[[ -n $wheelhouse ]] || die "--wheelhouse is required" 2

for tool in appstreamcli cmp cut desktop-file-validate dpkg-deb du find git \
  grep gzip install ln mkdir mktemp python3 sha256sum stat tar touch uname; do
  command -v "$tool" >/dev/null || die "Required build tool is missing: $tool" 2
done
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || die \
  "Debian preview packages require Linux x86_64" 2
[[ -r /etc/os-release ]] || die "Cannot identify the Ubuntu build host" 2
# shellcheck disable=SC1091 -- standard operating-system identity file.
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || die \
  "Debian preview packages require Ubuntu 24.04" 2
mapfile -t python_identity < <(
  python3 -I - <<'PY'
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
  die "Debian preview packages require 64-bit CPython 3.12 on x86_64" 2
fi

if [[ -n $(git -C "$repo_dir" status --porcelain=v1 --untracked-files=normal) ]]; then
  die "Refusing to build a Debian package from a dirty tree." 2
fi
commit=$(git -C "$repo_dir" rev-parse --verify "$source_ref^{commit}")
short_commit=$(git -C "$repo_dir" rev-parse --short=12 "$commit")
source_epoch=$(git -C "$repo_dir" show -s --format=%ct "$commit")
[[ $commit =~ ^[0-9a-f]{40}$ ]] || die "Git returned an invalid source commit" 2
[[ $short_commit =~ ^[0-9a-f]{12}$ ]] || die "Git returned an invalid short commit" 2
[[ $source_epoch =~ ^[1-9][0-9]{8,11}$ ]] || die \
  "Git returned an invalid source timestamp" 2

wheelhouse=$(CDPATH= cd -- "$wheelhouse" && pwd)
[[ -d $wheelhouse && ! -L $wheelhouse ]] || die \
  "The offline wheelhouse must be a real directory" 2
mkdir -p -- "$output_dir"
output_dir=$(CDPATH= cd -- "$output_dir" && pwd)

staging_dir=$(mktemp -d)
package_temporary=""
checksum_temporary=""
cleanup() {
  rm -rf --one-file-system -- "$staging_dir"
  if [[ -n ${package_temporary:-} ]]; then
    rm -f -- "$package_temporary"
  fi
  if [[ -n ${checksum_temporary:-} ]]; then
    rm -f -- "$checksum_temporary"
  fi
}
trap cleanup EXIT

source_root="$staging_dir/source"
mkdir -m 0755 -- "$source_root"
git -C "$repo_dir" archive --format=tar "$commit" | tar -xf - -C "$source_root"
if [[ -n $(find "$source_root" -type l -print -quit) ]]; then
  die "The selected source revision contains a symbolic link" 2
fi

# The executing builder and helper must be bytes from the selected revision,
# not an uncommitted or older script operating on a different source snapshot.
for relative in scripts/build-deb.sh scripts/build_deb_support.py; do
  [[ -f $source_root/$relative ]] || die \
    "The selected revision does not contain $relative" 2
  cmp -s -- "$repo_dir/$relative" "$source_root/$relative" || die \
    "The executing builder differs from the selected Git revision: $relative" 2
done

helper="$source_root/scripts/build_deb_support.py"
manifest_helper="$source_root/scripts/install_manifest.py"
debian_source="$source_root/packaging/debian"
runtime_lock="$source_root/packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt"
for required in \
  "$helper" \
  "$manifest_helper" \
  "$runtime_lock" \
  "$debian_source/control.in" \
  "$debian_source/copyright" \
  "$debian_source/THIRD-PARTY-NOTICES" \
  "$debian_source/README.md" \
  "$debian_source/preinst" \
  "$debian_source/postinst" \
  "$debian_source/prerm" \
  "$debian_source/postrm" \
  "$debian_source/open-voice-input-launcher.py" \
  "$debian_source/murmur-ime-engine.service" \
  "$debian_source/murmur-ime-voice.service" \
  "$debian_source/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop" \
  "$debian_source/io.github.SidUParis.OpenVoiceInputLinux.metainfo.xml" \
  "$source_root/packaging/icons/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg"; do
  [[ -f $required && ! -L $required ]] || die \
    "Required Debian package input is missing or unsafe: $required" 2
done

python3 -I "$manifest_helper" secure-dir \
  --path "$output_dir" --kind "Debian package output"
package_version=$(python3 -I "$helper" version \
  --pyproject "$source_root/voice/pyproject.toml" \
  --source-epoch "$source_epoch" \
  --short-commit "$short_commit")
package_name="open-voice-input-linux_${package_version}_amd64.deb"
package_path="$output_dir/$package_name"
checksum_path="$package_path.sha256"
if [[ -e $package_path || -L $package_path \
  || -e $checksum_path || -L $checksum_path ]]; then
  die "Refusing to overwrite an existing Debian package artifact: $package_path" 2
fi

package_root="$staging_dir/package-root"
vendor_root="$package_root/usr/lib/open-voice-input-linux/python"
mkdir -m 0755 -p \
  "$package_root/DEBIAN" \
  "$package_root/usr/bin" \
  "$package_root/usr/lib/systemd/user/graphical-session.target.wants" \
  "$package_root/usr/share/applications" \
  "$package_root/usr/share/doc/open-voice-input-linux" \
  "$package_root/usr/share/icons/hicolor/scalable/apps"
mkdir -m 0755 -p "$package_root/usr/share/metainfo"

python3 -I "$helper" unpack-runtime \
  --lock "$runtime_lock" \
  --wheelhouse "$wheelhouse" \
  --output "$vendor_root"
cp -a -- "$source_root/voice/murmur_voice" "$vendor_root/murmur_voice"
cp -a -- "$source_root/engine/murmur_ime_engine" "$vendor_root/murmur_ime_engine"
if [[ -n $(find "$vendor_root" -type l -print -quit) ]]; then
  die "The private Python import tree contains a symbolic link" 2
fi

for launcher in murmur-ime-engine murmur-voice-daemon open-voice-input-settings; do
  install -m 0755 -- "$debian_source/open-voice-input-launcher.py" \
    "$package_root/usr/bin/$launcher"
done
install -m 0644 -- "$debian_source/murmur-ime-engine.service" \
  "$package_root/usr/lib/systemd/user/murmur-ime-engine.service"
install -m 0644 -- "$debian_source/murmur-ime-voice.service" \
  "$package_root/usr/lib/systemd/user/murmur-ime-voice.service"
ln -s ../murmur-ime-engine.service \
  "$package_root/usr/lib/systemd/user/graphical-session.target.wants/murmur-ime-engine.service"
install -m 0644 -- \
  "$debian_source/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop" \
  "$package_root/usr/share/applications/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop"
install -m 0644 -- \
  "$source_root/packaging/icons/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg" \
  "$package_root/usr/share/icons/hicolor/scalable/apps/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg"
install -m 0644 -- \
  "$debian_source/io.github.SidUParis.OpenVoiceInputLinux.metainfo.xml" \
  "$package_root/usr/share/metainfo/io.github.SidUParis.OpenVoiceInputLinux.metainfo.xml"
install -m 0644 -- "$debian_source/copyright" \
  "$package_root/usr/share/doc/open-voice-input-linux/copyright"
install -m 0644 -- "$debian_source/THIRD-PARTY-NOTICES" \
  "$package_root/usr/share/doc/open-voice-input-linux/THIRD-PARTY-NOTICES"
install -m 0644 -- "$debian_source/README.md" \
  "$package_root/usr/share/doc/open-voice-input-linux/README.Debian"
install -m 0644 -- "$source_root/LICENSE" \
  "$package_root/usr/share/doc/open-voice-input-linux/LICENSE"
install -m 0644 -- "$source_root/NOTICE.md" \
  "$package_root/usr/share/doc/open-voice-input-linux/NOTICE.md"
install -m 0644 -- "$source_root/voice/NOTICE.md" \
  "$package_root/usr/share/doc/open-voice-input-linux/VOICE-NOTICE.md"
SOURCE_DATE_EPOCH="$source_epoch" gzip -n -9 -c -- "$source_root/CHANGELOG.md" \
  >"$package_root/usr/share/doc/open-voice-input-linux/changelog.gz"

runtime_lock_digest=$(sha256sum "$runtime_lock")
runtime_lock_digest=${runtime_lock_digest%% *}
printf '%s\n' \
  "source_commit=$commit" \
  "source_epoch=$source_epoch" \
  "package_version=$package_version" \
  "target=ubuntu-24.04-amd64-cpython-3.12" \
  "runtime_lock=packaging/$(basename -- "$runtime_lock")" \
  "runtime_lock_sha256=$runtime_lock_digest" \
  >"$package_root/usr/share/doc/open-voice-input-linux/BUILD-INFO"
python3 -I "$helper" write-sbom \
  --lock "$runtime_lock" \
  --output "$package_root/usr/share/doc/open-voice-input-linux/SBOM.cdx.json" \
  --source-commit "$commit" \
  --source-epoch "$source_epoch" \
  --package-version "$package_version"

desktop-file-validate \
  "$package_root/usr/share/applications/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop"
appstreamcli validate --no-net \
  "$package_root/usr/share/metainfo/io.github.SidUParis.OpenVoiceInputLinux.metainfo.xml"

install -m 0755 -- "$debian_source/preinst" "$package_root/DEBIAN/preinst"
install -m 0755 -- "$debian_source/postinst" "$package_root/DEBIAN/postinst"
install -m 0755 -- "$debian_source/prerm" "$package_root/DEBIAN/prerm"
install -m 0755 -- "$debian_source/postrm" "$package_root/DEBIAN/postrm"
# Normalize all payload modes and timestamps after copying from wheels and Git.
find "$vendor_root" -type d -exec chmod 0755 {} +
find "$vendor_root" -type f -exec chmod 0644 {} +
find "$package_root" -exec touch -h -d "@$source_epoch" {} +
installed_size=$(du -s -k --exclude=DEBIAN "$package_root" | cut -f 1)
if ((installed_size > 10 * 1024)); then
  die "Debian payload exceeds the 10 MiB lightweight-client budget" 2
fi
python3 -I "$helper" render-control \
  --template "$debian_source/control.in" \
  --output "$package_root/DEBIAN/control" \
  --version "$package_version" \
  --installed-size "$installed_size"
chmod 0644 "$package_root/DEBIAN/control"
# Rendering control changes the DEBIAN directory timestamp. Normalize the
# complete tree only after the final package byte has been written.
find "$package_root" -exec touch -h -d "@$source_epoch" {} +

package_temporary=$(mktemp "$output_dir/.${package_name}.tmp.XXXXXX")
checksum_temporary=$(mktemp "$output_dir/.${package_name}.sha256.tmp.XXXXXX")
chmod 0600 "$package_temporary" "$checksum_temporary"
SOURCE_DATE_EPOCH="$source_epoch" dpkg-deb \
  --root-owner-group -Zxz -z9 --build "$package_root" "$package_temporary" >/dev/null
package_bytes=$(stat --format='%s' "$package_temporary")
if ((package_bytes > 5 * 1024 * 1024)); then
  die "Debian package exceeds the 5 MiB lightweight-client budget" 2
fi

# Inspect the completed archive before publication. No package installation is
# performed by this builder.
[[ $(dpkg-deb -f "$package_temporary" Package) == open-voice-input-linux ]] || die \
  "Built package has the wrong package name" 2
[[ $(dpkg-deb -f "$package_temporary" Version) == "$package_version" ]] || die \
  "Built package has the wrong package version" 2
[[ $(dpkg-deb -f "$package_temporary" Architecture) == amd64 ]] || die \
  "Built package has the wrong architecture" 2
if dpkg-deb --fsys-tarfile "$package_temporary" | tar -tf - \
  | grep -Eq '(^|/)(voice\.json|vocabulary\.json|corrections\.json|adaptive-corrections\.json|data-collection\.json|interaction\.json|microphone-priority\.json|output-style\.json)$'; then
  die "Built package unexpectedly owns a private user configuration filename" 2
fi

package_digest=$(sha256sum "$package_temporary")
package_digest=${package_digest%% *}
printf '%s  %s\n' "$package_digest" "$package_name" >"$checksum_temporary"
chmod 0644 "$package_temporary" "$checksum_temporary"

python3 -I "$manifest_helper" move-no-clobber \
  --source "$package_temporary" --destination "$package_path" >/dev/null
package_temporary=""
if ! python3 -I "$manifest_helper" move-no-clobber \
  --source "$checksum_temporary" --destination "$checksum_path" >/dev/null; then
  die "Package published, but checksum destination already exists: $checksum_path"
fi
checksum_temporary=""
printf '%s\n' "$package_path"
