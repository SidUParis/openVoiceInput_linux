# Open Voice Input Linux Python inline-preedit prototype

This prototype exists to make the core interaction testable before the
ibus-rime/librime engine is implemented: cumulative speech hypotheses appear
as native IBus preedit at the application's caret, and one authoritative final
result becomes committed text.

The executable, IBus engine, D-Bus bridge, systemd unit, and install directory
below intentionally retain their historical `murmur-*` and `org.murmur.*`
names as 0.x compatibility ABI.

It is implemented and has offline coverage with the self-contained voice
daemon in this repository. The daemon bridge records the previous engine,
temporarily selects `murmur-voice`, acquires the focused preedit session,
forwards cumulative ASR partials and the final result, keeps at most a
five-second same-focus correction-observation lease, then restores the previous
engine. The next toggle, cancel, or error ends that lease early. Focus loss
invalidates learning immediately, but restoration may wait for the remaining
lease because this prototype has no reverse focus-loss signal. It does not
require a separate Doubao Murmur checkout.

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
previous_engine=$(ibus engine)
test -n "$previous_engine"
ibus engine murmur-voice
```

Use `scripts/send_preedit_demo.py` to feed partial/final text, or connect the
voice sidecar to the D-Bus contract below. Keep that second shell open and
restore the exact engine it recorded with `ibus engine "$previous_engine"`.

The deterministic demo uses manual engine selection so every transition is
visible. The verified local voice sidecar performs the same selection and
restoration automatically for the duration of a recording.

For an optional persistent per-user development install:

```bash
./scripts/install-user.sh --allow-network
```

That script installs the prototype engine plus a managed standalone voice
environment and two systemd user units under the XDG user directories. A
source checkout uses the explicit developer-network flag shown above; a
no-network install requires the complete lock/SBOM-verified preview bundle.
Full installation,
configuration, upgrade, and uninstall behavior is documented in
[user-service.md](user-service.md). `scripts/uninstall-user.sh` removes managed
code and units without touching IBus, Rime, Rime Ice, or their user data; the
private voice configuration is retained.

## Temporary sidecar-to-engine D-Bus bridge

```text
Bus name:  org.murmur.IME.Preedit1
Object:    /org/murmur/IME/Preedit1
Interface: org.murmur.IME.Preedit1

Acquire(utterance_id: s) -> accepted: b
Partial(utterance_id: s, revision: t, text: s) -> accepted: b
Final(utterance_id: s, revision: t, text: s) -> accepted: b
FinishObservation(utterance_id: s)
  -> (accepted: b, baseline_text: s, committed_start: u, committed_end: u,
      current_text: s, cursor: u, anchor: u)
Cancel(utterance_id: s) -> accepted: b
```

`Acquire` internally captures the active engine's monotonically increasing
focus token and the caller's unique D-Bus name. Every later call must match the
caller, utterance, and focus token. Text revisions must strictly increase.
Focus loss, engine disable, caller disappearance, and a switch to a private
field all clear preedit/observation and invalidate the session. An input-context
reset before Final also invalidates preedit; during the post-Final observation,
GTK's ordinary same-focus edit reset instead requests a fresh surrounding
snapshot while the focus token remains authoritative. `Final` is accepted once,
clears preedit, and calls `commit_text()` once. It then waits for a newer IBus
surrounding-text revision to anchor the committed span. `FinishObservation`
consumes one bounded snapshot only for the same sender, utterance, and focus.
Missing surrounding-text support or an untrustworthy baseline returns no
observation without undoing the commit. See [dbus-api.md](dbus-api.md) for the
complete contract.

IBus's global placeholder context identifies itself as client `fake`. It is
never considered an editable focus and cannot acquire a voice session; this
prevents an `ibus engine` CLI operation from becoming an accidental target.

The engine refuses `Acquire` in IBus password/PIN fields, fields marked with
the IBus `PRIVATE` hint, or clients without native preedit capability. Text and
IDs are size-bounded, and transcript content is never logged.

## Important coexistence limit

IBus assigns only one engine to an input context. While `murmur-voice` is
selected, ordinary keys pass through but stock `ibus-rime` is not processing
them. This includes the post-final observation window of at most five seconds;
the next toggle can end it early. While `rime` is selected, the prototype
cannot own its preedit. This is therefore a real inline-preedit demonstration,
not yet the combined Chinese keyboard.

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
5. After final, make one replacement in the same field and finish observation.
   A bounded snapshot must be returned once. Repeat with focus loss, private
   input, or unsupported surrounding text; no snapshot may be returned and the
   committed final must remain untouched.

Run the dependency-free state and contract tests with:

```bash
PYTHONPATH=engine python3 -m unittest discover -s engine/tests -v
```

The suite covers the state and contract boundaries without a provider key or
microphone.
