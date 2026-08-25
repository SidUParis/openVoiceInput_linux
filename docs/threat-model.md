# Threat model for the 0.x preview

Review basis: the implementation and documentation prepared together for the
`v0.1.0-alpha.1` preview, reviewed on 2026-08-26. This document covers the
current temporary IBus-engine switch, standalone voice daemon, and bounded
microphone-route recovery. It does not claim that the future combined librime
engine has been implemented or reviewed.

## Security and privacy objectives

Open Voice Input Linux is designed to preserve these properties:

1. A voice result is committed only to the focused, explicitly acquired IBus
   context that started that utterance.
2. Password, PIN, private, fake, unfocused, and non-preedit contexts cannot
   acquire voice input.
3. Cancelling, changing focus, losing the daemon, or receiving a stale result
   never redirects text through a clipboard fallback.
4. The provider key, vocabulary, corrections, audio, and recognised text are
   not bundled or written to logs.
5. Network delay or failure cannot block ordinary keyboard input, grow an
   unbounded audio queue, or leave the machine permanently on the temporary
   voice-only engine.
6. Installation, upgrade, and uninstall modify only project-owned paths and
   never read, write, lock, or remove the user's stock Rime database.
7. An offline preview accepted by the verifier contains the exact committed
   source payload and the exact locked wheelhouse described by its manifest
   and SBOM.

## Assets and trust boundaries

Sensitive assets are the provider API key, microphone audio, live/final text,
explicit vocabulary and correction pairs, the focused input context, the
previous IBus engine, the selected audio source/profile, and the user's
existing Rime data.

The current boundaries are:

- The IBus engine is keyboard-critical and performs no microphone or network
  work.
- The voice daemon owns audio capture and the provider connection. It sends
  partial/final events to the engine over the user's session D-Bus.
- PulseAudio/PipeWire and PortAudio are host trust boundaries. The daemon may
  add input to one unambiguous output-only ALSA profile and bind its own stream
  to one verified physical source. It never directly changes mute, volume, or
  calls `set-default-source`; the host audio policy may nevertheless recompute
  its global default when a card profile is activated.
- The private Unix control socket and session D-Bus are boundaries between
  Unix users, not between applications running as the same user.
- Volcengine is a remote processor selected by the user. Standard TLS protects
  transport, but the provider can necessarily process audio and any explicit
  vocabulary/corrections included with a request.
- The installer trusts the local operating system, Python interpreter,
  systemd user manager, IBus, and a preview archive that has passed the
  repository verifier. It does not elevate privileges.

## Threats and implemented controls

### Late or misdirected text

Every preedit session is bound to the D-Bus sender, focused engine instance,
focus token, utterance ID, and a strictly increasing revision. Final is
accepted once. Focus-out, reset, disable, caller disappearance, cancel, and
daemon loss clear preedit and invalidate the session. The application never
falls back to clipboard paste after a preedit acquisition succeeds.

Evidence: `engine/murmur_ime_engine/session.py`,
`engine/murmur_ime_engine/registry.py`, `voice/murmur_voice/preedit.py`, and
the engine session/registry plus voice preedit/session tests.

### Sensitive input fields

The engine checks IBus purpose, private hints, focus, client identity, and
preedit capability before acquisition. Password and PIN contexts are denied.
Text is bounded by Unicode code points and encoded byte size before display.

Evidence: `engine/murmur_ime_engine/policy.py` and
`engine/tests/test_policy.py`.

### Credential disclosure or unsafe local files

The CLI uses a masked prompt and never accepts a key in argv. The GTK window
does not preload the stored key and clears the entry after a save attempt.
Key, vocabulary, and correction files use a private `0700` directory and
`0600` regular files, reject links/foreign ownership/public modes/oversize or
unknown fields, and are replaced atomically. Key removal requires the managed
voice service to be explicitly inactive. Logs contain fixed status/error
classes rather than secret or dictated values.

Evidence: `voice/murmur_voice/config.py`,
`voice/murmur_voice/settings_controller.py`, and their tests.

### Resource exhaustion and network stalls

One utterance stops normally at 600 seconds, emits a warning at 540 seconds,
and waits at most 20 seconds for the authoritative final. Pending PCM is
bounded to 10 seconds; overflow cancels instead of blocking the audio callback
or growing memory indefinitely. Provider frames and decoded payloads are
bounded. Old generations and late worker callbacks cannot enter a new
utterance.

Evidence: `voice/murmur_voice/session.py`,
`voice/murmur_voice/volcengine.py`, and their boundary tests.

### Stale or ambiguous microphone routing

Each explicit start re-enumerates the audio route before opening the provider
connection. A real non-monitor default is kept. A monitor default is treated
as stale; a real source is selected only when exactly one is available, or
when exactly one ALSA card has exactly one same-output input-capable profile
and exactly one source bound to that card. PulseAudio 15 and PipeWire expose
different card identities, so
numeric card IDs, exact device names, and exact ALSA-card/bus-path pairs are
handled as separate strict schemas; conflicting, partial, or multiple matches
are rejected.

The selected source is applied only to the daemon's PortAudio `pulse` stream.
`PULSE_SOURCE` is changed under a process-wide lock only while that stream is
constructed and started, then its previous presence/value is restored even on
failure. The provider is not contacted when preflight or focus validation
fails; a later stream-open failure aborts and closes both boundaries. A failed
profile transition is rolled back only while the live profile and previously
observed default still match the transaction; unrecognised concurrent state is
preserved rather than overwritten.

Evidence: `voice/murmur_voice/audio.py`, `voice/murmur_voice/session.py`, and
their audio-route, deadline, focus-loss, and failure-order tests.

### Crash or engine-restoration failure

The previous IBus engine is recorded in a private, validated runtime state
before switching. Normal final/cancel restores it. Startup and systemd
`ExecStopPost` retry residual restoration after a crash. A stale state is
cleared without overriding a different real engine that the user selected.
Install and uninstall retain recovery material and keep services stopped when
restoration cannot be proven.

Evidence: `voice/murmur_voice/engine_restore.py`, the engine-restore tests,
and installer rollback/process-race tests.

### Package substitution and destructive lifecycle races

The preview has a canonical exact-file SHA256 manifest and deterministic
CycloneDX wheelhouse SBOM. The verifier checks wheel `RECORD`, dependency
markers, locked versions and whole-wheel hashes, project-package bytes against
the bundled source, entry points, licences, target OS/architecture/Python, and
a real isolated no-index installation. Build outputs publish through
fsync plus atomic no-clobber rename.

The user installer re-verifies a private staged wheelhouse, runs isolated pip,
records exact project-owned paths and hashes, locks both XDG roots, and uses
no-clobber commits plus identity-checked rollback. It refuses foreign or
changed files and live foreground daemons. Cleanup failure is reported as a
failure and retains named recovery locations.

Evidence: `scripts/generate_preview_sbom.py`,
`scripts/verify_preview_bundle.py`, `scripts/install_manifest.py`, lifecycle
scripts/tests, and the `preview-bundle` CI job.

### Rime-data corruption

The transition preview does not vendor Rime data and its installer has no path
under `~/.config/ibus/rime`. It records/restores the exact previous IBus engine
instead of assuming Rime. Automated lifecycle tests use isolated fake homes;
the documented same-machine smoke test also compared a metadata-only Rime
fingerprint before and after install/upgrade/uninstall/reinstall.

## Accepted preview risks

- A malicious process running as the same Unix user can race to acquire the
  session D-Bus preedit service or interact with the user's private control
  socket. There is not yet a short-lived user-presence capability. Do not use
  the preview when mutually untrusted same-UID applications are in scope.
- Volcengine receives microphone audio and explicit request vocabulary or
  correction pairs. Cancellation cannot retract bytes already uploaded, and
  provider retention/region/account policy is outside this project.
- The fallback key store is a private plaintext file rather than Secret
  Service. It protects against other local users under normal Unix permission
  assumptions, not against malware or a compromised account.
- TLS uses the platform trust store without certificate pinning. A compromised
  host trust store or provider account is outside the current threat model.
- SHA256 manifests and reproducible bytes prove integrity and consistency, not
  publisher identity. A signed/attested public release is still a release
  gate.
- The transition engine cannot inspect stock Rime composition state. Starting
  voice with unfinished composition may discard it, so the user must first
  commit or cancel the visible composition. Automatic refusal requires the
  future combined librime-capable engine. A desktop-global shortcut/indicator
  and broad Wayland/application matrix also remain future work.
- System Python, distribution packages, IBus, GTK, PortAudio, systemd, and the
  kernel are trusted host components and are not covered by the wheelhouse
  SBOM.
- Recovering an output-only ALSA card leaves the unique same-output duplex
  profile active after success so the selected microphone continues to exist.
  This is a global per-user audio-profile change. PulseAudio provides no
  compare-and-swap operation, so failed recovery uses identity re-checks and
  best-effort rollback; concurrent or unreadable state is deliberately left
  unchanged. Activating that profile can also cause host policy modules to
  recompute the global default even though the daemon never requests a default
  change. The short process-environment window used to open the explicit Pulse
  stream is serialized inside the daemon but is not an OS-level capability
  boundary against hostile same-process code.
- The Pulse transaction has bounded command and rollback budgets, but native
  PortAudio device enumeration and stream opening do not expose a portable
  cancellation API. A broken host audio driver can therefore delay a start
  beyond the normal control-response window; restarting the user service is
  the current recovery. The IBus engine remains a separate process and normal
  keyboard input is unaffected.

## Review result and re-review triggers

No unresolved issue found in this review justifies publishing a known unsafe
default, but the accepted risks above keep the software explicitly labelled a
developer preview. A fresh graphical machine with physical-microphone,
provider, and representative-application coverage remains an explicit alpha
validation gap and must be disclosed in the release notes. Rotation of every
development provider key plus a verified signed tag remain pre-publication
gates; immutable release status must be verified immediately after publication.

Re-review is required before adding a provider, changing D-Bus/control-socket
ownership, reading application context or clipboard data, adding automatic
learning, changing secret storage, vendoring Rime data, installing with
privileges, or replacing the temporary engine with the combined librime
engine.
