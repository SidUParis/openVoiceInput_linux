# Licence and attribution audit

Audit basis: source tree and verified Ubuntu 24.04 preview at implementation
commit `57181db`, reviewed on 2026-08-24. This is an engineering inventory, not
legal advice.

Alpha.2 delta reviewed on 2026-08-30: the DJI link probe dynamically loads the
host's `libusb-1.0`; it adds no bundled wheel or copied library. The Ubuntu
`libusb-1.0-0` package records LGPL-2.1-or-later terms and remains an external system
component in this inventory.

Alpha.3 adds policy, settings, and tests around the existing audio boundary; it
adds no third-party source, model, wheel, system library, or bundled binary.

## Result

The current source and preview have a documented licence path for every
bundled code component reviewed. No Rime Ice data, ibus-rime, librime, audio,
model, recording, or other binary asset is vendored. The audit found no
unattributed third-party source in the current bundle.

## Project code

- New Open Voice Input Linux code is distributed under `GPL-3.0-only`. The
  complete text is in the repository `LICENSE`, the voice package `LICENSE`,
  and the built project wheel's licence directory.
- Project metadata declares `GPL-3.0-only`, the canonical repository, issue,
  documentation, and security-policy URLs.
- The local SVG settings icon is original project artwork and has no external
  binary or font dependency. The settings screenshot is a project-generated
  rendering from an empty temporary profile; it contains no user content or
  third-party media.

## Adapted Doubao Murmur code

`audio.py`, `volcengine.py`, and `preedit.py` contain adapted portions from the
MIT-licensed Doubao Murmur checkout identified in `NOTICE.md`. Each file has a
file-level SPDX licence identifier, the upstream copyright statement, the
modification statement, and a pointer to the preserved MIT terms.

The repository and built project wheel include the complete `NOTICE.md`; the
offline verifier requires both `LICENSE` and `NOTICE.md` to match the verified
source. This engineering review found no additional upstream restriction that
conflicts with distributing the modified project under `GPL-3.0-only` while
preserving the MIT notice. This conclusion must be revisited if the upstream
boundary or bundled material changes.

## Bundled runtime wheels

The offline preview contains exactly these third-party runtime wheels:

| Component | Locked version | Licence recorded in verified wheel metadata |
|---|---:|---|
| sounddevice | 0.5.6 | MIT |
| websockets | 17.0.1 | BSD-3-Clause |
| cffi | 2.1.1 | MIT-0 |
| pycparser | 3.0 | BSD-3-Clause |

Their exact filenames and SHA256 values are pinned in
`packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt`. The generated
CycloneDX SBOM reads licence metadata only after verifying every wheel's
`RECORD` file and includes the whole-wheel hashes and dependency graph. The
installer refuses any extra, missing, renamed, or changed wheel.

`setuptools==83.0.0` is a pinned MIT build input. It is downloaded into an
ephemeral isolated build environment and is not shipped in the runtime
wheelhouse.

## External system components

Python, PyGObject, GTK, Gio, IBus, PortAudio, `libusb-1.0`, systemd, and
standard host tools are supplied by Ubuntu and are not copied into the preview.
Consequently they are correctly described as external prerequisites rather
than bundled SBOM components. A future Debian/Arch package must inventory the
exact distribution payload and preserve the distribution's corresponding
notices.

ibus-rime, librime, and Rime Ice are architectural references only. Their code
and data are not present in this tree. Vendoring or linking them later requires
a new audit, pinned source/version/hash, preserved upstream notices, and an
updated package-level SBOM.

## Automated and release checks

- CI confirms the project wheel contains `LICENSE` and `NOTICE.md` and contains
  no cache/bytecode files.
- The preview verifier binds the installed project package bytes, entry points,
  top-level package, and licence files to the committed source snapshot.
- `SHA256SUMS` covers the SBOM, wheelhouse, source, licences, and all other
  bundle files; the outer checksum covers the final archive.
- Current-file, index, and reachable-history secret scanning is separate from
  this audit and remains a required CI job.

## Required re-audit events

Repeat this audit before vendoring Rime or other data, adding a Python/runtime
dependency, changing the adapted upstream files, adding artwork/media/fonts,
shipping a distribution-native package, changing the project licence, or
publishing the first signed release.
