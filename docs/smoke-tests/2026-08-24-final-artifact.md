# Final-artifact user lifecycle smoke — 2026-08-24

This record covers implementation commit
`57181db5bd992a8497ac841c050c00f5ea36b212` and the exact Ubuntu 24.04
x86_64/CPython 3.12 preview produced by successful GitHub Actions run
[`32682997623`](https://github.com/SidUParis/openVoiceInput_linux/actions/runs/32682997623).
It used the maintainer's existing Ubuntu 24.04 GNOME/X11 user, not a fresh VM,
so it does not close the fresh-graphical-machine release gate.

The archive SHA256 was:

```text
069d01097bdb38e0f5416a7e17e3af1eb882289aaac0e466689378f785358ee8
```

The CI archive and an independent clean local build from the same commit were
byte-identical. The archive passed its outer checksum, canonical extraction,
exact source/ref comparison, wheel/SBOM/lock verification, isolated no-index
installation, `pip check`, entry-point checks, and mock lifecycle verification.
A repeat build refused to overwrite the existing output and left its bytes
unchanged.

## Real same-user lifecycle

Before the test, the active IBus engine was `libpinyin`; the existing engine
service was active/enabled; the standalone voice service was inactive/disabled;
no standalone provider configuration existed; and the compatibility Doubao
Murmur Flatpak was running.

The unpacked CI artifact then completed this real sequence:

1. Upgrade from ownership-manifest v1 to v2.
2. Verify manifest v2 against the installed engine/voice tree, both systemd
   user units, the settings desktop entry, and its SVG icon.
3. Upgrade v2 to v2 and verify the replacement again.
4. Validate both installed user units with `systemd-analyze --user verify`, run
   `pip check` in the managed environment, and start the installed GTK settings
   entry point under Xvfb without creating provider configuration.
5. Uninstall and confirm that the project root, units, desktop entry, and icon
   were removed, while the exact prior IBus engine was restored.
6. Reinstall from the same artifact and repeat the manifest, service, IBus,
   managed-environment, and settings checks.
7. Compare a metadata-only digest of every existing entry under
   `~/.config/ibus/rime` before and after the entire sequence. It was unchanged;
   no Rime file content was read.

The final installed state was rechecked after the lifecycle: manifest version
2 validates, the engine service is active/enabled, the voice service is
inactive/disabled, `libpinyin` remains active, the standalone configuration
directory remains absent, the compatibility Flatpak is still running, and no
installer transaction directory remains. The final desktop entry uses exactly
`Settings;Accessibility;`, avoiding the validator warning fixed by this commit.

No API key, microphone audio, transcript, personal vocabulary, correction
pair, or Rime file content was read, recorded, or placed in command output.

## Gate that remains open

Repeat the complete configure/start/inline-partial/two-pass-final/crash/
upgrade/uninstall sequence in a fresh graphical Ubuntu user or VM, using a
newly rotated key entered only in that guest. The test must cover representative
GTK, Qt, Chromium/Electron, terminal, and Wayland contexts without preserving
audio or dictated text.
