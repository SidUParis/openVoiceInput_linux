# Architecture

## Design goal

Murmur IME must feel like one input method while isolating keyboard input from
all microphone, network, and provider failures. The IBus engine is therefore a
small synchronous frontend; ASR runs in a separately supervised user daemon.

## Components

### IBus engine

The engine is derived from `ibus-rime` and links to librime. It owns the active
IBus input context and is the only component allowed to mutate application
preedit or commit text.

Responsibilities:

- normal Rime key processing and candidate UI;
- focus-in/focus-out and input-purpose tracking;
- a monotonically increasing focus token;
- voice command initiation and cancellation;
- replacing the complete voice preedit with each cumulative ASR hypothesis;
- committing one final result only when session and focus tokens still match.

The engine never opens a microphone, reads secrets, or performs network I/O.

### Voice daemon

The daemon is D-Bus activated and owns one dictation utterance at a time.

Responsibilities:

- microphone capture and level/error reporting;
- provider authentication and WebSocket lifecycle;
- Volcengine `bigmodel_async` request/response handling;
- live partial, two-pass final, timeout, and cancellation events;
- zero transcription text or secret content in logs.

The daemon never commits text and cannot choose a target application.

### Settings application

The GTK settings application manages non-secret preferences and provider
credentials. The API key is stored through Secret Service/libsecret. A
permission-`0600` configuration file is a fallback for minimal systems only.

### Recording indicator

The indicator is deliberately not a transcription window. It may show only:

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
5. A safety timeout may offer the latest text for manual recovery, but must not
   silently commit it into a different application.

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

Murmur IME will keep packaged Rime data in
`/usr/share/murmur-ime/rime-data` and mutable user data in
`$XDG_DATA_HOME/murmur-ime/rime`. It must never concurrently open the stock
ibus-rime database under `~/.config/ibus/rime`. Importing existing preferences
or user data is an explicit, one-time migration operation.
