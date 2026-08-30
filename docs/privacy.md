# Privacy and trust boundaries

Open Voice Input Linux keeps ordinary Rime keyboard input local. Voice input
is different: during an explicitly started dictation, 16 kHz mono PCM audio is
streamed to the Volcengine BigModel ASR service configured by the user.

## Remote data and billing

- The user must activate the matching speech service in their own Volcengine
  project and provide their own API key. Quota and charges belong to that
  account; the project does not bundle or share a key.
- Cancelling stops capture and prevents local commit, but cannot retract audio
  already uploaded before cancellation.
- Provider-side storage, retention, regional processing, and account policy
  are governed by the user's Volcengine agreement and configuration.
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
  users must revoke or rotate the credential separately in Volcengine.
- The fallback key-only file is atomically written under
  `$XDG_CONFIG_HOME/murmur-ime/voice.json` with directory mode `0700` and file
  mode `0600`. It is rejected if it is a symlink, foreign-owned, public, too
  large, or contains fields other than `api_key`.
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
  hashes, provider/model identity, and three deliberately separate labels;
- `provider_final.text`: the authoritative Volcengine result, labelled
  `teacher-unreviewed`, which is a pseudo-label rather than ground truth;
- `spoken_verbatim.text` and `preferred_output.text`: both `null` and
  `unreviewed` until a separate human-review workflow exists.

The collection feature does not make an extra cloud upload, copy data to the
Orange computer, train, fine-tune, or distil a model, or add application-level
encryption. The normal ASR path still sends the audio to Volcengine as described
above. The selected filesystem determines effective visibility, sharing,
backup, and at-rest protection; directory/file modes cannot strengthen a
filesystem that does not enforce them.

Disabling collection and changing its destination take effect for the next
utterance without a service restart. A disable that has returned also prevents
older queued or staged, unpublished records from being published. Already
published records remain until the user deliberately removes them. The
uninstaller preserves `microphone-priority.json`, `data-collection.json`, and
every dataset in a user-selected directory. Orange transport, label review,
deletion tooling, and
model training remain future work. See
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
system default-source change. There is no mid-utterance handoff: link or device
changes are considered at the next dictation.
