# Privacy and trust boundaries

Open Voice Input Linux keeps ordinary Rime keyboard input local. Voice input
is different: during an explicitly started dictation, 16 kHz mono PCM audio is
streamed to the Volcengine BigModel ASR service configured by the user.

## Remote data and billing

- The user must activate the matching speech service in their own Volcengine
  project and provide their own API key. Quota and charges belong to that
  account; the project does not bundle or share a key.
- Cancelling stops capture and prevents local commit, but cannot retract audio
  already uploaded before cancellation.
- Provider-side storage, retention, regional processing, and account policy
  are governed by the user's Volcengine agreement and configuration.
- The current standalone daemon sends audio and reviewed ASR options. Its
  optional personal vocabulary sends only terms the user explicitly adds; it
  never reads the clipboard, typing history, document, transcript history, or
  Rime user database automatically.

## Local secrets and text

- `murmur-voice-daemon configure` uses a masked TTY prompt and never accepts a
  key as a command-line argument.
- The fallback key-only file is atomically written under
  `$XDG_CONFIG_HOME/murmur-ime/voice.json` with directory mode `0700` and file
  mode `0600`. It is rejected if it is a symlink, foreign-owned, public, too
  large, or contains fields other than `api_key`.
- The optional vocabulary is stored separately as `vocabulary.json` under the
  same private directory, with the same ownership and permission checks.
- API keys, transcripts, vocabulary, and remote payloads are not written to
  logs. Status and errors use fixed codes.
- Live text travels over the user's session D-Bus to the focused IBus engine.
  It does not use clipboard paste in the primary path.

## Input-context safety

The engine refuses acquisition for password, PIN, private, fake, unfocused,
and non-preedit contexts. Focus loss clears preedit. Sender identity,
utterance ID, focus state, and strictly increasing revision protect the rest
of the session; late callbacks from an earlier recording are discarded.

The session bus and private control socket are per-user boundaries, not a
sandbox between applications owned by the same Unix account. The first D-Bus
`Acquire` call therefore trusts other processes running as that same user.
Users who require isolation between same-UID applications should not run the
developer preview. A future hardened design can require an explicit short-lived
capability armed by the user before the daemon may acquire preedit.

## Resource limits

One dictation stops normally at 600 seconds and waits at most 20 seconds for
the provider's authoritative two-pass final. Pending unsent PCM is bounded to
10 seconds. An exceeded network queue cancels the session rather than growing
memory indefinitely; compressed provider responses also have a decoded-size
limit.
