# Dependency and licence inventory

The source tree vendors no third-party Python package or Rime data. The
target-specific offline preview archive includes four third-party runtime
wheels whose versions and exact wheel hashes are recorded in
`packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt`.
The standalone Ubuntu 24.04 `.deb` safely unpacks those same locked wheels into
its private root-owned import tree and declares the remaining system
dependencies through package metadata. It preserves the bundled licences and
notices; it does not vendor or pin Ubuntu packages.

## Direct runtime dependencies

| Component | Declared/tested version | Purpose | Licence metadata |
|---|---|---|---|
| Python | `>=3.11` | Engine and daemon runtime | PSF licence; system package |
| PyGObject / Gio / GTK4 | Ubuntu system package; tested PyGObject `3.48.2` and GTK `4.14.5` | IBus, session D-Bus, and native settings UI | GNU LGPL metadata; system package |
| IBus GI bindings | Ubuntu system package | Focus-bound preedit and commit | External system package; not vendored |
| `sounddevice` | Source range `>=0.4.6,<1`; preview lock `0.5.6` | PortAudio microphone capture | MIT metadata |
| `websockets` | Source range `>=13,<18`; preview lock `17.0.1` | Volcengine WebSocket transport | BSD-3-Clause metadata |
| `cffi` | Preview lock `2.1.1` (transitive) | Native PortAudio binding used by sounddevice | MIT-0 metadata |
| `pycparser` | Preview lock `3.0` (transitive) | cffi parser dependency on CPython | BSD-3-Clause metadata |
| PortAudio | Distribution library | Native audio backend for sounddevice | External system package; not vendored |
| `pactl` (`pulseaudio-utils` on Ubuntu) | Optional PulseAudio-compatible system command | Per-recording source discovery and conservative profile recovery; PortAudio fallback when absent | External system package; not vendored |
| `libusb-1.0` (`libusb-1.0-0` on Ubuntu) | Optional distribution library, loaded dynamically | Bounded DJI Mic Mini 2 transmitter-link probe before a new dictation; unknown never promotes DJI ahead of a known alternative | LGPL-2.1-or-later; system package, not vendored |

The DJI probe never changes an audio route itself. `libusb` is used only to
read the receiver's bounded vendor status before opening a new daemon stream;
an absent, busy, inaccessible, or unrecognised device yields unknown. That
status never promotes DJI ahead of a known alternative; an already-default
unique DJI remains only a final continuity fallback when no non-DJI or
recoverable input can be selected. There is no mid-utterance source handoff.

The voice wheel is GPL-3.0-only and contains its `LICENSE` plus the complete
Doubao Murmur MIT notice in `NOTICE.md`. Adapted source boundaries are listed
in the repository-level [NOTICE](../NOTICE.md).

The offline preview installer requires the bundle's `BUNDLE-INFO`, canonical
SBOM, target lock, and exact wheelhouse, and uses `pip --no-index`. A plain
source checkout does not contain that release metadata: use the explicit
`--allow-network` developer mode there, or build a complete preview bundle.
The `.deb` builder consumes the same fixed runtime lock and never uses that
development escape hatch.

Offline installation passes every wheelhouse wheel as an explicit path with
`--ignore-installed --no-deps`. Thus a compatible but older `sounddevice`,
`websockets`, `cffi`, or `pycparser` visible through
`--system-site-packages` cannot satisfy and bypass the bundled wheel. Pip
places the selected wheel in the virtual environment without trying to remove
the host copy, and the venv-local package shadows that copy. Before invoking
pip, the installer requires the wheelhouse file set, versions, and hashes to
exactly match both the target lock and canonical SBOM; extra, missing, or
changed wheels are rejected. The explicit
`--allow-network` developer mode intentionally keeps normal pip resolution:
it may reuse compatible host packages and does not provide the preview
lock/SBOM provenance guarantee.

The installer copies only the validated allowlist into its private staging
tree, validates the copy again, and runs pip in isolated mode so host `PIP_*`,
`PYTHONPATH`, and `PYTHONHOME` settings cannot redirect or suppress the
install. Before committing the staged runtime it rechecks every installed
distribution's venv-local location, version, imported module ownership, and
PEP 610 source-wheel path and SHA256 against the lock/SBOM inventory.

The managed launcher and systemd unit set `PYTHONNOUSERSITE=1`: the virtual
environment can see Ubuntu's GI bindings without importing unrelated Python
packages from the user's `~/.local` site directory. The bundled Python runtime
wheels are nevertheless installed in the venv's own `site-packages` ahead of
the Ubuntu system-package paths.

## Preview SBOM scope

Every offline preview contains `SBOM.cdx.json`, a deterministic CycloneDX 1.5
inventory generated without network access. It identifies the project wheel
and every bundled runtime-dependency wheel by package name, version, Package
URL, whole-wheel SHA-256, and licence metadata. Its dependency graph is rebuilt
from wheel `Requires-Dist` fields after evaluating markers for the target in
`BUNDLE-INFO`. Wheel `METADATA` is trusted only after its `RECORD` hashes and
file set have been checked.

That SBOM deliberately describes **only files in the bundled Python
wheelhouse**. Ubuntu packages and libraries such as Python, IBus, GTK,
PyGObject, PortAudio, and the build/test tools below are prerequisites or build
inputs, not payloads in the preview archive, so they are not falsely presented
as bundled CycloneDX components.

## Standalone `.deb` SBOM scope

The Ubuntu 24.04 `amd64` package contains a second deterministic CycloneDX 1.5
document. Its metadata component identifies `open-voice-input-linux`, the exact
40-character source commit, the Debian package version, and the fixed target.
Its component list identifies the four bundled Python runtime wheels with
their locked versions, Package URLs, and whole-wheel SHA-256 values. Matching
`BUILD-INFO` also records the source epoch and runtime-lock digest.

This package SBOM is deliberately **package scoped, not operating-system
scoped**. It does not claim that Ubuntu's Python, IBus, GTK, PyGObject,
PortAudio, libusb, PulseAudio utilities, systemd, or their transitive packages
are bundled, hashed, or pinned. Those packages remain declared external
dependencies resolved by APT. It is also not a per-file hash manifest for the
application source; the exact commit provenance plus the checksum of the
completed `.deb` cover the released package bytes. A signed repository or
system image would need a separately locked build environment and a broader
installation-level inventory before making an operating-system reproducibility
claim.

## Test and build tools

- `pytest` 9.0.2, MIT metadata;
- `ruff` 0.15.4 in CI, MIT metadata;
- system `Xvfb`, `xdotool`, `x11-utils` (`xwininfo`), ImageMagick (`import`
  and `compare`), `dbus-daemon` (`dbus-run-session`), IBus with `ibus-gtk4`,
  and GTK4 for the isolated real-preedit smoke; these are CI/test inputs, not
  preview payloads;
- `setuptools>=77` for general source builds; the preview project wheel uses
  the separately pinned and hashed `setuptools==83.0.0` build backend, MIT
  metadata. The build-backend wheel is not shipped in the runtime wheelhouse.

CI builds the wheel, installs it into a fresh virtual environment, runs
`pip check`, checks the daemon and GTK settings entry points (the latter under
Xvfb), and asserts that both licence files are present in the archive.

## Release policy

The source ranges above remain appropriate for development. The Ubuntu 24.04
x86_64 / CPython 3.12 preview locks all Python runtime wheels and its
setuptools build backend, supplies `SOURCE_DATE_EPOCH`, and builds under
isolated Python and pip modes with a fixed file-creation mask. This does not
make the whole installation reproducible: Ubuntu system packages, CPython and
pip come from the build/target hosts and are not vendored or pinned by this
archive.
The standalone `.deb` likewise records its exact source commit and locked
Python-wheel inputs, normalizes package timestamps, and is compared byte for
byte when rebuilt on the supported CI runner. That same-runner check does not
pin the Ubuntu build toolchain or imply that independently provisioned hosts
produce identical operating-system bits. A signature on either alpha artifact
does not claim broader operating-system reproducibility. Rime Ice remains
external until a pinned version and checksum can be packaged without writing
to the user's stock Rime database.
