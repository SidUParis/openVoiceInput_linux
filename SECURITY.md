# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting after it is enabled for the
public preview. Do not open a public issue containing an API key, dictated
text, recording, credential-bearing log, or a reproducible exploit against
another user.

GitHub does not offer private vulnerability reporting while this repository is
private. Before the transition, the maintainers must publish and verify a
separate confidential contact route. Required CI checks must already protect
`main` while the repository is private. During the controlled public
transition, maintainers must enable GitHub private reporting, verify it, and
only then announce or invite use of the preview. Until a contact route is
chosen, this repository intentionally remains private.

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
