# Open Voice Input Linux inline-preedit prototype demo

This local-only demo proves that Open Voice Input Linux can replace a floating
transcript box with text rendered by IBus directly at the active application
caret. It uses fixed Chinese sample text: it does not open the microphone,
read credentials, or make network requests.

The commands and D-Bus names below intentionally retain the historical
`murmur-*` and `org.murmur.*` 0.x compatibility ABI.

## Run the demo

Run each command from the repository root.

1. Start the development engine and leave it running in the foreground:

   ```bash
   ./engine/murmur-ime-engine
   ```

2. In a second terminal, select the dynamically registered engine and open the
   GTK test window:

   ```bash
   previous_engine=$(ibus engine)
   test -n "$previous_engine"
   ibus engine murmur-voice
   ./scripts/preedit_demo.py
   ```

3. Click the single-line field in the test window, leave the caret there, and
   run this in a third terminal:

   ```bash
   ./scripts/send_preedit_demo.py
   ```

The partial Chinese sentence should replace itself in place at the caret. It
must not be appended to the entry's committed value. The final, punctuated
sentence is then committed exactly once, at which point the `已提交文本` label
also changes.

To exercise cleanup without committing anything:

```bash
./scripts/send_preedit_demo.py --cancel
```

In the same second terminal, restore the exact engine recorded before testing:

```bash
ibus engine "$previous_engine"
```

The development registration is intentionally temporary. Stopping
`murmur-ime-engine` removes its D-Bus service and it must be started again for
another demo session.

## Zero-download isolated smoke

The repository also contains a one-command smoke test that does not select or
query the real desktop's IBus engine:

```bash
python3 -I scripts/run_isolated_preedit_smoke.py
```

It starts a private Xvfb display, session D-Bus, IBus daemon, temporary HOME,
dynamic engine and GTK entry. Six fixed Chinese partials must be accepted while
the entry's committed value remains empty; the fixed final must then be
committed exactly once. It retains `partial.png`, `final.png`, and private logs
in the printed mode-0700 temporary directory. It never starts the voice daemon,
opens a microphone, reads provider configuration, or contacts a provider.

This is a real IBus/GTK preedit test, but it is not a fresh-machine test. It
reuses the host's installed IBus/GTK/Python packages and has no real logind,
PipeWire, systemd user session, provider account, or application compatibility
matrix.

## Demo protocol

The sender keeps one `Gio.DBusProxy` and therefore one session-bus connection
for the entire sequence. The engine binds `Acquire` to that connection's
unique sender so an unrelated process cannot modify the acquired preedit.

```text
Bus name:  org.murmur.IME.Preedit1
Object:    /org/murmur/IME/Preedit1
Interface: org.murmur.IME.Preedit1

Acquire(utterance_id: s) -> (accepted: b)
Partial(utterance_id: s, revision: t, text: s) -> (accepted: b)
Final(utterance_id: s, revision: t, text: s) -> (accepted: b)
Cancel(utterance_id: s) -> (accepted: b)
```

Every `Partial` and `Final` uses the same utterance ID and a strictly
increasing revision. Rejected calls stop the demo. If the sender is interrupted
after a successful acquisition, it makes a best-effort `Cancel` call.

## Troubleshooting

- `ServiceUnknown`: start `./engine/murmur-ime-engine` first and keep it
  running.
- `ibus engine murmur-voice` cannot find the engine: the development engine
  has not registered with the current IBus daemon yet.
- `Acquire was rejected`: select `murmur-voice`, focus an editable field, and
  make sure another demo does not already own the preedit.
- Calls are accepted but no inline text appears: click the GTK entry again and
  confirm `ibus engine` prints `murmur-voice`.
- Use `Ctrl+C` in the engine terminal when finished, then restore the exact
  engine captured in `previous_engine` with the command above.

This prototype isolates UI behavior from ASR behavior. A later voice daemon can
produce the same ordered calls from streaming recognition without changing the
IBus rendering and focus-safety path exercised here.
