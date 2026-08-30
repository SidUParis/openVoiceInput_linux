# Changelog

All notable user-visible changes to Open Voice Input Linux will be recorded
here. The project has not published a stable release yet.

## [Unreleased]

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
