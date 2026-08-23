# Roadmap

This roadmap deliberately separates the keyboard-critical path from network
and audio work. A milestone is complete only when keyboard input remains usable
during ASR outages and cancellation/focus tests pass.

## Phase 0 — Repository and legal boundary

- [x] Create the private GitHub repository and initial architecture.
- [x] Select GPL-3.0-only for the project's new original code.
- [ ] Record file-level attribution before importing any upstream code.
- [ ] Decide whether to preserve `ibus-rime` history or import a minimal fork.

## Phase 1 — Rime-capable IBus engine

- [ ] Build and package an engine derived from `ibus-rime`.
- [ ] Use isolated system and user Rime data directories.
- [ ] Package a pinned, checksummed Rime Ice release without install-time
  downloads.
- [ ] Provide an explicit one-time import without sharing the live ibus-rime
  database.
- [ ] Match normal ibus-rime typing, candidates, properties, and deployment.
- [ ] Add engine focus/session identifiers for voice requests.

## Phase 2 — Voice daemon

- [ ] Extract the tested Volcengine v3 client behind a provider interface.
- [ ] Capture 16 kHz mono PCM without blocking the engine.
- [ ] Implement `bigmodel_async` live hypotheses and two-pass final results.
- [ ] Enable DDC, punctuation, ITN, and sentence segmentation by default.
- [ ] Expose a versioned D-Bus API and utterance-based cancellation.

## Phase 3 — Inline voice input

- [ ] Start and stop from a configurable shortcut and microphone indicator.
- [ ] Render partial ASR as IBus preedit at the caret.
- [ ] Commit only the authoritative two-pass final result.
- [ ] Cancel on `Esc`, focus-out, engine switch, daemon restart, or timeout.
- [ ] Reject voice start during an active Rime composition in the MVP.
- [ ] Disable voice in password/PIN/private input contexts.

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
