# Open Voice Input Linux security and privacy requirements

## Credentials

- The transition preview stores the API key only in its validated atomic
  `0600` file inside a `0700` directory. Secret Service/libsecret is the
  preferred future store once its migration and deletion lifecycle is
  implemented and tested; the current UI must not imply that it is already in
  use.
- Never place real credentials in Git, examples, command output, crash reports,
  telemetry, or debug logs.
- Mask settings UI values and avoid returning a stored key to the UI process.
- A file fallback must be atomic and mode `0600` inside a mode `0700` directory.
- Local key removal must require an inactive managed service, explicit
  confirmation, and the same owner/type/symlink/permission validation. It does
  not replace provider-side credential revocation.
- CI scans the index, worktree, and complete reachable history for supported
  Volcengine, AWS, GitHub, OpenAI, and PEM credential shapes without printing
  matched values. It is a release guard, not a substitute for key rotation.

## Audio and text

- Capture and upload audio only after an explicit user action.
- Make the selected remote provider visible in settings.
- `Esc` stops local processing, but documentation must explain that already
  uploaded audio cannot be withdrawn.
- Never log dictated text. Length, timing, protocol status, and redacted request
  identifiers are sufficient for diagnostics.
- Treat personal vocabulary and correction pairs as private text: load only
  explicit user entries from private files, never infer them from transcripts,
  and never apply an unbounded local string replacement.

## Focus safety

- Bind every dictation to the engine instance and focus token that started it.
- On focus-out, engine switch, or application exit: clear preedit, cancel the
  utterance ID, and ignore late responses.
- Do not auto-commit a recovered/timeout result after focus changed.
- Disable voice recording for password, PIN, and other private input purposes.

## Process isolation

- The engine must remain functional if the daemon crashes or the network stalls.
- The daemon runs without authority to synthesize keys or access arbitrary
  application text.
- D-Bus methods validate caller identity, utterance ID, sizes, and state order.

## Non-goals for the MVP

- No ambient microphone monitoring.
- No automatic clipboard or selected-text collection.
- No generative LLM rewriting or remote application-context upload.
- No silent provider fallback.
