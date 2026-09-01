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
reproducible exploit against another user. GitHub private vulnerability
reporting is enabled for this public preview, so reporters may use
[Report a vulnerability](https://github.com/SidUParis/openVoiceInput_linux/security/advisories/new)
or the private email route above.

Required CI checks protect `main`, and GitHub Secret Scanning with Push
Protection is enabled. These controls reduce accidental exposure but do not
make a provider key safe to share in an issue, pull request, recording, log, or
support message.

## Supported versions

The project is currently a pre-release prototype. Security fixes apply to the
latest `main` revision; no stable release branch is supported yet.

## Important boundaries

- A Volcengine API key is a secret and must never be committed or bundled.
- Audio recorded during an active dictation is sent to the configured remote
  provider. Cancelling locally cannot retract audio already uploaded.
- In caret mode, password, PIN, private, stale, and unfocused input contexts
  must reject voice acquisition. Explicit clipboard mode cannot inspect a
  remote field and must be documented as unsuitable for secrets.
- The IBus engine must never perform network or microphone I/O.
- Late partial or final results must not switch into clipboard delivery, and
  no path may auto-paste.
