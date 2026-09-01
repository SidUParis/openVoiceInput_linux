# Open Voice Input Linux voice daemon

This directory contains the first self-contained voice-daemon MVP. It no
longer imports or runs a separate Doubao Murmur checkout. The foreground
daemon captures 16 kHz mono PCM, sends it through the user-selected online ASR
client, and sends provider-supported cumulative hypotheses plus one
authoritative final to the existing org.murmur.IME.Preedit1 engine. It briefly
observes bounded same-focus corrections and then restores the previously
selected IBus engine.

There is no transcription window and no clipboard/paste fallback. Transcript
text appears as native IBus preedit at the focused caret and is never written
to logs. An explicitly enabled local collector is the only intentional
audio/provider-final record described below.

## Runtime dependencies

- Python 3.11 or newer;
- IBus and PyGObject/Gio (python3-gi on Debian/Ubuntu);
- PortAudio (libportaudio2; development headers may be needed to build
  sounddevice);
- Python packages sounddevice 0.4.6 or newer but below 1, and websockets 13
  or newer but below 18.
- GTK4 introspection data when using the bundled native settings window
  (`gir1.2-gtk-4.0` on Ubuntu).
- Optional `libusb-1.0` (`libusb-1.0-0` on Ubuntu) for the bounded DJI Mic Mini
  2 transmitter-link probe. When unavailable, link state is unknown: DJI is
  not promoted ahead of a known alternative, while an already-default unique
  DJI remains only a final continuity fallback when no non-DJI or recoverable
  input can be selected.

From this directory, install into a virtual environment that can see the
system PyGObject package:

    python3 -m venv --system-site-packages .venv
    PYTHONNOUSERSITE=1 .venv/bin/pip install -e '.[test]'

## Private provider configuration

The legacy fallback configuration contains only the Volcengine API key. A
version-2 private configuration additionally selects one reviewed provider and
fixed model. Provider endpoints are not user-controlled. Volcengine's 2.0
resource ID, two-pass recognition, DDC, ITN, punctuation, sentence settings,
and 200 ms chunks continue to use reviewed defaults.

Each user must first activate the matching speech service in their own provider
account. Audio usage, quota, billing, regional processing, retention, and
account policy belong to the selected provider and that account; the project
never bundles a shared key. Volcengine is the default and only path validated
with a real key on the maintainer workstation; Qwen and OpenAI remain
experimental. Cancelling stops local input but cannot retract microphone audio
already sent during the active dictation.

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
corrections, choose the optional local-collection destination, and explicitly
enable/start or disable/stop the user service. Saving alone never contacts the
selected provider or restarts an active recording. All local choices are read
again at the next dictation without a service restart.
After the service is explicitly disabled and stopped, a two-step destructive
button can remove only the local private key file; it never contacts or revokes
the provider credential itself.

A separately delivered compatibility Flatpak can act as a controller/indicator
for this daemon's bounded command surface. It does not contain or own this
repository's microphone capture, provider client, DJI selection, or dataset
writer; those remain in the installed host daemon.

## Faithful and clean terminal output

The private `$XDG_CONFIG_HOME/murmur-ime/output-style.json` is strict schema v1
with mode `faithful` or `clean`, stored as a user-owned `0600` file below a
`0700` directory. Missing means faithful. The daemon freezes the setting once
at utterance start, so saving during capture affects only the next dictation.

Streaming partials are always raw. Faithful mode commits the authoritative
provider final unchanged. Clean mode applies the bounded local deterministic
deletion-only cleaner only at the provider terminal event. It makes no LLM or
extra network request, never inserts text or substitutes a term, number, or
letter case, and falls back to raw on any error, oversize,
excessive/non-replayable edits, or all-content removal. If it changes output,
the automatic observation is consumed without adaptive extraction; explicit
review still learns only from raw provider text versus user-entered spoken
verbatim.

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

The daemon safely reloads the file before every new dictation. A change affects
the next dictation without restarting the foreground process or installed user
service; an invalid replacement fails closed before microphone/provider use.

Each ASR request then sends only those explicit terms through the selected
provider's reviewed context mechanism. Volcengine receives its documented
`request.context` hotwords JSON string; Qwen receives request vocabulary and
OpenAI receives a prompt. Terms never come from command arguments, clipboard, selected text,
typing history, documents, transcripts, or the Rime database, and they are
never written to logs. Provider-side handling follows the selected service's
terms and the user's account configuration.

## Configurable microphone priority

The native settings window stores the user's complete four-class ordering in
`$XDG_CONFIG_HOME/murmur-ime/microphone-priority.json` (or
`~/.config/murmur-ime/microphone-priority.json`). The file uses the same
private ownership, regular-file, `0700` directory, and `0600` file checks as
the key and recognition settings. A missing file selects the documented
deterministic compatibility initialization without writing anything; this is
not a recommendation for the user's own equipment. An existing invalid file
fails the next dictation with `microphone-policy-invalid` until the user
explicitly repairs it in settings.

The ordering is reloaded before every new dictation and never changes an
already open stream. See the microphone-routing section below for category,
fallback, and DJI link-state semantics.

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
and conflicting duplicate sources. Corrections are safely reloaded before each
new dictation, so changing them does not require a service restart.

Each saved pair is compiled into the selected provider's bounded context
mechanism: Volcengine receives `request.context.correct_words`, while Qwen and
OpenAI receive vocabulary/prompt context without a promise of exact
replacement. After a nonempty authoritative final, the
current alpha enables a bounded five-second adaptive observation by default.
If the same focused field supplies trustworthy IBus surrounding text, one
high-confidence replacement can activate immediately; multiple independent
replacements become inactive review candidates. The private version-2
`adaptive-corrections.json` contains only bounded pairs, classification,
state, support, and a transcript-free recent result. Version-1 ledgers migrate
safely. The settings window shows counts, reasons, review candidates, explicit
confirmation, and a manual whole-utterance fallback for applications without
surrounding-text support; only derived bounded pairs are persisted. The client
never reads the clipboard or global keys and never runs a second local string
replacement after ASR. Because the configured provider
does not publish request-level pair limits or matching-boundary guarantees,
this feature is labelled experimental and uses conservative local limits.

## Optional local WAV/JSON collection

Collection is disabled by default. The native settings window requires the
user to select an existing absolute local or mounted folder before enabling
it. Saving initializes or reopens `openvoiceinput-dataset-v1` below that
folder and writes the private choice to
`$XDG_CONFIG_HOME/murmur-ime/data-collection.json`. It does not contact the
provider, start a recording, or restart the daemon; each new dictation reloads
the choice.

For an enabled utterance, audio chunks successfully submitted to the ASR client
are copied as exact 16 kHz mono signed 16-bit PCM into bounded memory. They are
offered to the background writer only after a nonempty authoritative provider
final was accepted by the focused IBus client. Cancel, failure, final rejection,
no final, and incomplete audio publish nothing.

Each atomically published `utterances/<utterance_id>/` contains `audio.wav` and
schema-v3 `record.json` with identifiers, UTC time, explicit-opt-in consent,
audio format/frame counts and hashes, provider/model identity, microphone
selection/actual-route provenance, three label roles, and a separate delivery
audit. `provider_final` is
`teacher-unreviewed`: it is a pseudo-label, not
ground truth. `spoken_verbatim` and `preferred_output` are both null/unreviewed
until a separate human-review workflow exists.

`delivery.text` is the exact inserted result with
`machine-derived-unreviewed` status. It stores mode, processor/version,
content-free outcome, and replayable deletion edits without replacing raw
provider text or filling either human label.

New records use schema v3. Existing v1/v2 records are not rewritten; all three
versions can coexist below the unchanged `openvoiceinput-dataset-v1` marker. The v2
`microphone` object stores category, a non-unique privacy-safe fingerprint,
selection provenance, DJI link state at selection, and bounded actual Pulse
source-output route transitions. It stores no raw Pulse source name, USB serial,
Bluetooth address, or custom device label. See
[`docs/personal-asr-data-plan.md`](../docs/personal-asr-data-plan.md) for the
reader migration rule.

Schema v2 also adds numeric `audio.quality` diagnostics computed after final
acceptance by the background writer: overall/first-second clipping and zero
fractions, RMS/peak dBFS, normalized DC offset, and sample count. This is only
future filtering evidence. It runs outside the callback/start path, rejects no
record, and changes no PCM sample.

The separate usage index advances to schema v2 and declares
`character_count_basis=delivered-text`; dashboard readers continue accepting
schema-v1 summaries with their prior raw-provider count semantics.

After post-commit learning finishes, an enabled collection may add an atomic,
append-only `feedback/<utterance_id>/<event_id>.json` event with bounded
correction pairs, classifications, decisions, counts, and the result code.
Every utterance directory remains exactly `audio.wav + record.json`; the writer
never changes that base record, does not retain surrounding input-field text,
and writes no feedback event when collection is disabled.

The active recorder is bounded by the same 600-second audio limit, and the
writer queue holds at most two completed records. WAV encoding, hashing, fsync,
and atomic rename run outside the audio callback and session lock. A full or
failed writer sets a fixed optional status and does not block normal dictation.
Disabling or changing the destination prevents older unpublished queued/staged
items from later becoming published; already published records remain.

This is best-effort direct-to-selected-folder storage. There is no fallback
local spool: if a mount stalls or disappears, the hidden staging item may be
cleaned up and that unpublished record may be lost. Normal daemon shutdown
gives accepted queued records up to 10 seconds to drain inside systemd's
30-second total stop budget; it does not wait indefinitely.

The collector does not authenticate to or mount Orange, upload to Google Drive,
train, fine-tune, or distil a model, implement review/deletion tooling, or add
application-level encryption. A compatible user-mounted Orange/SSHFS path is
still a selectable filesystem folder; complete records can be backed up to
Drive separately. The selected filesystem determines effective visibility and
at-rest protection. See
[the remote-storage guide](../docs/remote-dataset-storage.md). Uninstall
preserves the private setting and every dataset below a user-selected folder.

## Run and control

Start the daemon in the foreground:

    PYTHONNOUSERSITE=1 .venv/bin/murmur-voice-daemon run

From another process or a desktop shortcut:

    .venv/bin/murmur-voice-daemon toggle
    .venv/bin/murmur-voice-daemon start
    .venv/bin/murmur-voice-daemon stop
    .venv/bin/murmur-voice-daemon press
    .venv/bin/murmur-voice-daemon release
    .venv/bin/murmur-voice-daemon cancel
    .venv/bin/murmur-voice-daemon status
    .venv/bin/murmur-voice-daemon adaptive-status

Commands use a bounded mode-0600 Unix socket strictly below
$XDG_RUNTIME_DIR; the daemon refuses missing, public, foreign-owned, or
out-of-tree runtime paths. Signals provide the same minimum control surface:

- SIGUSR1: start;
- SIGUSR2: stop;
- SIGHUP: cancel;
- SIGINT or SIGTERM: cancel and shut down.

`interaction.json` selects `toggle` (the default) or `push_to_talk`. The
daemon reloads it on each new press. In push-to-talk mode a successful press
owns the session it started and release stops only that session; repeated
key-down is ignored, an accidental hold shorter than the configured threshold
is cancelled, Escape/cancel clears ownership, and a bounded watchdog stops a
session whose release was lost. A press during the adaptive-observation lease
atomically finishes that lease before starting the next utterance.

The package exposes these press/release commands as a minimal integration
interface but deliberately does not choose or monitor a physical key. A
normal desktop activation shortcut works for `toggle`. Push-to-talk requires
an integration which can emit distinct key-down and key-up events. Generic
Wayland global shortcuts do not reliably expose release; the daemon neither
claims otherwise nor scans all evdev devices. X11-specific or compositor-
specific helpers remain external integrations until they can be permissioned
and tested independently.

The microphone starts only after the focused engine accepts Acquire. Network
work runs on a private asyncio thread in this separate daemon, never in the
IBus engine. Before that thread connects, every explicit start performs a
fresh input-device preflight and sends one invisible empty preedit heartbeat;
focus lost during discovery therefore fails with `preedit-lost` before network
or capture starts. On PulseAudio/PipeWire systems with `pactl`, the daemon
validates one exact `pulse` PortAudio endpoint and binds its own recording
stream to an exact physical source; a stale monitor default is not used or
directly replaced. If Bluetooth teardown left a card in an output-only profile
with no real source, the daemon may activate the unique highest-priority
profile that adds one input while preserving the active output profile and
sink count, then bind the recovered source only to its own stream. Activating a
card profile can still cause host PulseAudio/PipeWire policy to recompute the
global default. The daemon never calls `set-default-source`, changes microphone
mute, or changes volume. Ambiguous or failed preflight returns
`microphone-unavailable` without contacting the ASR provider; reconnect/select
an input and start again. The next start always enumerates again, so no daemon
restart is required after a device change.

The private `microphone-priority.json` file stores one complete order for DJI,
headset, other external, and built-in microphone categories. A missing file
uses the deterministic compatibility initialization
`DJI > headset > other external > built-in`; this is not a product
recommendation. An existing invalid or unsafe file rejects the next start
before preedit, provider, USB, profile, or microphone activity. The native
settings window can repair it by explicitly saving a complete allowlisted
order. Each new dictation reloads the file and re-enumerates sources. Within
one category, an exact saved source wins, then the live system default, then a
unique candidate; unresolved same-category ambiguity falls through to the
next category rather than being guessed.

When exactly one DJI Mic Mini 2 source is present, preflight performs a bounded
read-only transmitter-link probe. Proven online makes DJI eligible at its saved
position; proven offline excludes the receiver that remains enumerated but
silent. Unknown status (including busy, inaccessible, malformed, or unavailable
`libusb`) does not promote DJI ahead of a known alternative. A unique DJI source
that is already the system default may be retained only as a last-resort path
when no non-DJI or recoverable input can be selected. Bluetooth counts as a
headset microphone only when an input source already exists (for example,
HSP/HFP); the daemon does not switch an A2DP playback profile. The probe neither
logs nor retains USB frames. Selection never changes a playback sink or requests
a system-wide default-source change. The source is fixed once the stream opens;
there is no mid-utterance handoff, and the next dictation checks again.

The `pactl` discovery/profile transaction has a three-second forward bound and
a seven-second reserved rollback window (a ten-second hard bound in total).
The synchronous provider/capture gate has a 35-second logical deadline,
enforced at safe checkpoints, rather than a hard control-response ceiling. The
control client waits 50 seconds so bounded acquisition, preflight, and cleanup
can return a specific error; 50 seconds exceeds the 29 + 10 + 8 second
pessimistic budget. PortAudio does not expose a portable
cancellation primitive for blocked native device discovery or stream
construction/start: the daemon checks the deadline at the next safe checkpoint
and closes a late-opened stream, but a defective native backend can still delay
the control reply.

Without `pactl`, the daemon makes no global audio-profile change and accepts
only an inspectable PortAudio hardware input that supports the required
format; otherwise it fails closed instead of trusting a generic default that
could resolve to an output monitor. Revisions strictly increase, stale-session
callbacks are ignored, and final, cancel, or error restores the previous IBus
engine.

The local single-recording limit is 600 seconds. Reaching it performs a safe
stop and waits up to 20 seconds for the selected provider's authoritative
final. A missing final cancels preedit; it never commits the latest live
hypothesis.
Pending raw audio is independently bounded to 10 seconds to prevent unbounded
memory growth during network stalls. During the last 60 seconds, the status
command reports recording-limit-warning so a desktop integration can show a
visible warning; this headless MVP does not itself draw an indicator.

## Offline tests

    PYTHONPATH=. pytest

The tests use fake audio streams, ASR providers, D-Bus proxies, IBus command
runners, DJI USB probes, dataset writers, timers, and a private temporary Unix
socket. They do not access a real microphone, network endpoint, mounted user
dataset, or IBus engine.

## Deliberate MVP limits

- No global-hotkey registration or floating recording indicator is included.
  A desktop shortcut can bind `toggle`; press/release is available for tools
  that genuinely provide both key edges.
- The current murmur-voice prototype is voice-only. While temporarily
  selected, ordinary keys pass through, but stock ibus-rime does not compose
  Chinese. Combining Rime Ice and voice in one librime-capable engine remains
  a later engine milestone.
- The optional user installer manages a foreground-style systemd user service,
  but desktop D-Bus activation and a distribution-native package remain later
  milestones.
- Local collection has no application-owned Orange authentication/mounting or
  first-party resumable transport, review/delete interface, automatic label
  validation, fallback spool, or model-training pipeline. A user-mounted
  filesystem path is allowed; a published provider-final pair remains an
  unreviewed pseudo-labelled record.
