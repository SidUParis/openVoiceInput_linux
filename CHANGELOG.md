# Changelog

All notable user-visible changes to Open Voice Input Linux will be recorded
here. The project has not published a stable release yet.

## [Unreleased]

## [0.1.0-alpha.8] - 2026-09-01

### Added

- A private, hot-loaded `output-target.json` now selects the existing native
  IBus caret target or an explicit RDP-compatible clipboard target. Missing
  configuration remains `caret`; the target is frozen for one utterance and a
  save affects only the next start.
- Clipboard target mode bypasses IBus acquisition and live preedit, retains
  partial hypotheses only in bounded memory, and writes only the authoritative
  terminal final. It never sends `Ctrl+V` or simulated keys: the user confirms
  the remote field and pastes manually.
- The GTK settings application adds a Chinese-first remote-desktop page with
  persistent armed, copied-history, unavailable, and copy-failure status
  messages plus explicit clipboard/privacy warnings.

### Changed

- New opted-in `record.json` files advance to schema v4. Raw provider and human
  label roles remain unchanged; `delivery.target` records the frozen `caret`
  or `clipboard` destination alongside the existing machine-derived delivery.
  Existing v1/v2/v3 records remain immutable and readable.
- Clipboard delivery skips surrounding-text adaptive extraction with the
  content-free reason `clipboard-output-no-surrounding-text`; explicit
  review-last continues to start from the raw provider final.
- Debian packages install both reviewed clipboard helpers for the supported
  graphical environments, while source installs keep the helpers optional so
  the default caret path is unaffected.

### Privacy and compatibility

- Clipboard mode is default-off and final-only. The helper receives transcript
  bytes on standard input—not arguments, environment, or logs—and is accepted
  only when it is a fixed root-owned executable paired with a live local
  X11/Wayland Unix socket. Invalid configuration or unavailable helpers fail
  before microphone/provider startup.
- Provider text is bounded to the same 4,096-codepoint/16 KiB limit used by the
  native caret path. Clipboard/write failure, cancellation, missing final, or
  oversized/malformed provider output publishes neither a successful record
  nor review state.
- `PrivateTmp=yes` remains enabled; only `/tmp/.X11-unix` is exposed read-only
  inside the voice service namespace when present. The clipboard target config
  is user-private, uninstall-preserved, and explicitly forbidden from public
  preview/Debian artifacts.

## [0.1.0-alpha.7] - 2026-09-01

### Added

- Volcengine streaming results are now assembled from timestamped
  `utterances[]` segments. Authoritative second-pass segments marked
  `definite=true` replace overlapping first-pass text and remain available
  across later frames, while bounded `result.text` remains the compatibility
  fallback until a definite segment arrives. A mixed malformed structured
  frame is rejected as a whole rather than partially truncating retained text.
- A private, hot-loaded `output-style.json` selects faithful delivery or a
  conservative clean-expression mode. Faithful delivery remains the default
  whenever the configuration is missing.
  Clean mode leaves streaming partials raw and applies only a bounded,
  deterministic, local deletion-only processor to the authoritative final; it
  makes no LLM or extra network request and falls back to raw on every unsafe
  or failed result.
- The cloud-recognition settings page exposes both modes and explains that a
  save affects the next utterance. Review-last shows raw provider text and the
  delivered result as separate read-only values while spoken-verbatim editing
  always starts from raw provider text.

### Changed

- Opted-in `record.json` advances to schema v3. Raw `provider_final` and the
  two null human-review labels retain their meanings; a separate
  `machine-derived-unreviewed` delivery object records mode,
  processor/version, outcome, and replayable original-coordinate deletions.
  Existing v1/v2 records are never rewritten.
- Content-free usage summaries advance to schema v2 and explicitly count the
  text actually delivered. Dashboard readers continue accepting schema-v1
  summaries with their original meaning.
- If clean delivery changes the committed text by removing content, the daemon
  immediately consumes the IBus observation and records
  `postprocessed-output-not-safe-for-asr-learning` without calling adaptive
  extraction. Unchanged or raw-fallback output keeps the existing observation
  behavior.

### Privacy and compatibility

- `output-style.json` uses the existing user-owned `0700` directory, `0600`
  file, bounded strict schema, and atomic-write rules. Source installers and
  Debian/systemd launch paths pass the intended config location; uninstall
  retains it. Preview, Debian and CI gates reject both `output-style.json` and
  the previously omitted `interaction.json` from public artifacts.

## [0.1.0-alpha.6] - 2026-09-01

### Added

- New opted-in dataset records distinguish the microphone selected by policy
  from the actual Pulse source-output observed during capture. They retain only
  category, a privacy-safe non-unique fingerprint, selection/link provenance,
  and a bounded route-transition list—never raw source names, USB serials,
  Bluetooth addresses, or custom hardware labels.
- The background dataset writer adds numeric whole-record/first-second PCM
  clipping, RMS/peak, DC-offset, and zero-fraction evidence for later review.
  It neither filters nor changes recordings and never runs in the startup or
  real-time audio callback path.
- `open-voice-input-settings --review-last` provides an explicit correction
  path for applications that cannot expose trustworthy IBus surrounding text.
  The daemon retains only the latest accepted provider final in memory for at
  most ten minutes and serves it over a host-only private runtime socket; no
  review transcript is placed in process arguments, logs, or a persistent
  review file.
- A review is bound to the still-current utterance ID and is consumed exactly
  once after the daemon updates the adaptive ledger. When collection is
  enabled, the same bounded result is queued as that utterance's append-only
  training-feedback sidecar; the UI distinguishes disabled, enqueue failure,
  and queued-but-not-yet-durable states.
- The adaptive page now distinguishes explicit vocabulary, explicit manual
  corrections, effective adaptive rules, and the exact combined correction
  count compiled for the next provider request. Confirming a candidate
  reloads the stored generation and reports success only when the rule is
  actually present in that bounded provider view.

### Changed

- `record.json` advances to schema v2 while the dataset marker and two-file
  utterance layout remain compatible with existing v1 datasets. Actual route
  state is explicitly `unknown` until observed; read-only route discovery runs
  asynchronously, quickly backs off to five seconds, and never gates audio.
- Applications such as Chromium that report surrounding text as unsupported
  now restore the exact previous IBus engine immediately instead of holding
  the five-second observation lease. A correction can still be submitted
  explicitly through review-last, but the application never reads Chrome's
  later edits automatically, weakens focus checks, or monitors global input.
- Missing `vocabulary.json` and `corrections.json` files now remain the normal
  representation of zero explicit entries. Automatic learning writes only
  the separate private adaptive ledger and does not create empty manual files
  or copy provider output wholesale into them.

### Privacy and data quality

- Microphone provenance is deliberately privacy-safe: it stores a broad
  category, non-unique fingerprint, policy/link evidence, and bounded route
  transitions, not Pulse source names, USB serials, Bluetooth addresses, or
  custom device labels.
- Audio quality evidence is calculated only after an opted-in recording has
  finished. Alpha.6 adds no startup quality gate, warm-up delay, automatic
  rejection, filtering, gain change, or modification of the provider stream;
  the metrics exist so later dataset review can separate useful and damaged
  examples.
- Review-last accepts only an explicit user-edited verbatim statement. It does
  not silently treat filler removal, rewriting, or polished prose as spoken
  ASR ground truth, and it never reads the clipboard, AT-SPI tree, global key
  events, Rime history, or Chrome edits.
- Owner-mapped FUSE filesystems that expose an owner-private regular file as
  `0700` are accepted for feedback validation alongside `0600`; symlinks,
  missing owner read/write permission, foreign ownership, and every
  group/other permission still fail closed.

### Known limitations

- The post-hoc metrics do not decide whether an utterance is suitable for
  training and do not repair already clipped or otherwise damaged audio.
- Automatic same-field correction still depends on an application exposing
  trustworthy IBus surrounding text. Chromium/Electron users must explicitly
  open review-last while its memory-only ten-minute record is still available.
- Explicit vocabulary and manual corrections remain optional user-managed
  hints. Their files may legitimately be absent; learned adaptive pairs live
  in `adaptive-corrections.json` and only effective, conflict-safe pairs reach
  the next provider request.

## [0.1.0-alpha.5] - 2026-08-31

### Added

- The public daemon now exposes configurable `toggle` and `push_to_talk`
  interaction modes. The press/release state machine ignores key repeat,
  cancels accidental short holds, bounds a lost release, clears ownership on
  cancel/error, and safely starts a new utterance from the observation state.
- The native settings window can select the interaction mode and its bounded
  hold/release safety values without hard-coding a physical key. The packaged
  user-service passes the private interaction policy explicitly.
- Adaptive learning v2 captures several independent replacements as inactive
  review candidates, activates one high-confidence replacement immediately,
  persists transcript-free result reasons and counts, and provides explicit
  confirmation plus a cross-application feedback entry in settings.
- Opted-in datasets can receive append-only atomic
  `feedback/<utterance_id>/<event_id>.json` events with bounded correction
  decisions. Every utterance remains strictly `audio.wav + record.json`, the
  base record is never changed, and disabled collection writes no feedback.
- A provider-neutral ASR registry now keeps microphone, IBus, retention, and
  session behavior independent of the cloud transport. Volcengine remains
  the default; reviewed Qwen real-time and OpenAI batch adapters can be chosen
  in settings or the CLI, while MiniMax stays visibly planned rather than
  pretending to expose an undocumented speech-to-text API.
- The home dashboard reports today's and cumulative character, duration, and
  utterance counts from bounded `usage/*.json` summaries. It never opens an
  audio file, `record.json`, or transcript to calculate those counters.

### Changed

- The native GTK4 settings window now uses a Chinese-first, task-oriented
  sidebar with an always-visible status area instead of one long technical
  form. Provider credentials, vocabulary, corrections, microphone preferences,
  optional data retention, and service controls keep their existing safety
  boundaries.
- Public landing pages now describe microphone routing as a user-defined
  preference with per-utterance fallback, rather than presenting one
  maintainer's device order as a product recommendation.
- README visuals and copy now foreground the caret-native workflow and the
  measured lightweight-client boundary. The alpha.4 `.deb` is about 404 KiB,
  reports about 2.7 MiB installed size, and bundles neither Electron nor local
  ASR model weights.
- AppStream metadata now includes the sanitized Chinese settings screenshot
  and the same native, lightweight-client boundary for graphical package
  browsers.
- Shortcut documentation now distinguishes a cross-desktop activation command
  from true key-down/key-up delivery: toggle works with normal GNOME/KDE
  shortcuts, while push-to-talk requires an integration that exposes release.
  The project does not claim a generic Wayland global-release hook or scan all
  input devices.
- Version-1 adaptive ledgers migrate safely to a five-state version-2 schema
  (`candidate`, `active`, `conflicted`, `suspended`, `archived`). Manual rules,
  conflict/cycle suppression, and the provider's bounded correction view remain
  authoritative.

### Build and quality

- Exact-commit Debian CI now rejects packages larger than 5 MiB or an
  `Installed-Size` above 10 MiB, so future UI work cannot silently replace the
  lightweight native client with a large runtime.

### Known limitations

- Qwen and OpenAI transports have comprehensive fake-protocol tests but have
  not been exercised with real user keys in this release; MiniMax is not a
  selectable backend. Volcengine remains the only physically validated
  provider path on the maintainer workstation.
- Generic desktop activation shortcuts support toggle mode. Push-to-talk needs
  a desktop, keyboard, or helper that emits distinct press and release events;
  the application does not claim a universal Wayland release hook.
- Automatic correction still depends on the focused application exposing a
  trustworthy post-commit edit. When it cannot, settings offers an explicit
  provider-text / preferred-text fallback instead of reading the clipboard or
  globally logging keys.
- Dashboard counters begin with newly published usage summaries and remain
  unavailable while collection is disabled or its mounted directory is
  offline. Legacy transcript-bearing records are deliberately not scanned to
  backfill statistics.

## [0.1.0-alpha.4] - 2026-08-30

### Added

- A standalone Ubuntu 24.04 `amd64` `.deb` with root-owned application code,
  global launchers, package-installed systemd user units, desktop/AppStream
  metadata, and an explicitly opt-in voice service. The microphone-free IBus
  engine remains attached to each graphical user session.
- An offline, exact-commit package builder which consumes the existing
  hash-locked runtime wheelhouse, records source provenance, emits a
  deterministic package-scoped CycloneDX SBOM, and produces a matching
  `.deb.sha256` file.
- A Chinese-first project landing page with English secondary copy, a
  reproducible synthetic interaction demo, social-preview artwork, an honest
  compatibility-report form, and launch/press guidance.

### Changed

- Preview releases now publish and independently verify four assets: the
  source/offline `.tar.gz`, its checksum, the standalone Ubuntu `.deb`, and
  its checksum.
- The packaged commands live in `/usr/bin`, while application modules and
  locked Python dependencies live below
  `/usr/lib/open-voice-input-linux/python`. Private settings and any selected
  dataset remain per-user files outside package ownership.

### Security and privacy

- Package installation refuses a higher-precedence legacy source-preview
  installation instead of silently leaving a desktop user on older code. The
  check tests only fixed installation pathnames; users migrate with the
  trusted source-preview uninstaller, which preserves private configuration
  and external datasets.
- Package payloads and maintainer scripts do not package, read, rewrite, or
  delete provider keys, correction memory, microphone policy, collection
  settings, recordings, or transcripts. A voice service already enabled by
  its user is restarted on upgrade and then reads that user's configuration in
  the normal daemon process.

### Known limitations

- The `.deb` is an Ubuntu 24.04 `amd64` alpha artifact, not a signed APT
  repository or a broad Debian/Ubuntu support claim. Physical microphone,
  provider, graphical-login, and representative application validation remain
  release gates.
- The package does not provide a built-in global shortcut. Users must bind a
  desktop shortcut to `murmur-voice-daemon toggle`; the separate compatibility
  controller is not bundled.
- Recognition still uses the user's Volcengine account. There is no local ASR
  backend or model training in this release.
- Adaptive corrections do not retroactively rewrite collected `record.json`
  files. Collected `provider_final` remains an unreviewed teacher label, with
  `spoken_verbatim` and `preferred_output` left null until a future review
  workflow supplies them.

## [0.1.0-alpha.3] - 2026-08-30

### Added

- A private, versioned microphone-priority policy and native settings controls
  that let users order DJI, headset, other external, and built-in microphone
  categories. The recommended default is `DJI > headset > other external >
  built-in`.
- Metadata-aware source classification for USB/Bluetooth headsets, 3.5 mm
  headset ports, other external inputs, built-in PCI/platform microphones, and
  the DJI Mic Mini 2 receiver.

### Changed

- Every new dictation reloads the saved priority, re-enumerates inputs, and
  falls through unavailable or unresolved categories. Within a category, an
  exact saved source, then the current system default, then a unique candidate
  can resolve it. A running utterance keeps its source until it ends.
- DJI online status now makes the receiver eligible at the user's saved
  position instead of unconditionally overriding every other microphone.
  Offline excludes it; unknown does not promote it ahead of known-working
  alternatives.

### Security and privacy

- A missing microphone policy uses the documented default without writing a
  file. An existing malformed, unsafe, or unsupported policy fails the next
  dictation start with `microphone-policy-invalid` before preedit acquisition,
  provider construction, USB probing, profile mutation, or microphone capture.
- Selection remains scoped to the daemon's new Pulse stream. It never requests
  a playback-sink, system-default, mute, volume, or mid-utterance route change.

### Known limitations

- Bluetooth A2DP playback alone does not expose a microphone. This release uses
  an existing HSP/HFP input when one is already active but does not switch the
  headset's global Bluetooth profile or reduce playback quality automatically.
- Device-category classification uses PulseAudio/PipeWire metadata. An
  unlabelled external device may fall into `other external`; optional exact
  source preferences remain available in the private schema for disambiguation.
- The built-in/headset/DJI, disconnect/reconnect, unavailable-higher-priority,
  and hidden-profile-recovery matrix is covered with fake devices but has not
  yet been completed with the corresponding physical hardware combinations.

## [0.1.0-alpha.2] - 2026-08-30

### Added

- Default-on, event-driven adaptive correction after an authoritative final:
  for at most five seconds, one same-focus replacement inside the committed
  span may be retained as a private wrong-to-canonical pair for future
  provider requests.
- A bounded private adaptive ledger with conflict, overlap, cascade, cycle,
  capacity, and unsafe-file suppression. Manual corrections remain
  authoritative and the combined provider view remains capped at 50 pairs.
- A real isolated IBus smoke that edits one committed result and consumes the
  observation exactly once without microphone, provider, key, or network use.
- Optional, default-off local data collection that pairs each completed
  dictation WAV with a versioned JSON record containing the authoritative
  Volcengine final. Records are staged and published atomically for future
  personal-ASR dataset work.
- GTK settings, private per-user configuration, installer, service, and
  uninstaller support for choosing and retaining an external collection
  directory without making voice input depend on that directory.
- Link-aware DJI Mic Mini 2 selection before each dictation: an online
  transmitter selects its receiver for that capture stream, while an offline
  transmitter avoids the silent receiver and chooses the current non-DJI
  default or an unambiguous fallback. If link status cannot be read, the
  current system default is preserved. Playback and the desktop-wide default
  source are not changed.

### Changed

- Key, vocabulary, manual corrections, and adaptive memory are loaded for each
  new dictation, so idle configuration changes no longer require a daemon
  restart.
- The temporary `murmur-voice` engine remains selected during the bounded
  observation. Direct keys pass through, but the previous Rime/IBus engine is
  restored only when the lease finishes or is ended early by toggle, cancel,
  or failure. Focus loss prevents learning immediately but may wait for the
  remaining lease before restoration.

### Security and privacy

- Collection remains disabled until the user explicitly enables it and picks
  a directory. Audio and JSON stay local; this release does not upload a
  dataset to Orange or another host and does not train or fine-tune a model.
- Provider finals are labelled as unreviewed teacher output. They are not
  presented as `spoken_verbatim` or `preferred_output`, so future training can
  distinguish pseudo-labels from user-verified text.
- Collection write failures are reported separately and do not block or cancel
  normal dictation. Uninstall retains both the private setting and any external
  dataset instead of deleting user data.

### Known limitations

- Adaptive observation is enabled by default in this alpha and
  currently has no settings-window switch. It only works in applications that
  provide trustworthy IBus surrounding text; unsupported or ambiguous cases
  learn nothing.
- Microphone choice is refreshed at the start of a dictation; this release does
  not hand an active recording between devices when link state changes
  mid-session.
- The companion Doubao Murmur Flatpak right-Alt controller remains a separate
  controller-only project and release. It is not bundled into this package.

## [0.1.0-alpha.1] - 2026-08-26

### Added

- Native caret-local IBus preedit for cumulative streaming hypotheses and one
  authoritative final commit without clipboard paste.
- A standalone Volcengine `bigmodel_async` daemon with two-pass recognition,
  DDC, ITN, punctuation, sentence segmentation, and bounded audio buffering.
- A GTK4 settings window for a masked API key, explicit personal vocabulary,
  provider-side recognition corrections, and service controls.
- A managed desktop application-menu entry and original project icon for the
  settings window.
- Transactional per-user install, upgrade, and uninstall with exact previous
  IBus-engine restoration and no writes to the user's Rime database.
- A clean Ubuntu 24.04 x86_64 / CPython 3.12 offline preview bundle with
  locked Python wheels, checksums, and a machine-readable CycloneDX SBOM.
- Per-recording microphone re-enumeration with exact per-stream routing around
  a stale monitor default and conservative output-only card-profile recovery
  after device disconnect.

### Security and privacy

- Microphone audio is sent to Volcengine only during explicit dictation and is
  billed to the user's own account. Cancelling cannot retract uploaded audio.
- Keys and optional vocabulary/correction files are stored in validated,
  private per-user files and are never bundled or logged.
- Password, PIN, private, stale, and unfocused input contexts reject voice
  acquisition; late results are not redirected to a clipboard fallback.
- The preview build backend is pinned to security-fixed `setuptools` 83.0.0;
  the final dependency graph has no open Dependabot alert.
- Managed launchers suppress runtime bytecode writes, and the installer plus
  both user services enforce a private file-creation mask so permissive login
  defaults cannot invalidate the ownership manifest.

### Known preview limitations

- Dictation temporarily switches from the current IBus engine to
  `murmur-voice`, then restores the exact previous engine. The permanent
  librime/Rime-capable combined engine is not implemented yet. Users must
  commit or cancel a visible Rime composition before starting dictation because
  the transition engine cannot inspect stock Rime composition state.
- There is no built-in global shortcut or standalone recording indicator;
  users must bind the documented control command in their desktop settings.
- One dictation is capped at 10 minutes and waits up to 20 seconds for the
  provider's final two-pass result.
- The preview target is Ubuntu 24.04 x86_64 with CPython 3.12. It is not a
  distribution-native package or a broadly qualified Wayland release.
- A fresh graphical-login test with a physical microphone and provider account,
  plus a broad application matrix, remains unperformed and is an explicit alpha
  validation gap.
- IBus preedit belongs to one desktop session and does not cross an RDP canvas;
  remote use requires microphone redirection and installation in the remote
  session, or an explicit clipboard fallback without live inline partials.
- Uninstall deliberately retains private key, vocabulary, and correction
  files; the local key can be cleared from settings before uninstalling.
