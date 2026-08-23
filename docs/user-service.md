# Per-user engine and voice service

The source-tree installer provides a reversible development installation of
both the inline-preedit engine and the standalone voice daemon. It uses only
the current user's XDG data/config directories and systemd user manager. It
does not use root, modify the IBus daemon, or read or write
`~/.config/ibus/rime`.

## Prerequisites

On Ubuntu, install the system runtime first:

```bash
sudo apt install ibus gir1.2-ibus-1.0 python3-gi python3-venv libportaudio2
```

The normal installer never silently downloads Python packages. A release or
offline test bundle should contain a wheelhouse with exactly one
`murmur_ime_voice-*.whl` plus wheels for all declared and transitive runtime
dependencies. On a connected development machine, creating that directory is
an explicit network/build action:

```bash
python3 -m venv .venv
.venv/bin/pip wheel --wheel-dir wheelhouse ./voice
./scripts/install-user.sh --wheelhouse ./wheelhouse
```

For source development only, the opt-in form below lets pip use the configured
package indexes. The warning and flag make the network boundary explicit:

```bash
./scripts/install-user.sh --allow-network
```

The installer rejects relative XDG roots, linked/unmanaged destination
environments, incomplete offline wheelhouses, and unknown options. During an
upgrade it stops an active voice service first and the engine second, replaces
and temporarily disables both units before replacing their project-owned code,
then re-enables, restarts, and verifies every service that should be active.
An interrupted replacement therefore cannot auto-start a half-updated runtime.
Stale Python modules and the managed voice environment are rebuilt.

## First configuration and startup

Installed files use these XDG-relative locations:

- code and managed virtual environment:
  `$XDG_DATA_HOME/murmur-ime/`;
- user units: `$XDG_CONFIG_HOME/systemd/user/`;
- API key: `$XDG_CONFIG_HOME/murmur-ime/voice.json`;
- optional vocabulary: `$XDG_CONFIG_HOME/murmur-ime/vocabulary.json`.

If `XDG_DATA_HOME` or `XDG_CONFIG_HOME` is unset, the standard
`~/.local/share` and `~/.config` defaults apply. The generated service records
the resolved config and vocabulary paths, so a custom XDG config root is used
consistently even if it is absent from the systemd manager's environment.

The engine service starts after installation. The voice unit is installed but
is enabled and started only when the key file and optional vocabulary already
pass the daemon's ownership, permission, schema, and content checks. Configure
a missing or invalid key using the exact command printed by the installer, for
example:

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
contain lifecycle/error classes, never keys, vocabulary, or dictated text.

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

Installation records the first valid, non-voice IBus engine in a private state
file. Uninstall stops voice before the engine. Only if the current engine is
still exactly `murmur-voice` does it restore that validated recorded engine and
verify the result; it never hard-codes `rime` or changes an unrelated current
engine. The units, managed virtual environment, engine code, and state file are
removed. A leftover current-user-owned Unix control socket is removed only
after the voice service is confirmed inactive. The private API-key and
vocabulary files are deliberately retained, and no Rime program or user
database is touched.
