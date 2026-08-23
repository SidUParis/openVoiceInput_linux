# Open Voice Input Linux roadmap

This roadmap deliberately separates the keyboard-critical path from network
and audio work. A milestone is complete only when keyboard input remains usable
during ASR outages and cancellation/focus tests pass.

The public project name is **Open Voice Input Linux**, with the canonical
repository `SidUParis/openVoiceInput_linux`. Historical `murmur-*` and
`org.murmur.*` runtime names remain unchanged throughout the 0.x line as
compatibility ABI for existing installations and the verified sidecar bridge.

## Phase 0 — Repository and legal boundary

- [x] Create the private GitHub repository and initial architecture.
- [x] Select GPL-3.0-only for the project's new original code.
- [x] Record file-level attribution before importing any upstream code.
- [ ] Decide whether to preserve `ibus-rime` history or import a minimal fork.

## Phase 0.5 — Inline-preedit transition prototype

- [x] Implement the pure Python `murmur-voice` IBus engine.
- [x] Dynamically register it without root access or restarting IBus.
- [x] Render cumulative partials as caret-local IBus preedit.
- [x] Commit one final result exactly once without clipboard injection.
- [x] Bind sessions to focus, caller, utterance, and increasing revisions.
- [x] Reject password, PIN, private, fake, and non-preedit input contexts.
- [x] Add deterministic GTK demo and a 13-test engine suite.
- [x] Verify the self-contained voice bridge with temporary
  `rime → murmur-voice → rime` switching for each recording.
- [x] Add optional per-user install/uninstall helpers and systemd user units
  for both the engine and standalone daemon.

## Phase 1 — Production Rime-capable IBus engine

- [ ] Build and package an engine derived from `ibus-rime`.
- [ ] Use isolated system and user Rime data directories.
- [ ] Package a pinned, checksummed Rime Ice release without install-time
  downloads.
- [ ] Provide an explicit one-time import without sharing the live ibus-rime
  database.
- [ ] Match normal ibus-rime typing, candidates, properties, and deployment.
- [ ] Add engine focus/session identifiers for voice requests.

The Python prototype does not satisfy this phase: IBus permits only one engine
per input context, so stock Rime cannot compose Chinese while `murmur-voice` is
selected.

## Phase 2 — Standalone transition voice daemon

- [x] Migrate the tested Volcengine v3 client into a self-contained package.
- [x] Capture 16 kHz mono PCM without blocking the engine.
- [x] Implement `bigmodel_async` live hypotheses and two-pass final results.
- [x] Enable DDC, punctuation, ITN, and sentence segmentation by default.
- [x] Add bounded local control, recording limits, stale-session rejection,
  and a reversible systemd user-service installation.
- [ ] Generalise the provider boundary and expose a production daemon D-Bus
  API; the transition implementation uses the Preedit1 bridge and local Unix
  control socket.

## Phase 3 — Inline voice input in the combined engine

- [ ] Start and stop from a configurable shortcut and microphone indicator.
- [ ] Render partial ASR as IBus preedit at the caret.
- [ ] Commit only the authoritative two-pass final result.
- [ ] Cancel on `Esc`, focus-out, engine switch, daemon restart, or timeout.
- [ ] Reject voice start during an active Rime composition in the MVP.
- [ ] Disable voice in password/PIN/private input contexts.

The corresponding preedit, final-commit, focus, revision, and private-field
rules are already exercised by the transition prototype; this phase ports
them into the librime-capable production engine and removes engine switching.

## Phase 4 — Settings and packaging

- [ ] GTK settings window with masked API key and connection test.
- [ ] Store secrets in Secret Service; provide a documented `0600` fallback.
- [ ] Debian/Ubuntu package and user D-Bus activation.
- [ ] Arch package and reproducible CI builds.
- [ ] Migration helper from Doubao Murmur without copying secrets.

## Phase 5 — Public preview

- [ ] Threat-model and privacy review.
- [ ] License and attribution audit.
- [ ] End-to-end tests for GTK, Qt, Chromium/Electron, terminals, and Wayland.
- [ ] Chinese documentation, screenshots, demo video, and contribution guide.
- [ ] Make the repository public and publish the first signed preview release.
