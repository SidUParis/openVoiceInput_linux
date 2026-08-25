# Per-user engine and voice service

The source-tree installer provides a reversible development installation of
both the inline-preedit engine and the standalone voice daemon. It uses only
the current user's XDG data/config directories and systemd user manager. It
does not use root, modify the IBus daemon, or read or write
`~/.config/ibus/rime`.

## Prerequisites

On Ubuntu, install the system runtime first:

```bash
sudo apt install ibus gir1.2-ibus-1.0 gir1.2-gtk-4.0 python3-gi python3-venv libportaudio2 util-linux
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

Installed files use these XDG-relative locations:

- code and managed virtual environment:
  `$XDG_DATA_HOME/murmur-ime/`;
- private ownership manifest:
  `$XDG_DATA_HOME/murmur-ime/install-manifest.json`;
- user units: `$XDG_CONFIG_HOME/systemd/user/`;
- API key: `$XDG_CONFIG_HOME/murmur-ime/voice.json`;
- optional vocabulary: `$XDG_CONFIG_HOME/murmur-ime/vocabulary.json`;
- optional corrections: `$XDG_CONFIG_HOME/murmur-ime/corrections.json`.

If `XDG_DATA_HOME` or `XDG_CONFIG_HOME` is unset, the standard
`~/.local/share` and `~/.config` defaults apply. The generated service records
the resolved config, vocabulary, and correction paths, so a custom XDG config
root is used consistently even if it is absent from the systemd manager's
environment.

The engine service starts after installation and is enabled for subsequent
graphical logins. Its unit is attached to `graphical-session.target`, rather
than the earlier user-manager `default.target`, so IBus and the graphical
session environment are available first. A temporary IBus startup delay is
retried every two seconds without exhausting the unit's start-rate limit. The
voice unit is installed but is enabled and started only when the key file,
optional vocabulary, and optional corrections already pass the daemon's
ownership, permission, schema, and content checks. Configure a missing or
invalid key in the GTK4 settings window:

```bash
~/.local/share/murmur-ime/open-voice-input-settings
```

The managed installation also registers an **Open Voice Input Linux** settings
entry in the current user's desktop application menu. The desktop file and
icon are covered by the ownership manifest and removed transactionally during
uninstall.

The stored key is never prefilled or revealed. Saving clears the password
field, does not contact Volcengine, and does not restart an active recording.
Use the explicit **Enable and start service** button after configuration.
To remove the local key, first use **Disable and stop**, then use the two-step
**Clear saved key** action. This deletes only the validated private local file;
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

After replacing an existing key, restart the idle service so the new in-memory
configuration is used:

```bash
systemctl --user restart murmur-ime-voice.service
```

Optional user-confirmed recognition corrections are edited in the native
settings window. They are private `recognized as` to `correct to` pairs, loaded
once at service start, and sent in Volcengine's documented provider-side
`context.correct_words` map. They are not learned from transcripts and are not
applied as a local string replacement. Saving either vocabulary or corrections
does not restart the service automatically.

The optional explicit vocabulary can be edited separately and then loaded by
restarting the idle daemon:

```bash
~/.local/share/murmur-ime/murmur-voice-daemon vocabulary \
  --vocabulary ~/.config/murmur-ime/vocabulary.json
systemctl --user restart murmur-ime-voice.service
```

The service may run at login, but it is idle: it does not open the microphone
or connect to Volcengine until an explicit `start` or `toggle` request. Bind a
desktop shortcut to:

```bash
~/.local/share/murmur-ime/murmur-voice-daemon toggle
```

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
contain lifecycle/error classes, never keys, vocabulary, corrections, or
dictated text.

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

## Uninstall

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
uninstall can roll back. The private API-key, vocabulary, and correction files
are retained, and no Rime program or user database is touched.
