# Open-source readiness checklist

This repository is a public early technical preview for community testing and
feedback. The unchecked items below remain release or production-readiness
gates; opening the source does not claim that the permanent combined librime
engine or a supported distribution package is already finished.

## Public-preview readiness and remaining gates

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
- [x] A zero-download isolated smoke test exercises a real private IBus daemon,
      dynamically registered engine and GTK4 entry under Xvfb: partials remain
      uncommitted at the caret and the final is committed exactly once, without
      reading a key, microphone, provider service or the desktop's real IBus
      state.
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
      remains required. The isolated real-IBus smoke closes the caret-rendering
      gap but intentionally does not claim a fresh OS, logind/systemd user
      session, microphone, provider or application-matrix result.
- [ ] All provider keys used during pre-release development have been rotated.
- [x] `main` requires the `security`, `engine`, `voice`, and `preview-bundle`
      checks; Actions are limited to GitHub-owned, full-SHA-pinned actions.
- [x] A private security and conduct-reporting route is documented
      in `SECURITY.md` and `CODE_OF_CONDUCT.md`: `sunxusidney@gmail.com`, with
      separate suggested subject prefixes for the two report types and an
      explicit prohibition on sending keys, recordings, or raw dictated text.
- [x] GitHub private vulnerability reporting is enabled and verified for the
      public repository; Secret Scanning and Push Protection are also enabled.
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
