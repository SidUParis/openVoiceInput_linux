# Open-source readiness checklist

The repository remains private until every required item below is complete.
“Ready” means a public technical preview, not a claim that the permanent
combined librime engine is already finished.

## Required before opening the repository

- [x] Repository licence and upstream boundary documented.
- [x] No real API key, recording, transcript, or Rime user database tracked.
- [x] Security policy, contribution guide, issue form, and pull-request checks.
- [x] CI for engine tests, formatting, compilation, and shell syntax.
- [ ] Public product name and repository URLs are consistent.
- [ ] Voice daemon source is self-contained in this repository with MIT
      attribution for migrated Doubao Murmur files.
- [ ] Settings require only the user's own Volcengine API key after the user
      has enabled the matching service in their account.
- [ ] Clean Ubuntu install, upgrade, and uninstall are reproducible.
- [ ] No installer writes to or locks `~/.config/ibus/rime`.
- [ ] Live partial, final-once, cancel, focus-loss, private-field, daemon-loss,
      and API failure paths have automated tests.
- [ ] README clearly labels the temporary engine-switch limitation.
- [ ] Dependency and licence inventory has been reviewed.
- [ ] A fresh-machine smoke test has been recorded without publishing secrets.

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
