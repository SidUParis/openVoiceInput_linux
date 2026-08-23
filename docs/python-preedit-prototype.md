# Python inline-preedit prototype

This prototype exists to make the core interaction testable before the
ibus-rime/librime engine is implemented: cumulative speech hypotheses appear
as native IBus preedit at the application's caret, and one authoritative final
result becomes committed text.

It is implemented and has been verified with the local Doubao Murmur sidecar.
The sidecar bridge records the previous engine, temporarily selects
`murmur-voice`, acquires the focused preedit session, forwards cumulative ASR
partials and the final result, then restores the previous engine on final,
cancel, or error. The bridge code remains in the local sidecar checkout and is
not vendored into this repository.

## Run from the repository

Dependencies on Ubuntu are `ibus`, `gir1.2-ibus-1.0`, and `python3-gi`.

```bash
./engine/murmur-ime-engine
```

The process calls `IBus.Bus.register_component()` at runtime. This is
intentional: IBus 1.5 does not scan `~/.local/share/ibus/component`, and a
development prototype should not need root access or an IBus restart. In a
second terminal, while a normal text field has focus:

```bash
ibus engine murmur-voice
```

Use `scripts/send_preedit_demo.py` to feed partial/final text, or connect the
voice sidecar to the D-Bus contract below. Restore the normal input method with
`ibus engine rime`.

The deterministic demo uses manual engine selection so every transition is
visible. The verified local voice sidecar performs the same selection and
restoration automatically for the duration of a recording.

For an optional persistent per-user development install:

```bash
./scripts/install-user.sh
```

That script copies only the prototype engine under the XDG user data directory
and enables the included systemd user unit. `scripts/uninstall-user.sh` removes
those prototype files without touching IBus, Rime, Rime Ice, or user data.

## Temporary sidecar-to-engine D-Bus bridge

```text
Bus name:  org.murmur.IME.Preedit1
Object:    /org/murmur/IME/Preedit1
Interface: org.murmur.IME.Preedit1

Acquire(utterance_id: s) -> accepted: b
Partial(utterance_id: s, revision: t, text: s) -> accepted: b
Final(utterance_id: s, revision: t, text: s) -> accepted: b
Cancel(utterance_id: s) -> accepted: b
```

`Acquire` internally captures the active engine's monotonically increasing
focus token and the caller's unique D-Bus name. Every later call must match the
caller, utterance, and focus token. Text revisions must strictly increase.
Focus loss, input-context reset, engine disable, caller disappearance, and a
switch to a private field all clear preedit and invalidate the session. `Final`
is accepted once, clears preedit, and calls `commit_text()` once.

IBus's global placeholder context identifies itself as client `fake`. It is
never considered an editable focus and cannot acquire a voice session; this
prevents an `ibus engine` CLI operation from becoming an accidental target.

The engine refuses `Acquire` in IBus password/PIN fields, fields marked with
the IBus `PRIVATE` hint, or clients without native preedit capability. Text and
IDs are size-bounded, and transcript content is never logged.

## Important coexistence limit

IBus assigns only one engine to an input context. While `murmur-voice` is
selected, ordinary keys pass through but stock `ibus-rime` is not processing
them. While `rime` is selected, the prototype cannot own its preedit. This is
therefore a real inline-preedit demonstration, not yet the combined Chinese
keyboard.

The production implementation must move the same focus/session rules into a
librime-capable engine derived from ibus-rime. It will then provide Rime Ice
typing and voice preedit in the same engine.

## Safety checks

1. Send a partial in field A, focus field B, then send the old final. Neither
   field may receive the final.
2. Try `Acquire` in password, PIN, and private test fields. All must return
   `false`, and the microphone should not start.
3. Send duplicate or decreasing revisions. They must return `false`.
4. Send an improved final after a partial. The preedit must be replaced by
   ordinary committed text exactly once.

Run the dependency-free state and contract tests with:

```bash
PYTHONPATH=engine python3 -m unittest discover -s engine/tests -v
```

The current suite contains 13 tests.
