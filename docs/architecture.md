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
rime -> murmur-voice -> Acquire/Partial/Final over D-Bus -> rime
```

The previous engine is restored on final, cancellation, or failure. This
removes the black transcription box during dictation, but it is not the final
combined input method: IBus assigns one engine per input context, so stock
Rime cannot provide Chinese keyboard composition while the voice-only engine
is selected. The production work is to move the proven session/preedit rules
into an engine derived from `ibus-rime` and linked to librime.

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
- committing one final result only when session and focus tokens still match.

The engine never opens a microphone, reads secrets, or performs network I/O.

### Voice daemon

The implemented developer-preview daemon runs in the foreground, owns one
dictation utterance at a time, accepts bounded commands on a private Unix
socket, and calls the engine's session D-Bus service. The optional source-tree
installer supervises it with a hardened systemd user unit and a dedicated
virtual environment. Distribution-native packaging and D-Bus activation are
not implemented yet.

Responsibilities:

- microphone capture and level/error reporting;
- provider authentication and WebSocket lifecycle;
- Volcengine `bigmodel_async` request/response handling;
- live partial, two-pass final, timeout, and cancellation events;
- zero transcription text or secret content in logs.

The daemon never commits text and cannot choose a target application.

### Settings application

A bounded GTK4 settings application now manages the private key-only fallback,
explicit vocabulary, and service controls. The masked interactive `configure`
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
IDLE -> STARTING -> RECORDING -> FINALIZING -> IDLE
  ^          |           |            |
  +----------+-----------+------------+
             cancel / focus loss / error
```

Each start request carries `{engine_id, focus_token, utterance_id}`. Every
daemon event echoes these values, and each text event carries a monotonically
increasing `revision`. The engine ignores any mismatched, stale, or late event.

## Text lifecycle

1. A streaming hypothesis replaces the entire voice preedit.
2. A `definite` two-pass sentence replaces the corresponding hypothesis.
3. The connection-level final event permits a single `commit_text` call.
4. Focus loss clears preedit and cancels the utterance.
5. The implemented safety timeout cancels preedit when an authoritative final
   is missing. Any future manual recovery must remain explicit and must never
   silently commit into a different application.

Clipboard injection and synthetic `Ctrl+V` are not part of the primary path.

## Rime composition boundary

The MVP refuses to start voice recording while a Rime composition is active.
This avoids silently committing or discarding partially typed text. A future
version may explicitly compose typed and spoken segments after interaction
tests define predictable behavior.

## Packaging

The initial package target is Debian/Ubuntu. IBus engines and D-Bus activation
integrate poorly with a fully sandboxed Flatpak, so Flatpak is not the primary
distribution format for the combined engine.

Open Voice Input Linux will keep packaged Rime data in
`/usr/share/murmur-ime/rime-data` and mutable user data in
`$XDG_DATA_HOME/murmur-ime/rime`. It must never concurrently open the stock
ibus-rime database under `~/.config/ibus/rime`. Importing existing preferences
or user data is an explicit, one-time migration operation.

The `murmur-ime` paths above, along with the 0.x IBus, D-Bus, executable, and
systemd names, remain historical compatibility ABI. The public product and
repository name is Open Voice Input Linux / `openVoiceInput_linux`.
