# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository once
the public preview opens. Do not open a public issue containing an API key,
dictated text, recording, credential-bearing log, or a reproducible exploit
against another user.

The repository intentionally remains private while GitHub private reporting
is unavailable. Enabling that feature and publishing a verified confidential
contact route are release gates; the project must not be made public first and
fixed later.

## Supported versions

The project is currently a pre-release prototype. Security fixes apply to the
latest `main` revision; no stable release branch is supported yet.

## Important boundaries

- A Volcengine API key is a secret and must never be committed or bundled.
- Audio recorded during an active dictation is sent to the configured remote
  provider. Cancelling locally cannot retract audio already uploaded.
- Password, PIN, private, stale, and unfocused input contexts must reject voice
  acquisition.
- The IBus engine must never perform network or microphone I/O.
- Late partial or final results must not be redirected to clipboard paste.
