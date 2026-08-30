# Open Voice Input Linux architecture

## Design goal

Open Voice Input Linux must feel like one input method while isolating
keyboard input from all microphone, network, and provider failures. The IBus
engine is therefore a small synchronous frontend; ASR runs in a separately
supervised user daemon.

## Implementation status

The repository now contains a pure Python, voice-only IBus engine and a
self-contained Volcengine daemon that prove native caret-local preedit and
final commit with this transition flow:

```text
current IBus engine -> murmur-voice -> Acquire/Partial/Final over D-Bus
                    -> <=5 s same-focus correction observation
                    -> exact previous IBus engine
```

After final commit, the previous engine is restored when bounded correction
observation finishes; the next toggle, cancellation, or daemon failure can
finish it early. Focus loss invalidates learning immediately, but the daemon
does not receive an engine-to-daemon focus signal, so restoration may wait for
the remainder of the five-second lease. This removes the black transcription
box during dictation, but it is not the final combined input method: IBus
assigns one engine per input context, so stock Rime cannot provide Chinese
keyboard composition while the voice-only engine is selected, including this
short observation lease. The production work is to move the proven
session/preedit rules into an engine derived from `ibus-rime` and linked to
librime.

## Components

### IBus engine

The production engine will be derived from `ibus-rime` and link to librime. It
will own the active IBus input context and be the only component allowed to
mutate application preedit or commit text.

Responsibilities:

- normal Rime key processing and candidate UI;
- focus-in/focus-out and input-purpose tracking;
- a monotonically increasing focus token;
- voice command initiation and cancellation;
- replacing the complete voice preedit with each cumulative ASR hypothesis;
- committing one final result only when session and focus tokens still match;
- anchoring that committed span from IBus surrounding text and returning one
  same-focus observation snapshot at the end of a bounded lease.

The engine never opens a microphone, reads secrets, or performs network I/O.
It does not inspect clipboard, AT-SPI, global keyboard events, or stock Rime
data.

### Voice daemon

The implemented developer-preview daemon runs in the foreground, owns one
dictation utterance at a time, accepts bounded commands on a private Unix
socket, and calls the engine's session D-Bus service. The optional source-tree
installer supervises it with a hardened systemd user unit and a dedicated
virtual environment. Distribution-native packaging and D-Bus activation are
not implemented yet.

Responsibilities:

- per-recording microphone re-enumeration, DJI Mic Mini 2 link-aware source
  choice, exact per-stream Pulse routing, conservative profile recovery after
  disconnects, capture, and fixed-code error reporting;
- a bounded start deadline plus an invisible post-preflight focus heartbeat,
  so delayed discovery cannot open capture for an invalidated input context;
- provider authentication and WebSocket lifecycle;
- Volcengine `bigmodel_async` request/response handling;
- live partial, two-pass final, timeout, and cancellation events;
- deterministic extraction and private persistence of at most one strict
  replacement from the bounded post-final snapshot;
- per-dictation reload and conflict-safe compilation of manual/adaptive
  correction pairs into a provider view of at most 50 entries;
- default-off local collection: bounded PCM retention for an enabled utterance
  and nonblocking handoff of an accepted provider final to a background writer;
- zero transcription text or secret content in logs.

The daemon never commits text and cannot choose a target application.

The DJI decision applies only while opening a new daemon capture stream.
Proven online selects the unique DJI source; proven offline chooses only a safe
non-DJI fallback; unknown preserves the existing system/default-source path.
It never changes a playback sink or requests a system default-source change,
and it does not hand a live utterance between microphones.

### Settings application

A bounded GTK4 settings application now manages the private key-only fallback,
explicit vocabulary, optional explicit recognition corrections, a
disabled-by-default local dataset destination, and service controls. Collection
enable/disable/path saves take effect at the next utterance without a daemon
restart. Adaptive correction memory is maintained automatically in a separate
private ledger and does not require a settings round trip. The masked
interactive `configure`
command remains available. Secret Service storage and its migration lifecycle
remain target features rather than part of this transition prototype.

### Recording indicator

The target indicator is deliberately not a transcription window. It may show
only:

- idle/ready;
- recording;
- finalizing/two-pass recognition;
- recoverable error.

It must not take focus. Inline transcription belongs to IBus preedit.

## Session state

```text
IDLE -> STARTING -> RECORDING -> FINALIZING -> OBSERVING -> IDLE
  ^          |           |            |             |
  +----------+-----------+------------+-------------+
             cancel / error / next toggle; focus loss invalidates learning
```

Each start request carries `{engine_id, focus_token, utterance_id}`. Every
daemon event echoes these values, and each text event carries a monotonically
increasing `revision`. The engine ignores any mismatched, stale, or late event.

## Text lifecycle

1. A streaming hypothesis replaces the entire voice preedit.
2. A `definite` two-pass sentence replaces the corresponding hypothesis.
3. The connection-level final event permits a single `commit_text` call.
4. A newer IBus surrounding-text revision anchors the exact committed span.
   If unsupported or ambiguous, commit still succeeds but learning is disabled.
5. For at most five seconds the same focus may produce one observation
   snapshot. Only a single replacement inside the anchored span is eligible;
   insertion, deletion, multiple edits, polishing, a final active selection,
   focus/private
   changes, or timeout learns nothing.
6. Finish restores the exact previous IBus engine. A next toggle may finish
   observation early before starting another dictation.
7. Focus loss clears preedit or invalidates observation immediately. After a
   committed final, the daemon restores the previous engine when the remaining
   lease expires because this prototype has no reverse focus-loss signal.
8. The implemented safety timeout cancels preedit when an authoritative final
   is missing. Any future manual recovery must remain explicit and must never
   silently commit into a different application.

Clipboard injection and synthetic `Ctrl+V` are not part of the primary path.
The observer also avoids clipboard, AT-SPI, and global keyboard monitoring. It
retains only a bounded pair/state/support ledger, never a separate surrounding
snapshot or transcript record.

If local collection was enabled at utterance start, the same accepted final
also freezes the exact captured PCM and offers it to a bounded background
queue. The writer completes `audio.wav` and `record.json` below `.pending`,
then atomically renames the directory into the selected dataset's `utterances/`
tree. `provider_final` remains an unreviewed pseudo-label;
`spoken_verbatim`/`preferred_output` are null. Cancellation, failure, or final
rejection discards the in-memory collector state.

## Rime composition boundary

The transition preview cannot inspect composition state inside the stock Rime
engine before switching to the voice-only engine. Users must therefore commit
or cancel any visible Rime composition before starting dictation; switching
may otherwise discard that unfinished composition. The future combined
librime-capable engine must refuse voice start while a composition is active,
unless interaction tests first define an explicit typed-and-spoken composition
behavior.

## Packaging

The initial package target is Debian/Ubuntu. IBus engines and D-Bus activation
integrate poorly with a fully sandboxed Flatpak, so Flatpak is not the primary
distribution format for the combined engine. A separately delivered
compatibility Flatpak may remain a controller/indicator for the daemon's
bounded control interface; it does not own audio capture, provider access,
microphone routing, or dataset publication and is not the package for this
engine/daemon implementation.

Open Voice Input Linux will keep packaged Rime data in
`/usr/share/murmur-ime/rime-data` and mutable user data in
`$XDG_DATA_HOME/murmur-ime/rime`. It must never concurrently open the stock
ibus-rime database under `~/.config/ibus/rime`. Importing existing preferences
or user data is an explicit, one-time migration operation.

## Personal ASR data path

Adaptive correction still retains no audio. The separate collector can now
publish an explicitly opted-in accepted utterance as exact 16 kHz mono signed
16-bit WAV plus versioned JSON under `openvoiceinput-dataset-v1` in a
user-selected existing local or mounted folder. It uses bounded memory and a
background writer; disabling prevents unpublished queued/staged publication
while already published records remain.

The current collector does not transfer to Orange, upload a dataset, review
labels, train a model, or add application-level encryption. Its schema keeps
the null `spoken_verbatim` and `preferred_output` fields distinct from the
unreviewed `provider_final` pseudo-label. Filesystem policy determines actual
visibility. Review, collection quality, and evaluation must precede any model
fine-tune or distillation. See
[personal-asr-data-plan.md](personal-asr-data-plan.md).

The `murmur-ime` paths above, along with the 0.x IBus, D-Bus, executable, and
systemd names, remain historical compatibility ABI. The public product and
repository name is Open Voice Input Linux / `openVoiceInput_linux`.
