# Privacy and trust boundaries

Open Voice Input Linux keeps ordinary Rime keyboard input local. Voice input
is different: during an explicitly started dictation, 16 kHz mono PCM audio is
sent to the online ASR service selected and configured by the user. Volcengine
is the default and the only path validated with a real key on the maintainer
workstation; Qwen and OpenAI remain experimental and have not had real-key
acceptance in this release. MiniMax is planned and not selectable.

## Remote data and billing

- The user must activate the matching speech service in their own provider
  account and provide that provider's API key. Quota and charges belong to
  that account; the project does not bundle or share a key.
- Cancelling stops capture and prevents local commit, but cannot retract audio
  already uploaded before cancellation.
- Provider-side storage, retention, regional processing, billing, and account
  policy are governed by the selected provider's terms and the user's account
  configuration.
- The current standalone daemon sends audio and reviewed ASR options. Its
  optional personal vocabulary and manual/adaptive recognition corrections
  send only bounded values kept in the user's private configuration. They never
  read the clipboard, AT-SPI accessibility tree, global keyboard events,
  document or Rime user database.

## Local secrets and text

- `murmur-voice-daemon configure` uses a masked TTY prompt and never accepts a
  key as a command-line argument.
- The GTK4 settings window never preloads or reveals the stored key and clears
  its password field after every save attempt.
- Its two-step key-clear action is allowed only while the managed voice service
  is explicitly inactive. It removes only a validated private local key file;
  users must revoke or rotate the credential separately with the selected
  provider.
- The private provider file is atomically written under
  `$XDG_CONFIG_HOME/murmur-ime/voice.json` with directory mode `0700` and file
  mode `0600`. Version 2 records the selected provider, fixed reviewed model,
  and API key; the legacy key-only form remains Volcengine-compatible. Unsafe,
  oversized, foreign-owned, public, linked, or unknown-field files are rejected.
- The optional vocabulary is stored separately as `vocabulary.json` under the
  same private directory, with the same ownership and permission checks.
- Optional recognition corrections are stored separately as
  `corrections.json` with the same checks. They are sent provider-side only;
  no local global replacement is applied to committed text.
- Adaptive correction memory is stored separately as
  `adaptive-corrections.json` with private ownership and permission checks.
  Each retained entry contains only a wrong/canonical pair of at most 64
  Unicode characters on either side, its state, and a support count. It is not
  a transcript history and stores no separate utterance/surrounding snapshot,
  timestamp, document context, edit stream, or audio.
- The local-collection choice is stored separately in private
  `data-collection.json`. A missing file means disabled; saving the setting is
  applied at the next dictation without restarting the service.
- Terminal delivery style is stored separately in private `output-style.json`
  as strict schema v1. A missing file means faithful/raw delivery. It uses the
  same user-owned `0700` directory, `0600` file, bounded read, no-symlink,
  no-extra-field, and atomic-write checks. The daemon reads it once at each
  utterance start, so an in-flight recording cannot change mode underneath the
  user.
- The complete microphone category ordering and optional exact-source
  preferences are stored separately in private `microphone-priority.json`.
  They are reloaded before each dictation, are never sent to the recognition
  provider, and do not contain audio or transcript text.
- API keys, live transcripts, vocabulary, manual/adaptive corrections, remote
  payloads, selected dataset paths, audio, and DJI status frames are not
  written to logs. An explicitly enabled dataset is the sole intentional local
  transcript/audio record described below. Status and errors use fixed codes.
- Live text travels over the user's session D-Bus to the focused IBus engine.
  It does not use clipboard paste in the primary path.

Clean expression mode does not change the provider upload boundary: the same
explicit dictation audio has already gone to the selected ASR provider. It
adds no LLM call and no extra network request. Live partial text stays raw. At
the terminal event, a bounded local deletion-only processor either produces a
replayable result or falls back to raw without blocking input; it cannot insert
content or replace terms, numbers, or letter case.

## Five-second correction observation

This observation is enabled by default in the current alpha after
a nonempty authoritative final; the settings window does not yet expose a
disable switch. It is event-driven and does not poll application text.

After one authoritative final commit, the engine can keep the same focused
input context for at most five seconds. When that application supports IBus
surrounding text, it anchors the committed span and later accepts only one
strict replacement inside it. Pure insertion/deletion, multiple edits, broad
polishing, text outside the span, focus/private-context changes, timeout, and
missing surrounding-text support produce no learned entry. Another dictation
toggle may finish the observation early.

If surrounding text is unsupported, the previous IBus engine is restored
immediately. A separate explicit `--review-last` workflow can load the latest
accepted provider final from daemon memory. It expires after ten minutes, is
replaced by the next accepted final, and is cleared on daemon shutdown. The
host-only review socket lives under a separate private runtime directory not
mounted into the compatibility Flatpak. The transcript is never put in argv,
logs, or a persistent review file.

The review submission is bound to the still-current, unexpired utterance ID.
Only the daemon updates the adaptive ledger and offers the bounded result to
the optional dataset feedback writer. A successful ledger update consumes the
review once; stale, replaced, expired, and duplicate submissions are rejected.
The UI reports feedback as disabled, enqueue-failed, or queued and awaiting
final publication, rather than treating asynchronous enqueue as durable save.

The changed block is bounded to three lexical tokens per side and must meet a
conservative similarity floor. A one-character source is learned only when it
can be expanded through an unchanged adjacent lexical token; case-only spelling
corrections remain allowed.

The observer does not open the microphone again and does not inspect the
clipboard, AT-SPI, a global keyboard hook, or other windows. It temporarily
processes only the bounded IBus surrounding snapshot for the current field and
does not persist that snapshot. Manual corrections take priority; conflicted,
overlapping, cascading, or cyclic adaptive rules are suppressed. The combined
manual/adaptive provider view is still capped at 50 pairs and is reloaded at the
next dictation without a daemon restart.

The review window labels the provider text as read-only and accepts only what
the user actually said verbatim. Removing fillers or polishing expression is a
different preferred-output task and must not silently become ASR gold.
When clean delivery differs from raw `provider_final`, the delivered version is
shown only as read-only context. The daemon immediately consumes the automatic
observation with the content-free reason
`postprocessed-output-not-safe-for-asr-learning` and never passes that span to
adaptive extraction. Explicit review still compares raw provider text with the
user's spoken-verbatim submission.

## Input-context safety

The engine refuses acquisition for password, PIN, private, fake, unfocused,
and non-preedit contexts. Focus loss clears preedit. Sender identity,
utterance ID, focus state, and strictly increasing revision protect the rest
of the session; late callbacks from an earlier recording are discarded. The
same focus and utterance binding continues through correction observation, and
any private-purpose or focus transition fails closed without learning.

The session bus and private control socket are per-user boundaries, not a
sandbox between applications owned by the same Unix account. The first D-Bus
`Acquire` call therefore trusts other processes running as that same user.
Users who require isolation between same-UID applications should not run the
developer preview. A future hardened design can require an explicit short-lived
capability armed by the user before the daemon may acquire preedit.

## Resource limits

One dictation stops normally at 600 seconds and waits at most 20 seconds for
the provider's authoritative two-pass final. Pending unsent PCM is bounded to
10 seconds. An exceeded network queue cancels the session rather than growing
memory indefinitely; compressed provider responses also have a decoded-size
limit.

When local collection is enabled, the recorder retains only the current
utterance's exact PCM in bounded memory (at most the same 600-second capture
limit) and offers a completed record to a bounded background-writer queue.
WAV encoding, filesystem sync, and publication do not run in the audio callback
or hold up the accepted final. An optional write failure is reported with a
fixed status code and does not block dictation.

This is best-effort direct-to-selected-folder storage, not a durable local
spool. Normal service shutdown gives the writer 10 seconds to drain inside
systemd's 30-second total stop budget. A stalled or unmounted destination can
leave or remove a hidden staging directory and lose that unpublished record;
already published records remain.

## Optional local recording retention

Collection is disabled by default. Enabling it requires an explicit settings
choice and an existing absolute local or mounted directory. The application
initializes or reopens `openvoiceinput-dataset-v1` below that directory. For an
enabled utterance, publication happens only after the authoritative provider
final was accepted by the focused IBus context. Cancelled, failed,
final-rejected, empty-audio, and no-final utterances are discarded from the
collector.

Each atomically published `utterances/<utterance_id>/` contains:

- `audio.wav`: the exact captured 16 kHz, mono, signed 16-bit PCM for the
  accepted utterance;
- `record.json`: versioned identifiers, time, audio format/frame counts and
  hashes, provider/model identity, privacy-preserving microphone provenance,
  post-hoc numeric PCM quality summaries, three deliberately separate labels,
  and one separate machine-delivery audit;
- `provider_final.text`: the authoritative result from the selected provider,
  labelled
  `teacher-unreviewed`, which is a pseudo-label rather than ground truth;
- `spoken_verbatim.text` and `preferred_output.text`: both `null` and
  `unreviewed` until a separate human-review workflow exists.
- `delivery`: exact inserted text with `machine-derived-unreviewed` status,
  frozen mode, processor/version, outcome, and replayable deletion edits. The
  raw provider label is retained even when clean delivery differs.

The immutable utterance record remains exactly `audio.wav` + `record.json`.
After it is published, the dataset-level schema-v2
`usage/<utterance_id>.json` index adds only time, audio duration and the
non-whitespace character count of delivered text for private dashboard totals.
Schema-v1 usage summaries remain readable with their original meaning.

PCM quality analysis runs only in the background writer after final acceptance.
It records bounded overall/first-second sample counts, clipped and zero
fractions, RMS/peak dBFS, and normalized DC offset. It does not classify a
record as good/bad, drop or alter audio, delay capture startup, or inspect
speech content.

Dashboard aggregation runs outside the GTK thread and reads only the dataset
marker plus `usage/<utterance_id>.json`. It never reads or displays
`provider_final`, `delivery`, the two review labels, or audio. Disabling
collection also disables the scan; an
unavailable local or mounted destination produces an unknown/unavailable state,
not a misleading zero, and does not stop ordinary dictation.

The collection feature does not authenticate to or mount Orange, upload to
Google Drive, train, fine-tune, or distil a model, or add application-level
encryption. The normal ASR path still sends the audio to the selected online
provider as described above. A compatible remote filesystem already mounted by
the user is part of the selected filesystem boundary. The selected filesystem determines effective
visibility, sharing, backup, and at-rest protection; directory/file modes
cannot strengthen a filesystem that does not enforce them.

Disabling collection and changing its destination take effect for the next
utterance without a service restart. A disable that has returned also prevents
older queued or staged, unpublished records from being published. Already
published records remain until the user deliberately removes them. The
uninstaller preserves `output-style.json`, `microphone-priority.json`,
`data-collection.json`, and every dataset in a user-selected directory.
First-party resumable Orange
transport, label review, deletion tooling, and model training remain future
work. User-managed SSHFS and asynchronous Google Drive backup are documented in
[remote-dataset-storage.md](remote-dataset-storage.md); see also
[personal-asr-data-plan.md](personal-asr-data-plan.md).

## Microphone priority and DJI link probe

The private microphone policy contains category order and optional exact Pulse
source names, not audio. It is reloaded for each new dictation. The recommended
default is `DJI > headset > other external > built-in`; users can reorder it.
Missing policy uses that default, while an existing invalid/unsafe file rejects
the next dictation start rather than silently changing the user's source choice.

A bounded USB status probe may distinguish a linked DJI Mic Mini 2 transmitter
from its still-enumerated but silent receiver. Proven online makes DJI eligible
at its configured position; proven offline excludes it; unknown does not promote
it ahead of a known-working alternative. The probe does not retain or log USB
frames or identifiers. Selection never changes a playback sink or requests a
system default-source change. The daemon does not request a mid-utterance
handoff: link or device changes are considered at the next dictation. A separate
desktop router may nevertheless move the already-open Pulse source-output. A
read-only background observer therefore records the actual route category and a
bounded transition list without delaying or filtering audio.

Until that source-output match succeeds, the dataset explicitly records actual
route status as `unknown`; it does not relabel the intended selection as the
actual microphone. Enumeration is front-loaded around stream open, backs off to
five seconds, caches already-seen source indexes, and stops at a fixed route
transition bound rather than continuously spawning high-frequency probes.

Dataset microphone metadata never stores raw Pulse source names, USB serials,
Bluetooth addresses, or user-visible hardware labels. Its stable fingerprint
is derived only from allowlisted category, transport, form factor, and numeric
vendor/product model identifiers. It is deliberately not a globally unique
device identifier, and identical models may share it. The observer runs only
for an active opted-in Pulse dataset capture, uses read-only
source/source-output enumeration, and never changes a sink, volume, mute state,
default source, or stream route.
