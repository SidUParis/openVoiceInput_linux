# Inline preedit prototype demo

This local-only demo proves that Murmur IME can replace a floating transcript
box with text rendered by IBus directly at the active application caret. It
uses fixed Chinese sample text: it does not open the microphone, read
credentials, or make network requests.

## Run the demo

Run each command from the repository root.

1. Start the development engine and leave it running in the foreground:

   ```bash
   ./engine/murmur-ime-engine
   ```

2. In a second terminal, select the dynamically registered engine and open the
   GTK test window:

   ```bash
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

Restore the normal Rime engine after testing:

```bash
ibus engine rime
```

The development registration is intentionally temporary. Stopping
`murmur-ime-engine` removes its D-Bus service and it must be started again for
another demo session.

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
- Use `Ctrl+C` in the engine terminal when finished, then restore `rime` with
  the command above.

This prototype isolates UI behavior from ASR behavior. A later voice daemon can
produce the same ordered calls from streaming recognition without changing the
IBus rendering and focus-safety path exercised here.
