# Offline Ubuntu 24.04 x86_64 / CPython 3.12 preview bundle

Each successful push to `main` publishes a short-lived Ubuntu 24.04 x86_64 /
CPython 3.12 preview artifact. Pull requests build and verify the same payload
but cannot upload an installable artifact. Before installing, confirm the
artifact's full commit SHA belongs to this repository's trusted `main`
history; checksums alone prove integrity, not publisher identity.
The archive contains only the committed source tree, a project wheel, every
Python runtime-dependency wheel, `BUNDLE-INFO`, a deterministic CycloneDX 1.5
`SBOM.cdx.json`, and a complete `SHA256SUMS`. Ignored files, local
configuration, credentials, bytecode, and Git metadata are excluded by
construction.

The bundle does not contain Ubuntu `.deb` packages. Install the documented
system prerequisites (`ibus`, `gir1.2-ibus-1.0`, `gir1.2-gtk-4.0`,
`python3-gi`, `python3-venv`, `libportaudio2`, optional `libusb-1.0-0` for the
DJI Mic Mini 2 link probe, `pulseaudio-utils` for `pactl`, and `util-linux` for
`flock`)
before moving the bundle to an offline machine. This preview intentionally refuses to build on any target
other than Ubuntu 24.04, x86_64 and 64-bit CPython 3.12; the target machine
must match those tags too.

After downloading both files from the same CI artifact:

```bash
sha256sum --check openVoiceInput_linux-preview-*.tar.gz.sha256
tar -xzf openVoiceInput_linux-preview-*.tar.gz
cd openVoiceInput_linux-preview-*/
sha256sum --check SHA256SUMS
./scripts/install-user.sh
```

Keep this verified extracted directory: the technical preview intentionally
does not copy `scripts/uninstall-user.sh` into the managed install root.

The default installer discovers the bundled `./wheelhouse`, disables package
indexes, and passes every wheel as an explicit path with
`--ignore-installed --no-deps`. This forces the locked runtime wheels into the
venv even when older compatible copies are visible through
`--system-site-packages`; `--ignore-installed` also prevents pip from trying to
remove those host copies. Before venv creation, the installer independently
rejects any extra, missing, renamed, or hash-mismatched wheel against the
target lock and canonical SBOM. It copies only that allowlist into a private
staging directory, validates the copy again, and invokes pip in isolated mode
with Python path overrides cleared. Host `PIP_DRY_RUN`, `PIP_TARGET`,
`PYTHONPATH`, and similar settings therefore cannot skip or redirect the
offline install. The verifier also compares every `murmur_voice` package byte,
console entry point, top-level declaration, compatibility tag, and bundled
licence against the Git-verified project source; replacing the project wheel
and regenerating its RECORD, SBOM, and checksums is not sufficient. An explicit
equivalent is:

```bash
./scripts/install-user.sh --wheelhouse "$PWD/wheelhouse"
```

CI verifies both forms after unpacking with isolated systemd and IBus mocks,
then performs a real install of the Python wheel set in a fresh virtual
environment with package indexes disabled. It confirms each installed
distribution has the SBOM version, lives in the venv's own `site-packages`, is
the module Python imports ahead of any system copy, and records the expected
staged-wheel path and SHA256 in its PEP 610 `direct_url.json`. The real user
installer performs the same installed-provenance check before committing its
staged runtime. The SHA256 files detect
accidental corruption; they are not a cryptographic statement of publisher
identity. Verification independently recomputes the SBOM from every wheel's
`METADATA` and `RECORD`, rejects missing or surplus wheels, checks the
target-specific `Requires-Dist` closure, requires the runtime wheel versions
and hashes to exactly match the fixed Ubuntu 24.04 x86_64 / CPython 3.12 lock,
matches whole-wheel SHA-256 values and licences, and requires the SBOM itself
to be covered by `SHA256SUMS`.

The runtime lock admits exactly the selected `sounddevice`, `websockets`,
`cffi`, and `pycparser` wheels by SHA256. Pip runs with `--require-hashes` and
`--only-binary=:all:`, so a different wheel, an unpinned transitive dependency,
or a source distribution fails the build. The project wheel is built
separately from committed source with a pinned, hashed setuptools backend,
`SOURCE_DATE_EPOCH`, isolated Python and pip modes, and a fixed file-creation
mask.

`./scripts/install-user.sh --allow-network` is an explicit source-development
escape hatch. It retains normal pip resolution and may therefore reuse a
compatible dependency exposed by the host; it is not the locked offline
preview installation path and carries no lock/SBOM provenance claim.

Maintainers can build the clean-source snapshot only from a clean committed
tree on the exact supported build target. Building the wheelhouse may use the
network; installing it does not:

```bash
python3 -m venv .venv
PREVIEW_PYTHON=.venv/bin/python \
  ./scripts/build-preview-bundle.sh --output-dir dist
```

For build-script integration, the fixed, offline generator interface is shown
below. It must run after the wheelhouse and `BUNDLE-INFO` are complete and
before `SHA256SUMS` is written:

```bash
"$build_python" "$bundle_dir/scripts/generate_preview_sbom.py" \
  --bundle-root "$bundle_dir" \
  --output "$bundle_dir/SBOM.cdx.json"
```

The output has no wall-clock timestamp or random identifier. Its UUID serial is
derived deterministically from the source commit, target, full Python version,
wheel filenames, and wheel hashes, so regenerating from identical inputs
produces identical bytes.

The required CI job passes the exact `$GITHUB_SHA` to both builder and verifier,
builds the archive twice into separate empty directories on the same ephemeral
runner, requires each directory to contain only one archive plus its matching
checksum, and compares both files byte for byte. This catches nondeterministic
timestamps, ordering, identifiers, and surplus release assets. It is a
same-runner determinism check, not a claim that two independently provisioned
operating systems are bit-for-bit identical.

These controls lock the Python wheel payload, not the operating system. The
archive does not vendor or pin Ubuntu `.deb` packages, the CPython patch
release, pip, glibc, PortAudio, GTK, or IBus. Consequently this preview is not
a claim that two arbitrary Ubuntu installations produce or contain identical
system-level bits. Those external system dependencies and build tools are
intentionally outside the bundled wheelhouse SBOM; a future signed distribution
package needs a separately locked OS build environment and installation-level
SBOM.
