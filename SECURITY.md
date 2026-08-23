# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do
not open a public issue containing an API key, dictated text, recording,
credential-bearing log, or a reproducible exploit against another user.

Until private reporting is enabled, contact the repository owner through the
private contact method listed on their GitHub profile and provide only the
minimum redacted detail needed to establish contact.

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
