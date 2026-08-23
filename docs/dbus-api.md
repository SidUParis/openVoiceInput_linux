# Open Voice Input Linux D-Bus API sketch

This is a design sketch, not yet a stable public API.

The 0.x `org.murmur.*` names are retained as compatibility ABI. They are
internal protocol identifiers, not the current public project name.

The currently implemented transition prototype uses the smaller
`org.murmur.IME.Preedit1` sidecar-to-engine bridge documented in
[python-preedit-prototype.md](python-preedit-prototype.md). The API below is the
target contract after the voice daemon and librime-capable engine are combined;
it is not yet implemented.

## Service

```text
Bus name:   org.murmur.IME.Voice1
Object:     /org/murmur/IME/Voice1
Interface:  org.murmur.IME.Voice1
```

## Engine to daemon

```text
Start(engine_id: s, focus_token: t, utterance_id: t, options: a{sv})
Stop(engine_id: s, focus_token: t, utterance_id: t)
Cancel(engine_id: s, focus_token: t, utterance_id: t)
```

`options` contains non-secret behavior flags only. The daemon resolves the API
key itself from Secret Service.

## Daemon to engine

```text
State(engine_id: s, focus_token: t, utterance_id: t, state: s)
Partial(engine_id: s, focus_token: t, utterance_id: t, revision: t, text: s)
Final(engine_id: s, focus_token: t, utterance_id: t, revision: t, text: s)
Error(engine_id: s, focus_token: t, utterance_id: t, code: s)
```

The engine accepts an event only when all three identity values match its
current voice session. Text events must also have a newer revision than the
last accepted text event. `Final` is emitted once, after the provider's
explicit connection-level final frame.

## Limits

- Maximum UTF-8 text size must be bounded.
- Only one active utterance per daemon in the MVP.
- Calls with stale or unknown utterance IDs are idempotently ignored.
- Error values are stable codes; remote payloads and secrets are never exposed.
