# Open Voice Input Linux voice daemon

This directory contains the first self-contained voice-daemon MVP. It no
longer imports or runs a separate Doubao Murmur checkout. The foreground
daemon captures 16 kHz mono PCM, streams it to Volcengine bigmodel_async,
sends cumulative hypotheses and one authoritative final to the existing
org.murmur.IME.Preedit1 engine, and restores the previously selected IBus
engine.

There is no transcription window and no clipboard/paste fallback. Transcript
text appears only as native IBus preedit at the focused caret and is never
written to logs.

## Runtime dependencies

- Python 3.11 or newer;
- IBus and PyGObject/Gio (python3-gi on Debian/Ubuntu);
- PortAudio (libportaudio2; development headers may be needed to build
  sounddevice);
- Python packages sounddevice 0.4.6 or newer but below 1, and websockets 13
  or newer but below 18.
- GTK4 introspection data when using the bundled native settings window
  (`gir1.2-gtk-4.0` on Ubuntu).

From this directory, install into a virtual environment that can see the
system PyGObject package:

    python3 -m venv --system-site-packages .venv
    PYTHONNOUSERSITE=1 .venv/bin/pip install -e '.[test]'

## Private key-only configuration

The fallback configuration contains only the Volcengine API key. Provider
endpoint, 2.0 resource ID, two-pass recognition, DDC, ITN, punctuation,
sentence settings, and 200 ms chunks use reviewed defaults.

Each user must first activate the matching BigModel streaming speech service
in their own Volcengine project. Audio usage, quota, and billing belong to that
account; the project never bundles a shared key. Cancelling stops local input
but cannot retract microphone audio already sent during the active dictation.

Run the masked, confirmation-based prompt:

    PYTHONNOUSERSITE=1 .venv/bin/murmur-voice-daemon configure

It atomically writes $XDG_CONFIG_HOME/murmur-ime/voice.json (or
~/.config/murmur-ime/voice.json) with directory mode 0700 and file mode 0600.
There is intentionally no API-key command-line argument, so the key does not
enter shell history. config.example.json contains only a non-working
placeholder.

An installed preview also provides `open-voice-input-settings`. Its
`Gtk.PasswordEntry` is never prefilled and is cleared after every save attempt.
The window can edit the explicit vocabulary and optional recognition
corrections, and explicitly enable/start or disable/stop the user service.
Saving alone never contacts Volcengine or restarts an active recording.
After the service is explicitly disabled and stopped, a two-step destructive
button can remove only the local private key file; it never contacts or revokes
the provider credential itself.

## Optional explicit personal vocabulary

The API-key file remains key-only. Personal terms live separately in
$XDG_CONFIG_HOME/murmur-ime/vocabulary.json (or
~/.config/murmur-ime/vocabulary.json), with the same private directory mode
0700 and file mode 0600. If this file is absent or contains an empty list, the
daemon sends no vocabulary context and behaves exactly as before.

Enter a replacement vocabulary interactively, one visible term per TTY line;
an empty line saves:

    .venv/bin/murmur-voice-daemon vocabulary

Alternatively, prepare a private UTF-8 file with one term per line and import
it without putting any term in the command arguments:

    chmod 600 /path/to/terms.txt
    .venv/bin/murmur-voice-daemon vocabulary --import-file /path/to/terms.txt

Both forms replace the complete list. An immediate empty line or an empty
import file clears it. The daemon accepts at most 200 terms of at most 64
Unicode characters each, trims surrounding whitespace, and performs stable
case-insensitive deduplication while retaining the first spelling. NUL, CR,
LF inside a term, unsafe permissions, symlinks, foreign ownership, invalid
UTF-8, and unexpected JSON fields are rejected.

The daemon loads the file once when `run` starts. Restart a foreground process,
or restart an installed user service after changing the list:

    systemctl --user restart murmur-ime-voice.service

Each ASR request then sends only those explicit terms to Volcengine using the
provider's documented `request.context` hotwords JSON string; empty lists omit
`context`. Terms never come from command arguments, clipboard, selected text,
typing history, documents, transcripts, or the Rime database, and they are
never written to logs. Provider-side handling follows the user's Volcengine
account policy.

## Optional explicit recognition corrections

For a phrase that is repeatedly recognized in the same wrong form, the native
settings window can store an explicit `recognized as` to `correct to` pair.
Pairs live separately in
`$XDG_CONFIG_HOME/murmur-ime/corrections.json` (or
`~/.config/murmur-ime/corrections.json`) with the same private ownership,
regular-file, `0700` directory, and `0600` file checks as the key and
vocabulary. Missing or empty corrections are valid defaults.

The daemon accepts at most 50 pairs, with at most 64 Unicode characters on
each side. It rejects empty values, control characters, unexpected fields,
and conflicting duplicate sources. Corrections are loaded once when `run`
starts, so changing them requires the same explicit service restart as the
vocabulary.

Each saved pair is sent with every new dictation in Volcengine's documented
`request.context.correct_words` map. Nothing is learned automatically, the
application never reads a previous transcript to create a pair, and the client
does not run a second local string replacement after ASR. Because Volcengine
does not publish request-level pair limits or matching-boundary guarantees,
this feature is labelled experimental and uses conservative local limits.

## Run and control

Start the daemon in the foreground:

    PYTHONNOUSERSITE=1 .venv/bin/murmur-voice-daemon run

From another process or a desktop shortcut:

    .venv/bin/murmur-voice-daemon toggle
    .venv/bin/murmur-voice-daemon start
    .venv/bin/murmur-voice-daemon stop
    .venv/bin/murmur-voice-daemon cancel
    .venv/bin/murmur-voice-daemon status

Commands use a bounded mode-0600 Unix socket strictly below
$XDG_RUNTIME_DIR; the daemon refuses missing, public, foreign-owned, or
out-of-tree runtime paths. Signals provide the same minimum control surface:

- SIGUSR1: start;
- SIGUSR2: stop;
- SIGHUP: cancel;
- SIGINT or SIGTERM: cancel and shut down.

The microphone starts only after the focused engine accepts Acquire. Network
work runs on a private asyncio thread in this separate daemon, never in the
IBus engine. Revisions strictly increase, stale-session callbacks are ignored,
and final, cancel, or error restores the previous IBus engine.

The local single-recording limit is 600 seconds. Reaching it performs a safe
stop and waits up to 20 seconds for Volcengine's explicit two-pass final. A
missing final cancels preedit; it never commits the latest live hypothesis.
Pending raw audio is independently bounded to 10 seconds to prevent unbounded
memory growth during network stalls. During the last 60 seconds, the status
command reports recording-limit-warning so a desktop integration can show a
visible warning; this headless MVP does not itself draw an indicator.

## Offline tests

    PYTHONPATH=. pytest

The tests use fake audio streams, ASR providers, D-Bus proxies, IBus command
runners, timers, and a private temporary Unix socket. They do not access a
real microphone, network endpoint, or IBus engine.

## Deliberate MVP limits

- No global-hotkey registration or floating recording indicator is included
  yet. A desktop shortcut can bind the implemented toggle command.
- The current murmur-voice prototype is voice-only. While temporarily
  selected, ordinary keys pass through, but stock ibus-rime does not compose
  Chinese. Combining Rime Ice and voice in one librime-capable engine remains
  a later engine milestone.
- The optional user installer manages a foreground-style systemd user service,
  but desktop D-Bus activation and a distribution-native package remain later
  milestones.
