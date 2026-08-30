# Changelog

All notable user-visible changes to Open Voice Input Linux will be recorded
here. The project has not published a stable release yet.

## [Unreleased]

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

### Build and quality

- Exact-commit Debian CI now rejects packages larger than 5 MiB or an
  `Installed-Size` above 10 MiB, so future UI work cannot silently replace the
  lightweight native client with a large runtime.

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
