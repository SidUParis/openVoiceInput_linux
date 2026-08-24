# Dependency and licence inventory

The source tree vendors no third-party Python package or Rime data. The
target-specific offline preview archive includes four third-party runtime
wheels whose versions and exact wheel hashes are recorded in
`packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt`.
Distribution packages must resolve system dependencies through the target
distribution and preserve their own licence files.

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

The voice wheel is GPL-3.0-only and contains its `LICENSE` plus the complete
Doubao Murmur MIT notice in `NOTICE.md`. Adapted source boundaries are listed
in the repository-level [NOTICE](../NOTICE.md).

The offline preview installer requires the bundle's `BUNDLE-INFO`, canonical
SBOM, target lock, and exact wheelhouse, and uses `pip --no-index`. A plain
source checkout does not contain that release metadata: use the explicit
`--allow-network` developer mode there, or build a complete preview bundle.
Distribution packages must provide pinned dependencies themselves and must not
use that development escape hatch.

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
as bundled CycloneDX components. A future Debian package or signed system image
must produce its own broader installation-level SBOM.

## Test and build tools

- `pytest` 9.0.2, MIT metadata;
- `ruff` 0.15.4 in CI, MIT metadata;
- `setuptools>=77` for general source builds; the preview project wheel uses
  the separately pinned and hashed `setuptools==82.0.1` build backend, MIT
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
Debian/Ubuntu packaging must pin the distribution package versions it builds
against, record all build inputs and hashes, and extend the wheelhouse SBOM to
cover that system payload before the first signed release. Rime Ice remains
external until a pinned version and checksum can be packaged without writing
to the user's stock Rime database.
