# User services and installation layouts

Both install layouts run the engine and voice daemon as the desktop user. They
share the same private XDG configuration and external dataset schema, but they
must not be installed on top of one another because per-user unit files take
precedence over package-installed user units under `/usr/lib/systemd/user`.

## Standalone Ubuntu `.deb` (normal installation)

The Ubuntu 24.04 `amd64` package installs root-owned application code and
locked Python dependencies below `/usr/lib/open-voice-input-linux/python`,
global commands in `/usr/bin`, and systemd user units in
`/usr/lib/systemd/user`. Install a downloaded, verified artifact with:

```bash
sudo apt install ./open-voice-input-linux_*_amd64.deb
```

The package-owned graphical-session link starts the microphone-free IBus
engine. It does not enable the voice service or open a microphone. Launch
**Open Voice Input Linux** from the desktop menu (or run
`open-voice-input-settings`), save the provider key locally, and choose the
explicit enable/start action. A terminal alternative is:

```bash
murmur-voice-daemon configure \
  --config "${XDG_CONFIG_HOME:-$HOME/.config}/murmur-ime/voice.json"
systemctl --user enable --now murmur-ime-voice.service
```

The `.deb` still does not bundle a global shortcut. Bind the desktop shortcut
of your choice to `murmur-voice-daemon toggle`. Read-only diagnostics use the
same units and global launcher:

```bash
systemctl --user status murmur-ime-engine.service murmur-ime-voice.service
journalctl --user -u murmur-ime-voice.service
murmur-voice-daemon status
```

Package pre-installation refuses a detected legacy source-preview installation
at the standard per-user unit, desktop-entry, or ownership-manifest paths. Run
`scripts/uninstall-user.sh` from the current or matching verified preview
bundle as that desktop user, then retry the package. This removes only the
legacy managed code and units; it preserves private configuration and external
datasets. Users who installed the source preview below nonstandard XDG roots
should run the same trusted uninstaller first and confirm the loaded unit paths:

```bash
systemctl --user show murmur-ime-engine.service murmur-ime-voice.service \
  --property=FragmentPath
```

Remove the packaged application with:

```bash
sudo apt remove open-voice-input-linux
```

Removal stops both project units before removing their launchers. Remove and
purge do not claim `$XDG_CONFIG_HOME/murmur-ime` or any selected dataset, so
keys, vocabulary, correction state, microphone/collection choices, and
collected records remain until the user deliberately deletes them.

## Source/offline per-user installer

The source-tree installer provides a reversible development installation of
both the inline-preedit engine and the standalone voice daemon. It uses only
the current user's XDG data/config directories and systemd user manager. It
does not use root, modify the IBus daemon, or read or write
`~/.config/ibus/rime`.

## Prerequisites

On Ubuntu, install the system runtime first:

```bash
sudo apt install ibus gir1.2-ibus-1.0 gir1.2-gtk-4.0 python3-gi python3-venv libportaudio2 libusb-1.0-0 pulseaudio-utils util-linux
```

The normal installer never silently downloads Python packages. A no-network
install must come from the complete CI preview bundle: its project wheel,
runtime dependency wheels, target lock, deterministic SBOM, and hashes are
verified as one set before the installer creates a virtual environment. An ad
hoc directory produced by `pip wheel` is intentionally rejected. See
[offline-preview.md](offline-preview.md) for artifact checksums, host tags, and
the verified installation procedure.

For source development only, the opt-in form below lets pip use the configured
package indexes. The warning and flag make the network boundary explicit:

```bash
./scripts/install-user.sh --allow-network
```

The installer rejects relative or overlapping XDG roots, linked/unowned
destinations, incomplete wheelhouses, unknown same-name services, and unknown
options. It first builds and import-checks the complete replacement in a
same-filesystem staging directory while the installed version keeps running.
Only then does it briefly stop voice and engine, atomically exchange the
project-owned runtime and units, restore the exact IBus engine, and verify the
services. ERR, interruption, start failure, or IBus failure rolls the old
runtime, units, enabled/active state, and engine back. A private ownership
manifest prevents upgrades or uninstalls from claiming unrelated same-name
files. Before an upgrade replaces the managed tree, it requests a controlled
shutdown through the fixed private socket and refuses to continue while any
exactly matched managed foreground daemon (including one using a custom socket)
remains. The final check runs after the fixed launcher path is atomically moved
to quarantine, closing the normal launch race before replacement. A failed
commit likewise isolates the replacement root first; if that version has
already started, both runtime quarantines are retained and services remain
stopped for recovery. These checks do not signal an argv-only match or read the
API-key file.

## First configuration and startup

For this source/offline layout, installed files use these XDG-relative
locations:

- code and managed virtual environment:
  `$XDG_DATA_HOME/murmur-ime/`;
- private ownership manifest:
  `$XDG_DATA_HOME/murmur-ime/install-manifest.json`;
- user units: `$XDG_CONFIG_HOME/systemd/user/`;
- API key: `$XDG_CONFIG_HOME/murmur-ime/voice.json`;
- optional vocabulary: `$XDG_CONFIG_HOME/murmur-ime/vocabulary.json`;
- optional manual corrections: `$XDG_CONFIG_HOME/murmur-ime/corrections.json`;
- automatically maintained adaptive correction memory:
  `$XDG_CONFIG_HOME/murmur-ime/adaptive-corrections.json`;
- optional local-collection choice:
  `$XDG_CONFIG_HOME/murmur-ime/data-collection.json`;
- optional microphone-priority policy:
  `$XDG_CONFIG_HOME/murmur-ime/microphone-priority.json`;
- terminal output style:
  `$XDG_CONFIG_HOME/murmur-ime/output-style.json`;
- final-delivery target (caret by default; explicit clipboard for remote desktop):
  `$XDG_CONFIG_HOME/murmur-ime/output-target.json`;
- optional collected records: `openvoiceinput-dataset-v1/` below the existing
  local or mounted directory explicitly selected by the user.

If `XDG_DATA_HOME` or `XDG_CONFIG_HOME` is unset, the standard
`~/.local/share` and `~/.config` defaults apply. The generated service records
the resolved config, vocabulary, manual-correction, adaptive-correction,
local-collection, microphone-priority, interaction, output-style, and
output-target paths, so a custom XDG config root is
used consistently even if it is absent from the systemd manager's environment.

The engine service starts after installation and is enabled for subsequent
graphical logins. Its unit is attached to `graphical-session.target`, rather
than the earlier user-manager `default.target`, so IBus and the graphical
session environment are available first. A temporary IBus startup delay is
retried every two seconds without exhausting the unit's start-rate limit. The
voice unit is installed but is enabled and started only when the key file,
optional vocabulary, manual corrections, and an existing adaptive ledger
already pass the daemon's ownership, permission, schema, and content checks.
Local collection is not part of this readiness gate: a missing, disabled, or
invalid optional collection setting must not prevent ordinary dictation.
Configure a missing or invalid key in the GTK4 settings window:

```bash
~/.local/share/murmur-ime/open-voice-input-settings
```

The managed installation also registers an **Open Voice Input Linux** settings
entry in the current user's desktop application menu. The desktop file and
icon are covered by the ownership manifest and removed transactionally during
uninstall.

The stored key is never prefilled or revealed. Saving clears the password
field, does not contact Volcengine, and does not restart an active recording.
Use the explicit **启用并启动** (enable and start) button after configuration.
To remove the local key, first use **停用并停止（取消当前听写）**, then use the
two-step **清除已保存的 Key…** action. This deletes only the validated private local file;
it does not revoke the key in the provider console.

The masked terminal flow remains available, using the exact command printed
by the installer, for example:

```bash
~/.local/share/murmur-ime/murmur-voice-daemon configure \
  --config ~/.config/murmur-ime/voice.json
systemctl --user enable --now murmur-ime-voice.service
```

The prompt is masked and the key never appears in an argument. The installer
does not enable the voice unit until the config validates, and exit status 2
is excluded from restart, so a later configuration error cannot create a
restart loop.

Optional user-confirmed recognition corrections are edited in the native
settings window. They are private `recognized as` to `correct to` pairs and are
sent as best-effort Volcengine guidance in the raw WebSocket
`request.corpus.context.correct_words` map. Manual and explicitly confirmed
active pairs are also enforced once by a bounded, boundary-aware local terminal
stage; live partials remain untouched.

The optional explicit vocabulary can be edited separately:

```bash
~/.local/share/murmur-ime/murmur-voice-daemon vocabulary \
  --vocabulary ~/.config/murmur-ime/vocabulary.json
```

The key, vocabulary, manual corrections, adaptive ledger, output style,
output target, and local-collection choice are reloaded before every new
dictation. Missing
`output-style.json` means faithful delivery; its strict v1 file is private
`0600` below a `0700` directory. Saving a change never mutates
an active recording; the next start/idle toggle uses it without a daemon
restart. A newly invalid key/vocabulary/correction file fails that start before
microphone/provider use rather than falling back to stale in-memory values.
Collection remains an optional side path: its invalid setting or unavailable
destination reports a fixed collection status and leaves normal dictation
available.

Missing `output-target.json` means caret delivery. Its strict v1 file is
private `0600` below the same `0700` directory. Explicit clipboard mode
preflights `xclip`/`wl-copy` before audio/provider use, copies only the
authoritative final, and never auto-pastes. It intentionally disables remote
partials and surrounding-text automatic learning; see
[remote-desktop.md](remote-desktop.md).

### Faithful and clean final output

The cloud-recognition page offers faithful (default) and clean output. The
daemon freezes this choice at utterance start. Partials stay raw; only the
authoritative terminal final is eligible for the local bounded deletion-only
cleaner. It makes no LLM or extra network request and never substitutes terms,
numbers, or letter case. Unsafe, oversized, excessive, emptying, or failed
cleanup returns the raw final without blocking input. If cleanup deletes
anything, automatic observation is consumed with
`postprocessed-output-not-safe-for-asr-learning`; explicit review continues to
use raw provider text.

### Optional filesystem training-data collection

Collection is off by default. In the settings window, select an existing
absolute local or mounted folder, enable WAV + raw recognition + actual
delivery retention, and choose **保存数据留存设置**. Saving initializes or
reopens `openvoiceinput-dataset-v1` below the selected folder. It does not
contact Volcengine, start capture, or restart the service; the next dictation
reads the choice.

For each enabled utterance whose authoritative provider final is either
accepted by the focused IBus context or successfully written to the explicitly
armed clipboard target, the collector publishes one
`utterances/<utterance_id>/` directory containing the exact 16 kHz mono signed
16-bit `audio.wav` and versioned `record.json`. The provider result is stored as
`provider_final` with `teacher-unreviewed` status. `spoken_verbatim` and
`preferred_output` are both null/unreviewed: the current pair is a future
review candidate, not a gold label or distillation-ready sample.

Schema v5 separately stores actual `delivery.text` as
`machine-derived-unreviewed`, the frozen style and target, and an ordered,
replayable confirmed-correction plus identity/clean pipeline. It never
overwrites raw provider text.

The v1 utterance directory stays an immutable two-file pair. After it is
published, the writer atomically adds a separate private
schema-v2 `usage/<utterance_id>.json` summary containing only timestamp,
duration and a delivered-text non-whitespace character count. Schema-v1
summaries remain readable. The settings dashboard scans this bounded
index in a background worker and never opens `record.json` or audio.

Capture copies bounded PCM into memory; WAV encoding, hashing, syncing, and
atomic publication run in a bounded background writer. A full/unavailable
writer or invalid destination reports `data-collection-failed` or
`data-collection-unavailable` but never blocks normal dictation. Cancelled,
failed, final-rejected, and no-final sessions produce no published record.

The collector does not authenticate to or mount Orange, upload to Google Drive,
train or fine-tune a model, or add application-level encryption. A compatible
Orange/SSHFS filesystem which the user mounted separately is still an existing
filesystem path and can be selected. Google Drive should receive only complete
local/Orange records through a separate asynchronous rclone backup. The
selected filesystem controls effective visibility, backup, and at-rest
protection.
Disabling or changing the destination immediately applies to the next
utterance; once the save returns, older queued/staged records cannot publish.
Already published records remain until the user deletes them directly.

Storage is best-effort direct to the selected folder; there is no fallback
local spool. Normal service shutdown gives its writer 10 seconds to drain
accepted queued records within systemd's 30-second total stop budget. A stalled
or unmounted destination may leave or remove a hidden staging directory and
lose that unpublished record. Published `utterances/` entries remain.
Connection commands, disconnect recovery, Google OAuth requirements, and the
non-destructive rclone workflow are documented in
[remote-dataset-storage.md](remote-dataset-storage.md).

### Adaptive correction observation

This observation is enabled by default in the current alpha after
a nonempty authoritative final; the settings window does not yet provide a
disable switch. It is event-driven and does not poll application text.

After an authoritative final is committed, the daemon enters a bounded
observation state for at most five seconds. If the same focused application
supports IBus surrounding text and the user makes exactly one replacement
inside the committed span, the daemon can append a bounded learned pair to
`adaptive-corrections.json`. It rejects insertions, deletions, multiple edits,
sentence polishing, changes outside the original span, focus/private-context
changes, timeout, and unsupported surrounding text. The next `toggle` finishes
observation early and proceeds to the next dictation.

Chromium's GTK input-method path does not currently provide this surrounding
text: its current
[`InputMethodContextImplGtk::SetSurroundingText`](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/ui/gtk/input_method_context_impl_gtk.cc)
implementation is empty. When the engine reports that capability as unavailable, Murmur consumes
the empty observation and restores the previous IBus engine immediately. It
does not wait five seconds or weaken the focus/private-input checks.

For an explicit cross-application review, run:

```bash
open-voice-input-settings --review-last
```

The daemon keeps only the latest accepted raw `provider_final`, delivered text,
and utterance ID in memory for ten minutes. A new accepted result replaces it and daemon shutdown
clears it. The host settings process reads it through
`$XDG_RUNTIME_DIR/murmur-ime-private/review.sock`; that parent is `0700`, the
socket is `0600`, and it is deliberately outside the separately delivered
Flatpak controller's `murmur-ime` runtime mount. No transcript is placed in an
argument, log, or persistent file. Provider original and delivery are read-only
in the window; the editable spoken field starts from raw provider text. Only an
explicit user-edited verbatim statement is submitted to the
existing bounded correction extractor. Filler removal, rewriting, and
polishing are not verbatim ASR labels.

Submission returns the review ID and verbatim text to that same host-only
socket. The daemon rejects an expired ID or one replaced by a newer accepted
final. It alone calls the adaptive updater and, when collection is enabled,
queues `result.as_feedback_document()` through the existing
`DataCollectionRuntime.record_feedback()` writer under the same utterance ID.
The review is consumed after the ledger update, including when optional
sidecar enqueue fails, so a duplicate cannot increase support twice. The UI
distinguishes collection disabled, enqueue failure, and queued-but-not-yet-
published feedback. Asynchronous queue acceptance is never described as final
storage success.

The older direct settings-controller helper remains an explicit offline/manual
fallback for administering correction rules without a running daemon. It does
not claim an utterance ID or training-data sidecar and is never selected as an
automatic fallback from the ID-bound review window.

The changed block is limited to three lexical tokens on each side and must
meet a conservative similarity floor. A one-character source must expand
through an unchanged adjacent lexical token; case-only spelling corrections
remain eligible.

The ledger stores only wrong/canonical strings of at most 64 Unicode characters
per side, state, and support count. It stores no separate transcript or
surrounding snapshot, audio, timestamp, or document context, and the observer
does not use clipboard, AT-SPI, or global keyboard monitoring. Manual
`corrections.json` entries take priority. Conflicted, overlapping, and cyclic
adaptive entries, including source/canonical cascades, are suppressed; the
combined provider request is still capped at 50 correction pairs.

During this five-second transition window `murmur-voice` remains the selected
IBus engine. Ordinary direct key events pass through to the application, but
the previous Rime/IBus engine is unavailable until observation completes or is
ended early. This limitation goes away only with the planned combined librime
engine.

The service may run at login, but it is idle: it does not open the microphone
or connect to Volcengine until an explicit `start` or `toggle` request. Bind a
desktop shortcut to:

```bash
~/.local/share/murmur-ime/murmur-voice-daemon toggle
```

The native settings page can instead select `push_to_talk`. An integration
which has real key edges sends:

```bash
~/.local/share/murmur-ime/murmur-voice-daemon press
~/.local/share/murmur-ime/murmur-voice-daemon release
```

The key name is intentionally not stored by the daemon: the user chooses it
in the desktop, keyboard firmware, or accessibility tool which owns the key
event. Repeat key-down is idempotent. A release below the configured minimum
hold cancels the utterance, and a missing release is bounded by the configured
watchdog before a normal stop. `cancel` (including an Escape binding) clears
held-key ownership. A press received during `observing` first finishes the
observation and then starts the next utterance atomically.

GNOME/KDE activation shortcuts can invoke `toggle` on X11 and Wayland, but a
generic Wayland global shortcut does not guarantee a key-up callback. Do not
configure push-to-talk unless the selected integration can invoke both
commands. Open Voice Input does not add the user to `input`, scan all evdev
devices, or advertise an X11 listener as a Wayland solution.

## Status and troubleshooting

```bash
systemctl --user status murmur-ime-engine.service murmur-ime-voice.service
journalctl --user -u murmur-ime-voice.service
~/.local/share/murmur-ime/murmur-voice-daemon status
```

The unit creates `%t/murmur-ime` as mode `0700`; the daemon creates its control
socket there as mode `0600`. Its `UMask=0077`, address-family restriction, and
no-new-privileges policy still allow the user-session D-Bus/IBus and
PipeWire/PulseAudio Unix sockets plus IPv4/IPv6 access to the configured ASR
provider. The installed launcher disables Python's per-user site-packages so
unrelated packages under `~/.local` cannot alter this managed runtime. Logs
contain lifecycle/error classes, never keys, vocabulary, corrections, selected
dataset paths, USB status frames, audio, or dictated text.

Each `start`/idle `toggle` rechecks the current microphone before provider
connection. On a PulseAudio-compatible PipeWire desktop, the daemon uses the
normally installed `pactl` command to reject monitor sources and route its own
stream around a stale default after a device disconnect. It validates one exact
PortAudio `pulse` endpoint up front, then binds that stream to the selected
physical source without calling `set-default-source`. In the narrow case where
no real source exists because one card retained an output-only profile, it can
add the matching input only when the active output, sink count, availability,
and a unique highest-priority candidate make that change unambiguous. Profile
activation may still cause host audio policy to recompute the global default.
The daemon does not unmute a source or change volume. The fixed status code
`microphone-unavailable` means recovery was absent, ambiguous, or failed; use
the desktop sound panel to select/unmute an input, reconnect the device if
needed, and start dictation again. Restarting the daemon is not required.

Before every new dictation, the daemon reloads the private microphone policy and
re-enumerates sources. The complete priority covers DJI, headset, other external,
and built-in categories; a missing file uses `DJI > headset > other external >
built-in`. The first usable, unambiguous category wins. Within it, an exact saved
source wins, then the live system default, then a unique candidate. Same-category
ambiguity falls through rather than being guessed. An existing invalid/unsafe
file returns `microphone-policy-invalid` before preedit, provider, profile, USB,
or capture activity; explicitly saving the displayed complete order repairs it.

When exactly one DJI Mic Mini 2 source is visible, the daemon also performs a
bounded link-state probe. Proven online makes DJI eligible at its saved position;
proven offline excludes the silent receiver. Unknown status (for example, a
busy/inaccessible receiver or unavailable `libusb`) does not promote DJI ahead
of known alternatives; an already-default unique DJI is only a last resort when
no non-DJI/recoverable input can be selected. This path is app-scoped: it never
changes the playback sink or calls `set-default-source`. The selected stream is
not handed off mid-utterance; device changes are picked up on the next `start`
or idle `toggle`.

The `pactl` discovery/profile transaction is bounded to three forward seconds,
with seven more seconds reserved for conservative rollback (ten seconds hard
total). After discovery, the daemon sends one empty, invisible preedit
heartbeat before provider connection;
if focus moved meanwhile, start fails with `preedit-lost` and neither network
audio nor microphone capture begins. A 35-second logical deadline gates
provider/capture opening and is enforced at safe checkpoints; it is not a hard
control-response ceiling. The local control client allows 50 seconds for the
pessimistic 29-second acquisition, 10-second preflight, and 8-second cleanup
path. Native PortAudio device discovery and stream-open calls have no portable
cancellation API; the daemon checks at the next safe checkpoint and closes a
late-opened stream, but a broken backend may delay the command response.

Useful read-only diagnostics are:

```bash
pactl get-default-source
pactl list short sources
```

A source ending in `.monitor` records speaker output, not a microphone. On a
minimal system without `pactl`, automatic PulseAudio/PipeWire profile repair
is unavailable and the daemon conservatively accepts only an inspectable
PortAudio hardware input.

Before a recording temporarily selects `murmur-voice`, the daemon atomically
records the actual prior engine in that private runtime directory. Normal
final/cancel clears it; startup and the systemd `ExecStopPost` helper restore a
record left by a crash or forced kill. The helper never guesses `rime` and does
not override a newer real engine selected explicitly by the user.

On unusual desktop sessions, confirm that the systemd user manager has the
graphical variables needed by IBus:

```bash
systemctl --user show-environment
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY
systemctl --user restart murmur-ime-engine.service murmur-ime-voice.service
```

Do not import API keys through the systemd environment.

## Remove the source/offline per-user installation

```bash
./scripts/uninstall-user.sh
```

The technical preview does not copy its uninstaller into the managed install
root. Keep the extracted preview directory (or download and verify the exact
same preview bundle again) so this command remains available.

Installation records the first valid, non-voice IBus engine in a private state
file. Uninstall verifies the ownership manifest before stopping anything,
requests a controlled shutdown from a foreground daemon, and refuses to remove
files while any managed daemon remains. Only if the current engine is exactly
`murmur-voice` does it restore and verify the recorded engine; failure is a hard
stop, never a warning followed by deletion. The managed runtime and units move
to same-filesystem quarantine before final deletion so an interrupted
uninstall can roll back. The private API-key, vocabulary, manual-correction,
adaptive-correction, `interaction.json`, `output-style.json`, `output-target.json`,
`microphone-priority.json`, and `data-collection.json` files are retained. Every
`openvoiceinput-dataset-v1` below a user-selected folder is outside installer
ownership and is never removed. No Rime program or user database is touched.
