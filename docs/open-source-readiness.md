# Open-source readiness checklist

The repository remains private until every required item below is complete.
“Ready” means a public technical preview, not a claim that the permanent
combined librime engine is already finished.

## Required before opening the repository

- [x] Repository licence and upstream boundary documented.
- [x] No real API key, recording, transcript, or Rime user database tracked.
- [x] Security policy, contribution guide, issue form, and pull-request checks.
- [x] CI for engine, voice, mock installer lifecycle, unit rendering,
      formatting, compilation, shell syntax, and provider-aware current/history
      credential scanning.
- [x] Public product name and repository URLs are consistent.
- [x] Voice daemon source is self-contained in this repository with MIT
      attribution for migrated Doubao Murmur files.
- [x] Configuration requires only the user's own Volcengine API key after the user
      has enabled the matching service in their account.
- [ ] Clean Ubuntu install, upgrade, and uninstall are reproducible. Offline
      mock lifecycle coverage exists, but a fresh graphical-user smoke test is
      still required.
- [ ] No installer writes to or locks `~/.config/ibus/rime`. The mock contract
      enforces this; retain the gate until the fresh-machine smoke test.
- [x] Live partial, final-once, cancel, focus-loss, private-field, daemon-loss,
      and API failure paths have automated tests.
- [x] README clearly labels the temporary engine-switch limitation.
- [x] Direct dependency and licence inventory has been reviewed and recorded
      in `docs/license-audit.md`; the transition threat model and accepted
      risks are recorded in `docs/threat-model.md`.
- [x] CI builds a clean-source Ubuntu x86_64 preview with a complete offline
      Python wheelhouse, exact SHA256 manifest, and unpacked mock-install test.
- [x] The Ubuntu 24.04/CPython 3.12 preview pins and hashes every bundled
      runtime wheel and its build backend, emits a deterministic CycloneDX 1.5
      SBOM, and independently recomputes that inventory during verification.
- [x] A bounded native GTK4 settings window stores only private key,
      vocabulary, and explicit-correction files and never preloads or logs the
      provider key.
- [x] The settings window has a validated desktop-menu entry and local SVG
      icon covered by the transactional ownership manifest and uninstall.
- [ ] A fresh-machine smoke test has been recorded without publishing secrets.
      A same-machine Ubuntu 24.04 install/upgrade/uninstall/reinstall smoke is
      recorded for the exact final artifact in
      `docs/smoke-tests/2026-08-24-final-artifact.md`; a fresh graphical user/VM
      remains required.
- [ ] All provider keys used during pre-release development have been rotated.
- [ ] `main` requires the `security`, `engine`, `voice`, and `preview-bundle`
      checks. This can and should be enabled while the repository is private.
- [ ] GitHub private vulnerability reporting is enabled and verified during
      the controlled public transition; GitHub does not expose it for this
      private personal repository.
- [ ] The release procedure in `docs/release-process.md` has been completed
      with a verified signing identity and an immutable signed preview.

## Release gates

1. `git grep` and history scans find no credential material.
2. CI passes from a clean checkout.
3. Installation requires no root-written user secrets.
4. The API key is masked, stored with least privilege, and never echoed.
5. Network failure leaves normal Rime keyboard input usable.
6. Changing focus or cancelling never commits a late result.
7. Uninstall restores the prior input method and removes only project-owned
   files.
8. Public docs state that audio is sent to Volcengine and billed under the
   user's account.

## Deferred after the first preview

- Permanent `ibus-rime`/librime-derived combined engine.
- Wayland desktop-global shortcut standardisation.
- Additional ASR providers.
- Distribution-native Debian and Arch repositories.
- Optional managed hotword-table tooling.
