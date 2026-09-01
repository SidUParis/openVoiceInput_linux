# Ubuntu 24.04 `.deb` package

`scripts/build-deb.sh` creates the `amd64` package entirely from one clean Git
commit plus the four hash-locked runtime wheels. It performs no network access.
The wheelhouse from the matching offline preview bundle is accepted; its
project wheel is ignored because application code is exported directly from
the selected Git commit.

The builder enforces the product's lightweight-client boundary: the compressed
`.deb` must stay at or below 5 MiB and its declared `Installed-Size` at or below
10 MiB. These are regression ceilings, not the current measured size; alpha.4
is about 404 KiB compressed and 2.7 MiB installed. Distribution dependencies
which APT may need to add are outside the package's own size.

The Ubuntu build host needs `dpkg-deb`, `desktop-file-utils`, and `appstream`
for archive construction and metadata validation. These checks do not access
the network.

```bash
./scripts/build-deb.sh \
  --ref HEAD \
  --wheelhouse /absolute/path/to/wheelhouse \
  --output-dir dist
```

The PEP 440 application version is mapped to an ordered Debian version. For
example, `0.1.0a4` becomes `0.1.0~alpha4-1`; a later stable `0.1.0-1` sorts
above every alpha. The exact 40-character source commit and source timestamp
are recorded in package `BUILD-INFO` and the deterministic CycloneDX SBOM.

Install or upgrade without extracting the archive:

```bash
sudo apt install ./dist/open-voice-input-linux_*_amd64.deb
```

Open **Open Voice Input Linux** from the desktop application menu, save the
Volcengine key locally, and explicitly enable the voice service. The IBus
engine is a package-owned graphical-session dependency; the voice service is
not enabled until the user requests it.

Remove the packaged code with:

```bash
sudo apt remove open-voice-input-linux
```

The package intentionally owns no file under `~/.config/murmur-ime` and no
dataset directory. Install, upgrade, remove, and purge therefore neither read
nor alter the API key, vocabulary, corrections, interaction mode, output style,
microphone policy, collection choice, or user-selected training data. The read-only
pre-install shadow check tests fixed legacy unit, desktop-entry, and
ownership-manifest pathnames but
never opens a user file. The remaining minimal maintainer scripts call only
Ubuntu's `deb-systemd-invoke --user`: install reloads running user managers and
starts the microphone-free engine; upgrade restarts only enabled/active project
units; remove stops both units before their launchers disappear; and post-remove
reloads user managers. They do not enumerate, read, create, or delete user
configuration. A user choice to enable the voice unit is retained across an
upgrade; removal leaves no running project daemon or restart loop.

The older source-tree installer creates higher-precedence units under
`~/.config/systemd/user`, a desktop entry under `~/.local/share/applications`,
and code under `~/.local/share/murmur-ime`. The package pre-install check
refuses install or upgrade when it finds these standard legacy paths, instead
of claiming a new package version while the desktop still runs old code. Run
`scripts/uninstall-user.sh` from the current or matching preview bundle as the
desktop user, then retry the `.deb` command. That trusted uninstall preserves
all private configuration and external datasets. Users with nonstandard XDG
roots should run the same uninstall first and confirm the loaded units with:

```bash
systemctl --user show murmur-ime-engine.service murmur-ime-voice.service \
  --property=FragmentPath
```
