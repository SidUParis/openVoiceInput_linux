# Open Voice Input Linux security and privacy requirements

## Credentials

- Store API keys in Secret Service/libsecret when available.
- Never place real credentials in Git, examples, command output, crash reports,
  telemetry, or debug logs.
- Mask settings UI values and avoid returning a stored key to the UI process.
- A file fallback must be atomic and mode `0600` inside a mode `0700` directory.

## Audio and text

- Capture and upload audio only after an explicit user action.
- Make the selected remote provider visible in settings.
- `Esc` stops local processing, but documentation must explain that already
  uploaded audio cannot be withdrawn.
- Never log dictated text. Length, timing, protocol status, and redacted request
  identifiers are sufficient for diagnostics.

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
