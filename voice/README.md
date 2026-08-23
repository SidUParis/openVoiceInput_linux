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

From this directory, install into a virtual environment that can see the
system PyGObject package:

    python3 -m venv --system-site-packages .venv
    .venv/bin/pip install -e '.[test]'

## Private key-only configuration

The fallback configuration contains only the Volcengine API key. Provider
endpoint, 2.0 resource ID, two-pass recognition, DDC, ITN, punctuation,
sentence settings, and 200 ms chunks use reviewed defaults.

Each user must first activate the matching BigModel streaming speech service
in their own Volcengine project. Audio usage, quota, and billing belong to that
account; the project never bundles a shared key. Cancelling stops local input
but cannot retract microphone audio already sent during the active dictation.

Run the masked, confirmation-based prompt:

    .venv/bin/murmur-voice-daemon configure

It atomically writes $XDG_CONFIG_HOME/murmur-ime/voice.json (or
~/.config/murmur-ime/voice.json) with directory mode 0700 and file mode 0600.
There is intentionally no API-key command-line argument, so the key does not
enter shell history. config.example.json contains only a non-working
placeholder.

## Run and control

Start the daemon in the foreground:

    .venv/bin/murmur-voice-daemon run

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
- No systemd or D-Bus activation and no package installation is performed in
  this phase.
