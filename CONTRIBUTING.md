# Contributing

Thank you for helping improve Open Voice Input Linux. The project welcomes bug
reports, documentation fixes, accessibility work, packaging help, and small,
well-tested changes.

## Before opening a change

- Do not include API keys, dictated text, recordings, private vocabulary or
  correction files, private Rime user data, screenshots of sensitive fields,
  or unredacted logs.
- Keep microphone/network work outside the keyboard-critical IBus process.
- Preserve the focus token, utterance ID, caller identity, revision ordering,
  and final-once checks on every inline-text path.
- Do not make the installer overwrite `~/.config/ibus/rime` or share its live
  user database.
- Record the source and licence of any imported file in `NOTICE.md`.

## Local checks

On Ubuntu, install `dbus-daemon`, `desktop-file-utils`, `python3-gi`,
`gir1.2-ibus-1.0`, `gir1.2-gtk-4.0`, `ibus`, `ibus-gtk4`, `imagemagick`,
`libportaudio2`, `python3-venv`, `util-linux`, `x11-utils`, `xdotool`, and
`xvfb`. In an isolated environment with the test tools installed, run the same
boundaries as CI:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=engine \
  python3 -m unittest discover -s engine/tests -v
PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s scripts/tests -v
python3 -I scripts/run_isolated_preedit_smoke.py
xvfb-run -a env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=voice \
  python3 -m pytest -q -p no:cacheprovider voice/tests
ruff check engine scripts voice
ruff format --check engine scripts voice
python3 -m compileall -q engine scripts voice/murmur_voice
python3 scripts/scan_repository_secrets.py
bash -n packaging/murmur-voice-daemon \
  packaging/open-voice-input-settings \
  scripts/build-preview-bundle.sh scripts/install-user.sh \
  scripts/uninstall-user.sh
git diff --check
```

The `preview-bundle` CI job additionally builds from `git archive`, verifies
the checksum manifest, installs the complete wheelhouse with `--no-index`, and
runs the mock install/upgrade/uninstall lifecycle. The bundled Python wheels
and project build backend are version- and hash-locked, and the bundle carries
a verified SBOM. Do not extend that claim to the Ubuntu packages, CPython patch
release, pip, or other host toolchain inputs that the preview does not pin.

Tests must not contact Volcengine, record a microphone, alter the clipboard,
or switch the user's real IBus engine. The isolated preedit smoke creates its
own X server, D-Bus, IBus daemon and temporary HOME, and uses only fixed
synthetic Chinese text. Network paths use local fakes.

## Reporting recognition errors

Please report the language, error category, and whether the final two-pass
result differed from the live draft. Include expected/actual text only as a
fully synthetic or deliberately redacted minimal example that contains no
private content. Do not upload the original recording: speech audio is hard to
anonymize reliably. If a real transcript is essential to diagnosis, wait for
the documented confidential reporting route instead of opening a public issue.
