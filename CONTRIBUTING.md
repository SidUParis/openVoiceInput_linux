# Contributing

Thank you for helping improve openVoiceInput_linux. The project welcomes bug
reports, documentation fixes, accessibility work, packaging help, and small,
well-tested changes.

## Before opening a change

- Do not include API keys, dictated text, recordings, private Rime user data,
  screenshots of sensitive fields, or unredacted logs.
- Keep microphone/network work outside the keyboard-critical IBus process.
- Preserve the focus token, utterance ID, caller identity, revision ordering,
  and final-once checks on every inline-text path.
- Do not make the installer overwrite `~/.config/ibus/rime` or share its live
  user database.
- Record the source and licence of any imported file in `NOTICE.md`.

## Local checks

On Ubuntu, install `python3-gi` and `gir1.2-ibus-1.0`, then run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=engine \
  python3 -m unittest discover -s engine/tests -v
ruff check engine scripts
ruff format --check engine scripts
python3 -m compileall -q engine scripts
bash -n scripts/install-user.sh scripts/uninstall-user.sh
git diff --check
```

Tests must not contact Volcengine, record a microphone, alter the clipboard,
or switch the user's real IBus engine. Network paths use local fakes.

## Reporting recognition errors

Please report the language, expected text, actual text, whether the final
two-pass result differed from the live draft, and relevant non-sensitive
vocabulary. Do not attach the original recording unless you have intentionally
removed private content and explicitly choose to share it.
