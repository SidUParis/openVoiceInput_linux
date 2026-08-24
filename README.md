# Open Voice Input Linux

[简体中文快速上手](docs/README.zh-CN.md)

**A lightweight Linux/IBus voice-input preview with native caret-local preedit.**

Open Voice Input Linux 的目标是面向 Linux/IBus 的开源中文输入法：键盘输入
由 librime + 雾凇拼音负责，语音输入由火山引擎流式 ASR 负责。当前开发预览
已经让识别草稿直接显示在应用光标处，并在二遍识别完成后原位提交；键盘与
语音永久合并为同一个 librime 引擎仍是下一阶段。

Open Voice Input Linux is an early-stage Linux input method project. Its goal
is to keep the complete local Rime/Rime Ice typing experience while adding
native voice dictation directly inside the focused input field.

Canonical repository:
[github.com/SidUParis/openVoiceInput_linux](https://github.com/SidUParis/openVoiceInput_linux)

The voice path is faithful transcription, not generative writing: live ASR,
two-pass recognition, disfluency removal, punctuation, sentence segmentation,
and inverse text normalization. It does not turn a short instruction into an
email or otherwise invent content.

> **Current status:** the Python IBus inline-preedit prototype and a
> self-contained Volcengine voice daemon are implemented. During one recording
> they temporarily switch `current IBus engine → murmur-voice`, stream
> partial/final text over D-Bus, and restore that exact previous engine on
> final, cancel, or failure. The
> optional per-user systemd installation now covers both processes. The
> permanent combined Rime + voice engine and a distribution-native package are
> the next milestones.

## Why a new IBus engine?

The original Doubao Murmur desktop application did not own the active IBus input
context, so it needed a separate transcription window and clipboard-based
paste. The implemented Python prototype proves the replacement path by owning
an IBus context during dictation:

- ASR partial results use IBus preedit and appear at the caret.
- The optimized final result uses IBus commit and enters the application once.
- No black transcription window or synthetic paste is required.
- The target UI uses a small floating microphone button only for recording,
  finalizing, and error state. The standalone daemon currently exposes these
  states through its control command; the compatibility app has the visual
  button.

The target combined architecture is:

```mermaid
flowchart LR
    K["Keyboard"] --> E["Open Voice Input IBus Engine"]
    E <--> R["librime + Rime Ice data"]
    M["Microphone"] --> V["Voice daemon"]
    S["Settings UI + private config"] --> V
    V -->|"partial / final over D-Bus"| E
    E -->|"preedit / commit"| F["Focused input field"]
    V --> I["Small recording indicator"]
```

## Repository components

- `engine/` — implemented Python `murmur-voice` prototype with dynamic IBus
  registration, focus-safe preedit/final commit, and a temporary D-Bus bridge.
  It will be replaced by the production `ibus-rime`/librime-capable engine.
- `voice/` — implemented, isolated Python daemon for microphone capture,
  Volcengine `bigmodel_async`, bounded local control, and the Preedit1 bridge.
- `settings/` — settings UI documentation and entry-point notes. The bounded
  GTK4 implementation lives in `voice/murmur_voice/settings_app.py` and
  `settings_controller.py`; it manages a masked API key, explicit vocabulary,
  recognition corrections, and user-service status/control through the
  daemon's private storage. Secret Service migration remains later work.
- `scripts/` — user install/uninstall helpers and a deterministic GTK preedit
  demonstration that does not use a microphone or network.
- `docs/` — architecture, security rules, prototype operation, and D-Bus
  contracts.

## 0.x compatibility ABI

The public project name and repository are **Open Voice Input Linux** and
`openVoiceInput_linux`. To avoid disrupting existing prototype installations
and the verified local sidecar bridge, the 0.x line intentionally retains
these historical internal identifiers:

- IBus engine `murmur-voice` and component `org.murmur.IME.Engine`;
- D-Bus bridge `org.murmur.IME.Preedit1` at
  `/org/murmur/IME/Preedit1`;
- executables `murmur-ime-engine` and `murmur-voice-daemon`, plus user units
  `murmur-ime-engine.service` and `murmur-ime-voice.service`;
- Python package `murmur_ime_engine`, text domain `murmur-ime`, and user data
  directory `$XDG_DATA_HOME/murmur-ime`.

These names are compatibility ABI, not public branding to replace
mechanically. Any future runtime-identifier migration must support existing
installations explicitly.

Rime Ice data is not currently vendored. Open Voice Input Linux will use
isolated system and user data directories; it will never concurrently write
the live database used by stock `ibus-rime`. Existing preferences and user
data will be imported only through an explicit migration flow. Release
packages must not download code or data during installation.

## What works now

- Native cumulative preedit at the active application caret.
- One authoritative `Final` committed exactly once, without clipboard paste.
- Focus token, D-Bus sender, utterance ID, and strictly increasing revision
  checks; stale results are rejected.
- Cancellation on focus loss, input-context reset, engine disable, or sidecar
  disappearance.
- Voice acquisition disabled for password, PIN, private, fake, and clients
  without preedit support.
- Runtime IBus registration without root access or an IBus/desktop restart.
- Self-contained microphone/Volcengine daemon with a 10-minute recording cap,
  a 10-second pending-audio cap, and generation-safe late callback rejection.
- A private mode-0600 Unix control socket with `toggle`, `start`, `stop`,
  `cancel`, and `status` commands.
- Native GTK4 settings that never prefill the saved key and never restart a
  recording implicitly.

The temporary switch is a development bridge. While `murmur-voice` is active,
ordinary keys pass through and stock `ibus-rime` is not providing Chinese
keyboard composition. A true single input method still requires the planned
engine derived from `ibus-rime` and linked to librime.

## Try the prototype

Run the engine directly from the repository:

```bash
./engine/murmur-ime-engine
```

The deterministic visual demo and sender are documented in
[docs/prototype-demo.md](docs/prototype-demo.md). The complete runtime,
temporary bridge, safety, and optional per-user install instructions are in
[docs/python-preedit-prototype.md](docs/python-preedit-prototype.md).

To try the standalone daemon from source after installing the engine:

```bash
cd voice
python3 -m venv --system-site-packages .venv
PYTHONNOUSERSITE=1 .venv/bin/pip install -e '.[test]'
PYTHONNOUSERSITE=1 .venv/bin/murmur-voice-daemon configure
PYTHONNOUSERSITE=1 .venv/bin/murmur-voice-daemon run
```

The configuration prompt is masked and never accepts a key as a command-line
argument. Full daemon commands, permissions, limits, and dependencies are in
[voice/README.md](voice/README.md).

For a persistent per-user install from a connected source checkout, explicitly
opt into development dependency resolution:

```bash
./scripts/install-user.sh --allow-network
```

For a locked, no-network install, use the complete CI preview bundle described
below. Its `--wheelhouse` path is accepted only together with the matching
project wheel, runtime lock, SBOM, and hashes; an ad hoc `pip wheel` directory
is intentionally rejected.

After installation, open the native configuration window with:

```bash
~/.local/share/murmur-ime/open-voice-input-settings
```

The managed user install also adds an **Open Voice Input Linux** settings
launcher and project icon to the desktop application menu.

Saving a key never contacts the provider or interrupts a recording. Use the
window's explicit enable/start action after configuration.

CI also publishes a clean-source Ubuntu x86_64 preview archive with a locked,
hashed Python wheelhouse, deterministic CycloneDX SBOM, and complete SHA-256
manifest. Its offline installation and verification procedure is documented in
[docs/offline-preview.md](docs/offline-preview.md).

User-visible preview changes and known limitations are tracked in
[CHANGELOG.md](CHANGELOG.md).

Configuration, systemd behavior, upgrades, desktop shortcuts, troubleshooting,
and safe uninstall are documented in
[docs/user-service.md](docs/user-service.md). This is still a developer
preview: there is not yet a distribution-native package or built-in global
shortcut.

Run the current offline test suites with:

```bash
PYTHONPATH=engine python3 -m unittest discover -s engine/tests -v
PYTHONPATH=voice python3 -m pytest -q -p no:cacheprovider voice/tests
```

The current tree contains separate engine, installer/service, and voice-daemon
test suites. They use protocol fixtures and fake audio/D-Bus/systemd/IBus
boundaries; they do not call a real microphone, editable IBus context, package
index, or cloud account.

## Target MVP behavior

1. Select **Open Voice Input Linux** as the active IBus engine.
2. Type normally with Rime/Rime Ice, fully offline.
3. Press the configured shortcut (right `Alt` where available) or the
   microphone button to start dictation.
4. See live hypotheses at the current caret as IBus preedit.
5. Stop recording; the two-pass, smoothed result replaces the hypothesis and
   is committed exactly once.
6. Press `Esc` or change focus to cancel without committing late results.

Voice input will be disabled for password/private input contexts. An active
Rime composition must be committed or cancelled before voice recording starts.

## Provider scope

The first provider is Volcengine BigModel ASR 2.0 using
`bigmodel_async`, `enable_nonstream`, `enable_ddc`, `enable_itn`, and
`enable_punc`. After the user activates the matching speech service in their
own Volcengine project, the application needs only that user's API key. No
shared key is bundled. Provider interfaces remain separate from the IBus
engine so additional ASR services can be added later.

Microphone audio is streamed to Volcengine only during an active dictation and
usage is billed to the user's own Volcengine account. Cancelling prevents a
local commit but cannot retract audio already uploaded. Read
[docs/privacy.md](docs/privacy.md) before using voice input with sensitive
data. The reviewed security assumptions and accepted preview risks are in the
[threat model](docs/threat-model.md); bundled-code and dependency attribution
is recorded in the [licence audit](docs/license-audit.md) and
[NOTICE](NOTICE.md). Maintainers should follow the
[release process](docs/release-process.md) before publishing a preview.

The correction strategy is documented in
[docs/recognition-accuracy.md](docs/recognition-accuracy.md): provider-side
two-pass recognition first, then the explicit private vocabulary for names and
specialist terms, with optional user-confirmed wrong-to-canonical mappings sent
through Volcengine's documented `context.correct_words`. There is no local
post-hoc text replacement. A provider-managed hotword table remains a later
advanced option. The daemon never learns silently from clipboard, typing
history, transcripts, documents, or the Rime database.

## Development targets

- Ubuntu 24.04 and 26.04 with IBus
- X11 and Wayland applications that implement IBus preedit correctly
- Debian package first; Arch packaging afterwards

See [ROADMAP.md](ROADMAP.md) and [docs/architecture.md](docs/architecture.md).

## License and upstream projects

Open Voice Input Linux's new original code is licensed under GPL-3.0-only. It
is designed around `ibus-rime` (GPL-3.0-or-later) and `librime`
(BSD-3-Clause). Rime Ice is currently an external GPL-3.0-only project and is
not bundled in this repository. Imported files retain their upstream license
and copyright notices; voice-daemon code migrated from Doubao Murmur must
preserve its MIT notices.

See [NOTICE.md](NOTICE.md) for attribution and distribution boundaries and
[docs/dependencies.md](docs/dependencies.md) for the direct dependency and
licence inventory.

Open Voice Input Linux is an independent community project and is not
affiliated with Rime, Volcengine, ByteDance, or Doubao.
