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
- Treat personal vocabulary and manual/adaptive correction pairs as private
  text. Adaptive inference is limited to one strict replacement inside the
  authoritative final's anchored IBus span during a five-second same-focus
  lease. Never infer from partials, unrelated text, clipboard, AT-SPI, or global
  key events, and never apply an unbounded local string replacement.
- Outside the explicit local collector, persist only bounded adaptive
  pair/state/support records, not full transcripts, edit streams, document
  context, or audio. Manual pairs take priority; conflicts, unsafe overlaps,
  and cycles must be suppressed, and the provider view remains capped at 50
  pairs.
- Local audio/provider-final collection must remain off by default and require
  an existing user-selected absolute directory. Publish only after the focused
  client accepts the authoritative final. Mark that provider text
  `teacher-unreviewed`; leave `spoken_verbatim` and `preferred_output` null
  until independent review.
- Collection filesystem work must stay outside the audio callback/session lock,
  use bounded memory and a bounded queue, and never block dictation. Disabling
  must prevent unpublished queued/staged publication while retaining already
  published records.
- Do not upload the local dataset, transfer it to Orange, train a model, or
  imply application-level encryption. The selected filesystem is the storage
  visibility/at-rest boundary.

## Focus safety

- Bind every dictation to the engine instance and focus token that started it.
- On focus-out, engine switch, or application exit: clear preedit, cancel the
  utterance/observation ID, and ignore late responses.
- Do not auto-commit a recovered/timeout result after focus changed.
- Disable voice recording for password, PIN, and other private input purposes.
- Treat private-purpose changes, missing surrounding text, and an untrusted
  committed-span anchor as no-learning outcomes.

## Process isolation

- The engine must remain functional if the daemon crashes or the network stalls.
- The daemon runs without authority to synthesize keys or access arbitrary
  application text; it receives only the bounded observation snapshot returned
  by the focused IBus engine.
- D-Bus methods validate caller identity, utterance ID, sizes, and state order.
- Microphone category priority is private, allowlisted, and loaded before each
  dictation. Missing configuration has a documented default; an existing
  invalid/unsafe file fails before audio/provider activity instead of silently
  changing the user's choice. DJI probing is bounded and read-only. Its result
  can affect only eligibility for the daemon's next stream: it must not move a
  playback sink or request/set a system default, and unknown status must not
  promote DJI ahead of a known-working alternative. Host policy may still
  recompute a default after conservative profile recovery. No mid-utterance
  handoff is required or claimed.

## Non-goals for the MVP

- No ambient microphone monitoring.
- No automatic clipboard or selected-text collection.
- No generative LLM rewriting or remote application-context upload.
- No silent provider fallback.
