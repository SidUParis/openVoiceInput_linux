# Offline Ubuntu x86_64 preview bundle

Each successful CI run publishes a short-lived Ubuntu x86_64 preview artifact.
The archive contains only the committed source tree, a project wheel, every
Python runtime-dependency wheel, `BUNDLE-INFO`, and a complete `SHA256SUMS`.
Ignored files, local configuration, credentials, bytecode, and Git metadata
are excluded by construction.

The bundle does not contain Ubuntu `.deb` packages. Install the documented
system prerequisites (`ibus`, `gir1.2-ibus-1.0`, `gir1.2-gtk-4.0`,
`python3-gi`, `python3-venv`, and `libportaudio2`) before moving the bundle to
an offline machine. Choose an artifact whose Ubuntu and Python tags match that
machine.

After downloading both files from the same CI artifact:

```bash
sha256sum --check openVoiceInput_linux-preview-*.tar.gz.sha256
tar -xzf openVoiceInput_linux-preview-*.tar.gz
cd openVoiceInput_linux-preview-*/
sha256sum --check SHA256SUMS
./scripts/install-user.sh
```

The default installer discovers the bundled `./wheelhouse` and passes
`--no-index` to pip. An explicit equivalent is:

```bash
./scripts/install-user.sh --wheelhouse "$PWD/wheelhouse"
```

CI verifies both forms after unpacking with isolated systemd and IBus mocks,
then performs a real install of the Python wheel set in a fresh virtual
environment with package indexes disabled. The SHA256 files detect accidental
corruption; they are not a cryptographic statement of publisher identity.

Maintainers can build the same artifact only from a clean committed tree on
Ubuntu x86_64. Building the wheelhouse may use the network; installing it does
not:

```bash
python3 -m venv .venv
PREVIEW_PYTHON=.venv/bin/python \
  ./scripts/build-preview-bundle.sh --output-dir dist
```
