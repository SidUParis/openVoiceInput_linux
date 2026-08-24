# Security policy

## Reporting a vulnerability

Send the initial private report to `sunxusidney@gmail.com` with the suggested
subject `[Open Voice Input Linux Security] <short summary>`. Include the
affected version or commit, impact, and minimal redacted reproduction steps.
Ordinary email is not end-to-end encrypted: do not send an API key, recording,
raw dictated text or transcript, credential-bearing log, or another user's
private data. If sensitive evidence is essential, first ask the maintainer to
agree on a safer transfer method.

Do not open a public issue containing private vulnerability details or a
reproducible exploit against another user. After GitHub private vulnerability
reporting is enabled for the public preview, reporters may use either that
GitHub channel or the private email route above.

GitHub does not offer private vulnerability reporting while this repository is
private. The email route above is the current non-public reporting channel;
GitHub private vulnerability reporting remains a separate public-transition
gate. Required CI checks must already protect `main` while the repository is
private. During the controlled public transition, maintainers must enable
GitHub private reporting, verify it, and only then announce or invite use of
the preview.

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
