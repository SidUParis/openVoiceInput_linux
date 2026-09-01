# Threat model for the 0.x preview

Review basis: the implementation and documentation published as
`v0.1.0-alpha.2` on 2026-08-30, extended for the configurable microphone-policy
candidate prepared the same day. This document covers the current temporary
IBus-engine switch, standalone voice daemon, per-dictation microphone
selection, adaptive correction, and disabled-by-default local WAV/JSON
collector, including faithful/clean terminal delivery. It does not claim that
the future combined librime engine, Orange
transport, human label-review workflow, or local model training has been
implemented or reviewed.

## Security and privacy objectives

Open Voice Input Linux is designed to preserve these properties:

1. A voice result is committed only to the focused, explicitly acquired IBus
   context that started that utterance.
2. Password, PIN, private, fake, unfocused, and non-preedit contexts cannot
   acquire voice input.
3. Cancelling, changing focus, losing the daemon, or receiving a stale result
   never redirects text through a clipboard fallback.
4. The provider key, vocabulary, manual/adaptive corrections, audio, and
   recognised text are not bundled or written to logs.
5. Network delay or failure cannot block ordinary keyboard input, grow an
   unbounded audio queue, or leave the machine permanently on the temporary
   voice-only engine.
6. Installation, upgrade, and uninstall modify only project-owned paths and
   never read, write, lock, or remove the user's stock Rime database.
7. Adaptive correction observes only one bounded post-commit replacement in
   the same focused context; ambiguous edits and context changes fail closed.
8. An offline preview accepted by the verifier contains the exact committed
   source payload and the exact locked wheelhouse described by its manifest
   and SBOM.
9. Local recording retention is disabled by default. When explicitly enabled,
   only an authoritative final accepted by the focused context can publish a
   bounded, versioned WAV/JSON record; collection failure cannot block
   dictation.
10. DJI transmitter status affects only the daemon's new capture stream and
    never changes a playback sink or requests a system-wide default source.
11. Clean delivery is local, final-only, deletion-only, replayable and bounded.
    A processor failure or invalid result falls back to raw provider text; a
    machine-cleaned span is never used as automatic ASR correction evidence.

## Assets and trust boundaries

Sensitive assets are the provider API key, microphone audio, live/final text,
explicit vocabulary, manual/adaptive correction pairs, microphone priority and
exact-source preferences, private output style, local-collection
consent/destination and published
records, the focused input context and its
bounded surrounding-text snapshot, the previous IBus engine, the selected
audio source/profile, DJI status frames, and the user's existing Rime data.

The current boundaries are:

- The IBus engine is keyboard-critical and performs no microphone or network
  work. For adaptive correction it consumes only IBus surrounding text from
  the already acquired, focused input context; it does not use clipboard,
  AT-SPI, or global keyboard monitoring.
- The voice daemon owns audio capture and the provider connection. It sends
  partial/final events to the engine over the user's session D-Bus. If the user
  selected clean output, the daemon postprocesses only the terminal final with
  a local bounded deletion rule; it adds no LLM or extra network request. If the user
  explicitly enabled collection, it also retains bounded PCM in memory and
  offers an accepted final to an isolated background filesystem writer.
- The selected local or mounted filesystem is a user-chosen trust boundary.
  The collector creates `openvoiceinput-dataset-v1`, validates its marker, and
  atomically publishes complete records. It provides no application-level
  encryption, application-owned upload, authentication, or Orange mounting;
  filesystem and mount policy determine effective visibility and at-rest
  protection. A user-managed SSHFS path therefore adds the remote host and
  network mount to this trust boundary.
- PulseAudio/PipeWire and PortAudio are host trust boundaries. The daemon may
  add input to one unambiguous output-only ALSA profile and bind its own stream
  to one verified physical source. It never directly changes mute, volume, or
  calls `set-default-source`; the host audio policy may nevertheless recompute
  its global default when a card profile is activated.
- `libusb` and the DJI receiver's vendor status interface are host/device trust
  boundaries for one bounded link-state probe before stream creation. Unknown,
  inaccessible, busy, or malformed status is not treated as proof of online or
  offline state.
- The private Unix control socket and session D-Bus are boundaries between
  Unix users, not between applications running as the same user.
- The selected online ASR service is a remote processor. Standard TLS protects
  transport, but that provider can necessarily process audio and any explicit
  vocabulary/corrections included with a request. Volcengine is the default
  and the only real-key-validated path in this release; Qwen and OpenAI remain
  experimental.
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

### Adaptive-correction pollution or contextual overreach

The observation lease begins only after one authoritative final commit and
lasts at most five seconds. The engine first anchors that committed span from a
newer IBus surrounding-text revision. Finish is accepted only from the same
D-Bus sender and utterance while the same input context remains focused and
non-private. Focus-out, disable, owner disappearance, timeout, missing
surrounding-text support, or failure to anchor the span yields no learned pair.
GTK can reset the input context as part of an ordinary same-focus edit, so an
observation-time reset only requests a fresh surrounding snapshot; the focus
token and anchored-span checks remain authoritative. A reset before Final still
invalidates the voice preedit session.

The daemon accepts exactly one replacement inside the anchored span. Pure
insertions/deletions, multiple edits, broad polishing, outside-span changes,
a selection still active at Finish, unchanged values, invalid controls, and either side over 64
Unicode characters are rejected. The changed block is additionally limited to
three lexical tokens per side and a conservative similarity floor. A
one-character source must expand through unchanged adjacent lexical context;
case-only spelling corrections are permitted. The persisted adaptive ledger
contains only the bounded pair, state, and support count, not a separate transcript,
surrounding snapshot, or edit stream. Manual corrections take priority.
Normalized source conflicts, source/canonical overlaps, provider cascades, and
cycles are suppressed; the combined provider view remains capped at 50 pairs. This constrains
accidental fact pollution and prevents the ledger from silently overriding an
explicit user mapping.

The observation path does not read clipboard contents, AT-SPI, global keyboard
events, microphone audio, other windows, or the Rime database. It transiently
processes the bounded current-field IBus snapshot and does not persist it. It
does not apply a local rewrite to text already committed by IBus. The next
toggle can finish the lease early; configuration is loaded only for the next
provider request.

Evidence: `engine/murmur_ime_engine/ibus_engine.py`,
`voice/murmur_voice/adaptive_correction.py`,
`voice/murmur_voice/adaptive_store.py`, and their boundary tests.

### Credential disclosure or unsafe local files

The CLI uses a masked prompt and never accepts a key in argv. The GTK window
does not preload the stored key and clears the entry after a save attempt.
Key, vocabulary, manual-correction, adaptive-correction, output-style, and
microphone-priority files use a private `0700` directory and `0600` regular
files, reject links/foreign
ownership/public modes/oversize or unknown fields, and are replaced
atomically. Key removal requires the managed voice service to be explicitly
inactive. Logs contain fixed status/error classes rather than secret or
dictated values.

The separate `data-collection.json` is also private and atomic; a missing file
means disabled. Enabling requires an existing absolute directory and
initializes a versioned dataset marker below it. The collector applies private
modes where the selected filesystem supports them, but does not claim that
Unix modes provide confidentiality on every mount and does not add encryption.
Neither configured paths, audio, provider text, nor USB status frames enter
logs.

Evidence: `voice/murmur_voice/config.py`,
`voice/murmur_voice/settings_controller.py`, and their tests.

### Resource exhaustion and network stalls

One utterance stops normally at 600 seconds, emits a warning at 540 seconds,
and waits at most 20 seconds for the authoritative final. Pending PCM is
bounded to 10 seconds; overflow cancels instead of blocking the audio callback
or growing memory indefinitely. Provider frames and decoded payloads are
bounded. Old generations and late worker callbacks cannot enter a new
utterance.

The terminal cleaner receives at most 4,096 codepoints and permits at most 64
strict original-coordinate deletions. It cannot insert or substitute content.
Oversize, excessive, malformed, non-replayable, exception, and all-content
removal cases return the raw provider final rather than blocking delivery.

Opted-in collection retains at most one 600-second PCM utterance in its active
recorder and uses a bounded two-record background queue. The audio callback
only appends bounded immutable chunks; WAV encoding, fsync, and rename run in
the writer thread. A full queue or writer failure drops that optional record
with a fixed error status instead of blocking capture, final delivery, or the
keyboard-critical engine.

Shutdown sets the writer stop event and grants a bounded 10-second drain inside
systemd's 30-second total stop budget. It does not wait indefinitely for a
selected filesystem. There is no fallback local spool.

Evidence: `voice/murmur_voice/session.py`,
`voice/murmur_voice/volcengine.py`, and their boundary tests.

### Optional local dataset publication

Collection is absent/disabled by default and reloaded at the start of every
utterance. A recorder is created only for an explicit enabled choice. It copies
the same successfully submitted 16 kHz mono signed 16-bit chunks into bounded
memory, then discards them on cancel, error, missing/empty final, or rejection
of the provider final by the focused IBus context. A record is not queued until
that final is accepted.

The background writer first creates a complete private staging directory under
`openvoiceinput-dataset-v1/.pending`, including WAV and JSON hashes, then uses
one atomic rename into `utterances/<utterance_id>`. The JSON identifies
`provider_final` as `teacher-unreviewed`; it leaves both `spoken_verbatim` and
`preferred_output` null and unreviewed. Schema v3 separately records actual
machine-derived delivery and replayable deletion metadata while retaining raw
provider text. This prevents an ASR result or cleaned output from being
silently presented as a human-verified acoustic label or preferred text.

After the unchanged two-file utterance pair is durable, the writer publishes a
separate schema-v2 `usage/<utterance_id>.json` summary with no transcript. Its
count is explicitly based on delivered text, while readers retain v1 support. The GTK
dashboard reads only these bounded private summaries on a worker thread. It
does not enumerate utterance directories, read record labels/audio, or create a
missing index while merely viewing statistics. Hidden interrupted summary
staging is ignored.

Configuration save and final publication share a short lock and the writer
rechecks the dataset identity and consent before rename. Once disabling or
redirecting collection returns, an older queued/staged record cannot become
published. Already published records are deliberately retained; uninstall
also preserves the private setting and all user-selected datasets. The current
feature implements no record deletion, review workflow, application-owned
Orange authentication/mounting or resumable transfer, cloud-dataset upload,
model training, or application-level encryption. User-mounted storage remains
inside the filesystem trust boundary described above; see
[remote-dataset-storage.md](remote-dataset-storage.md).
If the selected filesystem stalls or disappears during staging, best-effort
cleanup may remove or leave the hidden staging directory and the unpublished
record may be lost; an already atomically published record is not rolled back.

Evidence: `voice/murmur_voice/data_collection.py`,
`voice/murmur_voice/session.py`, `voice/murmur_voice/settings_controller.py`,
and their collection/session/settings tests.

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

Each start loads a private, complete category order and re-enumerates sources.
Missing configuration uses `DJI > headset > other external > built-in`; an
existing invalid or unsafe file fails before preedit, provider, USB, profile,
or microphone activity. Within each category, an exact saved source, then the
current default, then a unique candidate can resolve it. Ambiguity falls through
instead of being guessed.

When exactly one DJI Mic Mini 2 source is enumerated, a bounded read-only
vendor-status probe distinguishes a linked transmitter from a receiver that
remains registered while silent. Proven online makes DJI eligible at its saved
position. Proven offline excludes it. Unknown is never promoted ahead of a
known alternative; an already-default unique DJI can remain only as a final
continuity path when no non-DJI or recoverable source can be selected. The USB
frame decoder is size/count/time bounded, and frame/device content is neither
logged nor persisted. The stream is not handed off during an utterance; link
and device changes take effect only on the next start.

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
before switching. Normal final starts a correction-observation lease of at most
five seconds; its completion, the next toggle, cancel, or failure restores the
exact previous engine. Focus loss invalidates the observation immediately, but
without a reverse focus signal the daemon may restore only at the lease
deadline. Startup and systemd `ExecStopPost` retry residual restoration after a
crash. A stale state is cleared without overriding a different real engine that
the user selected. Install and uninstall retain recovery material and keep
services stopped when restoration cannot be proven.

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
- The selected online ASR provider receives microphone audio and any explicit
  request vocabulary or correction context supported by that provider.
  Cancellation cannot retract bytes already uploaded, and provider billing,
  retention, region, and account policy are outside this project.
- Enabling local collection deliberately creates sensitive audio/text records.
  The selected filesystem or mount controls who can read, back up, or replicate
  them; the application supplies no static encryption. `provider_final` and
  machine-derived `delivery` are unreviewed and must not be treated as gold or
  distillation-ready
  merely because the pair was published atomically.
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
- After final commit, the voice-only engine remains selected during the
  adaptive observation for at most five seconds. Direct keys pass through to
  the application, but stock Rime or another previous IBus engine cannot
  compose until restoration. The next toggle shortens this interval. This is a
  usability limitation of the transition architecture, not a permanent
  combined-engine design.
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
- The DJI probe depends on the receiver's undocumented vendor status framing
  and exclusive access to its USB interface. Busy, inaccessible, absent, or
  unrecognised devices yield unknown. Unknown never promotes DJI ahead of a
  known alternative; an already-default unique DJI is only a final continuity
  fallback when no non-DJI or recoverable input exists. There is no
  mid-utterance live source handoff, so changing transmitter state while
  speaking may leave that utterance on its already opened source.

## Review result and re-review triggers

No unresolved issue found in this review justifies publishing a known unsafe
default, but the accepted risks above keep the software explicitly labelled a
developer preview. A fresh graphical machine with physical-microphone,
provider, and representative-application coverage remains an explicit alpha
validation gap and must be disclosed in the release notes. Rotation of every
development provider key plus a verified signed tag remain pre-publication
gates; immutable release status must be verified immediately after publication.

Re-review is required before adding a provider, changing D-Bus/control-socket
ownership, reading application context or clipboard data, changing collection
consent/schema/publication or adding upload/deletion/review/model-training
behavior, broadening adaptive observation beyond the anchored IBus span,
changing conflict/overlap/cycle policy, changing secret storage, vendoring
Rime data, installing with privileges, or replacing the temporary engine with
the combined librime engine.
