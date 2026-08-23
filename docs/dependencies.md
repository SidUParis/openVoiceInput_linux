# Dependency and licence inventory

This developer preview vendors no third-party Python package or Rime data.
Distribution packages must resolve system dependencies through the target
distribution and preserve their own licence files.

## Direct runtime dependencies

| Component | Declared/tested version | Purpose | Licence metadata |
|---|---|---|---|
| Python | `>=3.11` | Engine and daemon runtime | PSF licence; system package |
| PyGObject / Gio | Ubuntu system package; tested `3.48.2` | IBus and session D-Bus bindings | GNU LGPL metadata; system package |
| IBus GI bindings | Ubuntu system package | Focus-bound preedit and commit | External system package; not vendored |
| `sounddevice` | `>=0.4.6,<1`; tested `0.5.6` | PortAudio microphone capture | MIT metadata |
| `websockets` | `>=13,<18`; tested `17.0.1` | Volcengine WebSocket transport | BSD-3-Clause metadata |
| PortAudio | Distribution library | Native audio backend for sounddevice | External system package; not vendored |

The voice wheel is GPL-3.0-only and contains its `LICENSE` plus the complete
Doubao Murmur MIT notice in `NOTICE.md`. Adapted source boundaries are listed
in the repository-level [NOTICE](../NOTICE.md).

## Test and build tools

- `pytest` 9.0.2, MIT metadata;
- `ruff` 0.15.4 in CI, MIT metadata;
- `setuptools>=77`, MIT metadata.

CI builds the wheel, installs it into a fresh virtual environment, runs
`pip check`, checks the console entry point, and asserts that both licence
files are present in the archive.

## Release policy

The ranges above are appropriate for source development, not a reproducible
binary release. Debian/Ubuntu packaging must pin the distribution package
versions it builds against, record build inputs and hashes, and produce an
SBOM before the first signed release. Rime Ice remains external until a pinned
version and checksum can be packaged without writing to the user's stock Rime
database.
