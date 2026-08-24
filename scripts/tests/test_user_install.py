from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
INSTALLER = REPOSITORY / "scripts" / "install-user.sh"
UNINSTALLER = REPOSITORY / "scripts" / "uninstall-user.sh"


class InstallerHarness:
    def __init__(self, repository: Path = REPOSITORY) -> None:
        self.repository = repository.resolve()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.data = self.root / "xdg data %literal$"
        self.config = self.root / "xdg config"
        self.runtime = self.root / "runtime"
        self.fake_bin = self.root / "fake-bin"
        self.log = self.root / "calls.log"
        self.active = self.root / "active-services"
        self.enabled = self.root / "enabled-services"
        self.ibus_state = self.root / "ibus-engine"
        self.wheelhouse = self.root / "wheelhouse"
        for directory in (
            self.home,
            self.data,
            self.config,
            self.fake_bin,
            self.wheelhouse,
            self.runtime,
        ):
            directory.mkdir(parents=True)
        self.runtime.chmod(0o700)
        (self.wheelhouse / "murmur_ime_voice-0.1.0-py3-none-any.whl").touch()
        self.ibus_state.write_text("rime-test\n", encoding="utf-8")
        self._write_fakes()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(self.data),
                "XDG_CONFIG_HOME": str(self.config),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "PATH": f"{self.fake_bin}:/usr/bin:/bin",
                "MOCK_LOG": str(self.log),
                "MOCK_ACTIVE_FILE": str(self.active),
                "MOCK_ENABLED_FILE": str(self.enabled),
                "MOCK_IBUS_STATE": str(self.ibus_state),
                "MOCK_FAIL_ONCE_FILE": str(self.root / "failed-once"),
                "MOCK_VOICE_SOCKET": str(self.runtime / "murmur-ime/voice.sock"),
                "REAL_PYTHON": sys.executable,
            }
        )

    def close(self) -> None:
        self.temporary.cleanup()

    def run(self, script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *arguments],
            cwd=self.repository,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def configure_key_placeholder(self) -> Path:
        path = self.config / "murmur-ime" / "voice.json"
        path.parent.mkdir(mode=0o700)
        path.write_text('{"api_key":"test-placeholder"}\n', encoding="utf-8")
        path.chmod(0o600)
        return path

    def _write_executable(self, name: str, content: str) -> None:
        path = self.fake_bin / name
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
        path.chmod(0o755)

    def _write_fakes(self) -> None:
        self._write_executable(
            "python3",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ ${1:-} == */install_manifest.py \
              && ${2:-} == voice-process-count \
              && ${MOCK_MANAGED_VOICE_PROCESSES:-0} != 0 ]]; then
              printf '%s\n' "$MOCK_MANAGED_VOICE_PROCESSES"
              exit 0
            fi
            if [[ ${1:-} == */render_systemd_units.py ]]; then
              exec "$REAL_PYTHON" "$@"
            fi
            if [[ ${1:-} == -c ]]; then
              exit 0
            fi
            if [[ ${1:-} == -m && ${2:-} == venv ]]; then
              if [[ ${3:-} == --help ]]; then
                exit 0
              fi
              destination=${!#}
              printf 'python3-venv %s\n' "$destination" >>"$MOCK_LOG"
              mkdir -p "$destination/bin"
              cat >"$destination/bin/python" <<'SCRIPT'
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'venv-python %s\n' "$*" >>"$MOCK_LOG"
            printf 'venv-usersite %s\n' "${PYTHONNOUSERSITE:-}" >>"$MOCK_LOG"
            if [[ ${1:-} == -m && ${2:-} == pip ]]; then
              [[ ${MOCK_FAIL_PIP:-0} != 1 ]] || exit 47
              launcher=$(dirname -- "$0")/murmur-voice-daemon
              printf '%s\n' '#!/usr/bin/env bash' 'exit 0' >"$launcher"
              chmod 0755 "$launcher"
              site=$(dirname -- "$0")/../lib/python3.12/site-packages/murmur_voice
              mkdir -p "$site"
              printf '%s\n' '__version__ = "0.1.0"' >"$site/__init__.py"
              exit 0
            fi
            if [[ ${1:-} == -c ]]; then
              if (($# >= 3)); then
                code=$2
                config=${!#}
                if [[ $code == *load_vocabulary* ]]; then
                  [[ ${MOCK_VOCABULARY_INVALID:-0} != 1 ]] || exit 1
                elif [[ $code == *load_corrections* ]]; then
                  [[ ${MOCK_CORRECTIONS_INVALID:-0} != 1 ]] || exit 1
                else
                  [[ -f $config ]] || exit 1
                fi
              fi
              exit 0
            fi
            if [[ $* == *'-m murmur_voice shutdown'* ]]; then
              [[ ${MOCK_SHUTDOWN_FAIL:-0} != 1 ]] || exit 48
              rm -f -- "$MOCK_VOICE_SOCKET"
              exit 0
            fi
            exit 0
            SCRIPT
              chmod 0755 "$destination/bin/python"
              exit 0
            fi
            exec "$REAL_PYTHON" "$@"
            """,
        )
        self._write_executable(
            "install",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'install %s\n' "$*" >>"$MOCK_LOG"
            exec /usr/bin/install "$@"
            """,
        )
        self._write_executable(
            "systemctl",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'systemctl %s\n' "$*" >>"$MOCK_LOG"
            if [[ ${1:-} == --user ]]; then
              shift
            fi
            if [[ ${1:-} == show-environment ]]; then
              exit 0
            fi
            if [[ ${1:-} == show ]]; then
              # No vendor unit collision in the default harness.
              exit 0
            fi
            if [[ ${1:-} == is-active ]]; then
              service=${!#}
              [[ -f $MOCK_ACTIVE_FILE ]] && grep -Fqx -- "$service" "$MOCK_ACTIVE_FILE"
              exit
            fi
            if [[ ${1:-} == is-enabled ]]; then
              service=${!#}
              [[ -f $MOCK_ENABLED_FILE ]] && grep -Fqx -- "$service" "$MOCK_ENABLED_FILE"
              exit
            fi
            command=${1:-}
            service=${!#}
            if [[ $command == start || $command == restart ]]; then
              if [[ $service == murmur-ime-engine.service \
                && ${MOCK_FAIL_ENGINE_START_ONCE:-0} == 1 \
                && ! -f $MOCK_FAIL_ONCE_FILE ]]; then
                touch "$MOCK_FAIL_ONCE_FILE"
                exit 49
              fi
              touch "$MOCK_ACTIVE_FILE"
              if ! grep -Fqx -- "$service" "$MOCK_ACTIVE_FILE"; then
                printf '%s\n' "$service" >>"$MOCK_ACTIVE_FILE"
              fi
              if [[ $command == restart \
                && $service == murmur-ime-engine.service \
                && ${MOCK_CLEAR_IBUS_ON_ENGINE_RESTART:-0} == 1 ]]; then
                : >"$MOCK_IBUS_STATE"
              fi
            elif [[ $command == stop || $command == disable ]]; then
              was_active=false
              temporary="$MOCK_ACTIVE_FILE.tmp"
              if [[ -f $MOCK_ACTIVE_FILE ]]; then
                if grep -Fqx -- "$service" "$MOCK_ACTIVE_FILE"; then
                  was_active=true
                fi
                grep -Fvx -- "$service" "$MOCK_ACTIVE_FILE" >"$temporary" || true
                mv -f -- "$temporary" "$MOCK_ACTIVE_FILE"
              fi
              if [[ $command == stop \
                && $service == murmur-ime-engine.service \
                && $was_active == true \
                && ${MOCK_CLEAR_IBUS_ON_ENGINE_STOP:-0} == 1 ]]; then
                : >"$MOCK_IBUS_STATE"
              fi
            fi
            if [[ $command == enable ]]; then
              touch "$MOCK_ENABLED_FILE"
              if ! grep -Fqx -- "$service" "$MOCK_ENABLED_FILE"; then
                printf '%s\n' "$service" >>"$MOCK_ENABLED_FILE"
              fi
            elif [[ $command == disable ]]; then
              temporary="$MOCK_ENABLED_FILE.tmp"
              if [[ -f $MOCK_ENABLED_FILE ]]; then
                grep -Fvx -- "$service" "$MOCK_ENABLED_FILE" >"$temporary" || true
                mv -f -- "$temporary" "$MOCK_ENABLED_FILE"
              fi
            fi
            exit 0
            """,
        )
        self._write_executable(
            "ibus",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            [[ ${1:-} == engine ]] || exit 1
            if (($# == 1)); then
              cat "$MOCK_IBUS_STATE"
            else
              if [[ ${MOCK_FAIL_IBUS_SET:-0} == 1 ]]; then
                exit 50
              fi
              if [[ ${MOCK_FAIL_IBUS_SET_ONCE:-0} == 1 \
                && ! -f $MOCK_FAIL_ONCE_FILE ]]; then
                touch "$MOCK_FAIL_ONCE_FILE"
                exit 50
              fi
              printf '%s\n' "$2" >"$MOCK_IBUS_STATE"
              printf 'ibus-set %s\n' "$2" >>"$MOCK_LOG"
            fi
            """,
        )


class UserInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = InstallerHarness()

    def tearDown(self) -> None:
        self.harness.close()

    def test_default_never_falls_back_to_network_without_a_wheelhouse(self) -> None:
        if (REPOSITORY / "wheelhouse").exists():
            self.skipTest("the source bundle includes its default wheelhouse")

        result = self.harness.run(INSTALLER)

        self.assertEqual(result.returncode, 2)
        self.assertIn("No offline wheelhouse", result.stderr)
        self.assertFalse(
            any(line.startswith("venv-python -m pip") for line in self.harness.calls())
        )
        self.assertFalse((self.harness.data / "murmur-ime").exists())

    def test_offline_install_restarts_active_services_and_records_engine(self) -> None:
        config = self.harness.configure_key_placeholder()
        self.harness.active.write_text(
            "murmur-ime-engine.service\nmurmur-ime-voice.service\n",
            encoding="utf-8",
        )
        self.harness.enabled.write_text(
            "murmur-ime-engine.service\nmurmur-ime-voice.service\n",
            encoding="utf-8",
        )
        self.harness.environment["MOCK_CLEAR_IBUS_ON_ENGINE_RESTART"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.harness.calls()
        pip_call = next(line for line in calls if line.startswith("venv-python -m pip"))
        self.assertIn("--no-index", pip_call)
        self.assertIn(f"--find-links {self.harness.wheelhouse}", pip_call)
        self.assertIn("systemctl --user start murmur-ime-engine.service", calls)
        self.assertIn("systemctl --user start murmur-ime-voice.service", calls)
        self.assertIn("ibus-set rime-test", calls)
        self.assertTrue(
            all(
                line == "venv-usersite 1"
                for line in calls
                if line.startswith("venv-usersite ")
            )
        )
        voice_stop = calls.index("systemctl --user stop murmur-ime-voice.service")
        engine_stop = calls.index("systemctl --user stop murmur-ime-engine.service")
        pip_install = next(
            index
            for index, line in enumerate(calls)
            if line.startswith("venv-python -m pip")
        )
        engine_restart = calls.index("systemctl --user start murmur-ime-engine.service")
        voice_restart = calls.index("systemctl --user start murmur-ime-voice.service")
        self.assertLess(voice_stop, engine_stop)
        venv_create = next(
            index
            for index, line in enumerate(calls)
            if line.startswith("python3-venv ")
        )
        engine_copy = next(
            index
            for index, line in enumerate(calls)
            if line.startswith("install -m 0755 ")
            and "/engine/murmur-ime-engine " in line
        )
        self.assertLess(engine_copy, engine_stop)
        self.assertLess(venv_create, engine_stop)
        self.assertLess(venv_create, pip_install)
        self.assertLess(pip_install, engine_restart)
        self.assertLess(engine_restart, voice_restart)

        install_root = self.harness.data / "murmur-ime"
        state = install_root / "previous-ibus-engine"
        self.assertEqual(state.read_text(encoding="utf-8"), "rime-test\n")
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        manifest = install_root / "install-manifest.json"
        self.assertTrue(manifest.is_file())
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        self.assertTrue((install_root / "voice-venv/.murmur-ime-managed").is_file())
        unit = (
            self.harness.config / "systemd/user/murmur-ime-voice.service"
        ).read_text(encoding="utf-8")
        self.assertIn("UMask=0077", unit)
        self.assertNotIn("ConditionPathExists=", unit)
        self.assertIn("Environment=PYTHONNOUSERSITE=1", unit)
        self.assertIn("%%literal$$", unit)
        self.assertIn("--vocabulary", unit)
        self.assertIn(str(self.harness.config / "murmur-ime/vocabulary.json"), unit)
        self.assertIn("--corrections", unit)
        self.assertIn(str(self.harness.config / "murmur-ime/corrections.json"), unit)
        self.assertTrue(config.exists())
        self.assertFalse((self.harness.config / "ibus/rime").exists())
        launcher = (install_root / "murmur-voice-daemon").read_text(encoding="utf-8")
        self.assertIn("PYTHONNOUSERSITE=1", launcher)
        self.assertIn(' -s -m murmur_voice "$@"', launcher)
        settings_launcher = (install_root / "open-voice-input-settings").read_text(
            encoding="utf-8"
        )
        self.assertIn("PYTHONNOUSERSITE=1", settings_launcher)
        self.assertIn(" -s -m murmur_voice.settings_app", settings_launcher)

    def test_upgrade_does_not_overwrite_first_recorded_engine(self) -> None:
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        state = self.harness.data / "murmur-ime/previous-ibus-engine"
        self.assertEqual(state.read_text(encoding="utf-8"), "rime-test\n")
        stale_venv = self.harness.data / "murmur-ime/voice-venv/stale.txt"
        stale_venv.write_text("stale\n", encoding="utf-8")
        self.harness.ibus_state.write_text("anthy\n", encoding="utf-8")

        second = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(state.read_text(encoding="utf-8"), "rime-test\n")
        self.assertFalse(stale_venv.exists())

    def test_pip_failure_leaves_the_running_install_untouched(self) -> None:
        self.harness.configure_key_placeholder()
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        original_manifest = (install_root / "install-manifest.json").read_bytes()
        self.harness.log.write_text("", encoding="utf-8")
        self.harness.environment["MOCK_FAIL_PIP"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(install_root.stat().st_ino, original_inode)
        self.assertEqual(
            (install_root / "install-manifest.json").read_bytes(), original_manifest
        )
        self.assertEqual(
            self.harness.ibus_state.read_text(encoding="utf-8"), "rime-test\n"
        )
        self.assertFalse(any(" stop " in f" {line} " for line in self.harness.calls()))
        self.assertFalse(list(self.harness.data.glob(".murmur-ime.stage.*")))

    def test_engine_start_failure_rolls_back_root_units_services_and_ibus(self) -> None:
        self.harness.configure_key_placeholder()
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        self.harness.log.write_text("", encoding="utf-8")
        self.harness.environment["MOCK_FAIL_ENGINE_START_ONCE"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restoring the previous managed runtime", result.stderr)
        self.assertEqual(install_root.stat().st_ino, original_inode)
        self.assertEqual(
            self.harness.ibus_state.read_text(encoding="utf-8"), "rime-test\n"
        )
        active = self.harness.active.read_text(encoding="utf-8").splitlines()
        enabled = self.harness.enabled.read_text(encoding="utf-8").splitlines()
        self.assertCountEqual(
            active, ["murmur-ime-engine.service", "murmur-ime-voice.service"]
        )
        self.assertCountEqual(
            enabled, ["murmur-ime-engine.service", "murmur-ime-voice.service"]
        )

    def test_ibus_restore_failure_rolls_back_after_atomic_swap(self) -> None:
        self.harness.configure_key_placeholder()
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        self.harness.log.write_text("", encoding="utf-8")
        self.harness.environment["MOCK_CLEAR_IBUS_ON_ENGINE_STOP"] = "1"
        self.harness.environment["MOCK_FAIL_IBUS_SET_ONCE"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(install_root.stat().st_ino, original_inode)
        self.assertEqual(
            self.harness.ibus_state.read_text(encoding="utf-8"), "rime-test\n"
        )
        self.assertIn("restoring the previous managed runtime", result.stderr)

    def test_install_and_uninstall_refuse_unmanifested_same_name_files(self) -> None:
        install_root = self.harness.data / "murmur-ime"
        install_root.mkdir(mode=0o700)
        launcher = install_root / "murmur-ime-engine"
        launcher.write_text("foreign\n", encoding="utf-8")
        unit_dir = self.harness.config / "systemd/user"
        unit_dir.mkdir(parents=True)
        engine_unit = unit_dir / "murmur-ime-engine.service"
        voice_unit = unit_dir / "murmur-ime-voice.service"
        engine_unit.write_text("foreign engine\n", encoding="utf-8")
        voice_unit.write_text("foreign voice\n", encoding="utf-8")

        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        uninstall_result = self.harness.run(UNINSTALLER)

        self.assertEqual(install_result.returncode, 2)
        self.assertEqual(uninstall_result.returncode, 2)
        self.assertEqual(launcher.read_text(encoding="utf-8"), "foreign\n")
        self.assertEqual(engine_unit.read_text(encoding="utf-8"), "foreign engine\n")
        self.assertEqual(voice_unit.read_text(encoding="utf-8"), "foreign voice\n")

    def test_install_rejects_group_writable_or_overlapping_xdg_roots(self) -> None:
        self.harness.data.chmod(0o770)
        writable = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(writable.returncode, 2)
        self.assertFalse((self.harness.data / "murmur-ime").exists())

        self.harness.data.chmod(0o755)
        nested_config = self.harness.data / "nested-config"
        nested_config.mkdir()
        self.harness.environment["XDG_CONFIG_HOME"] = str(nested_config)
        overlapping = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(overlapping.returncode, 2)
        self.assertIn("must not be nested", overlapping.stderr)

    def test_upgrade_rejects_a_linked_runtime_cache(self) -> None:
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        package = self.harness.data / "murmur-ime/murmur_ime_engine"
        (package / "__pycache__").symlink_to(
            self.harness.home, target_is_directory=True
        )

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("ownership manifest", result.stderr)

    def test_missing_key_installs_but_does_not_enable_or_start_voice(self) -> None:
        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.harness.calls()
        self.assertIn("systemctl --user disable murmur-ime-voice.service", calls)
        self.assertNotIn("systemctl --user enable murmur-ime-voice.service", calls)
        self.assertNotIn("systemctl --user start murmur-ime-voice.service", calls)
        self.assertNotIn("systemctl --user restart murmur-ime-voice.service", calls)
        self.assertIn("configure --config", result.stdout)
        self.assertIn("enable --now murmur-ime-voice.service", result.stdout)

    def test_invalid_vocabulary_does_not_enable_or_start_voice(self) -> None:
        self.harness.configure_key_placeholder()
        self.harness.environment["MOCK_VOCABULARY_INVALID"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.harness.calls()
        self.assertIn("systemctl --user disable murmur-ime-voice.service", calls)
        self.assertNotIn("systemctl --user enable murmur-ime-voice.service", calls)
        self.assertNotIn("systemctl --user start murmur-ime-voice.service", calls)
        self.assertNotIn("configure --config", result.stdout)
        self.assertIn("vocabulary --vocabulary", result.stdout)
        self.assertIn("enable --now murmur-ime-voice.service", result.stdout)

    def test_invalid_corrections_do_not_enable_or_start_voice(self) -> None:
        self.harness.configure_key_placeholder()
        self.harness.environment["MOCK_CORRECTIONS_INVALID"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.harness.calls()
        self.assertIn("systemctl --user disable murmur-ime-voice.service", calls)
        self.assertNotIn("systemctl --user enable murmur-ime-voice.service", calls)
        self.assertNotIn("systemctl --user start murmur-ime-voice.service", calls)
        self.assertNotIn("configure --config", result.stdout)
        self.assertIn("recognition corrections", result.stdout)
        self.assertIn("enable --now murmur-ime-voice.service", result.stdout)

    def test_uninstall_restores_only_recorded_engine_and_retains_key(self) -> None:
        config = self.harness.configure_key_placeholder()
        corrections = config.parent / "corrections.json"
        corrections.write_text(
            '{"version":1,"pairs":[{"wrong":"test-wrong","canonical":"TestRight"}]}\n',
            encoding="utf-8",
        )
        corrections.chmod(0o600)
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        self.harness.ibus_state.write_text("murmur-voice\n", encoding="utf-8")
        runtime_dir = self.harness.runtime / "murmur-ime"
        runtime_dir.mkdir(mode=0o700)
        socket_path = runtime_dir / "voice.sock"
        control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        control_socket.bind(str(socket_path))
        control_socket.close()
        socket_path.chmod(0o600)
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(UNINSTALLER)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.harness.ibus_state.read_text(encoding="utf-8"), "rime-test\n"
        )
        calls = self.harness.calls()
        self.assertEqual(
            [line for line in calls if line.startswith("ibus-set")],
            ["ibus-set rime-test"],
        )
        voice_stop = calls.index("systemctl --user stop murmur-ime-voice.service")
        engine_stop = calls.index("systemctl --user stop murmur-ime-engine.service")
        self.assertLess(voice_stop, engine_stop)
        self.assertTrue(config.exists())
        self.assertTrue(corrections.exists())
        self.assertFalse((self.harness.data / "murmur-ime/voice-venv").exists())
        self.assertFalse((self.harness.config / "ibus/rime").exists())
        self.assertFalse(socket_path.exists())
        self.assertFalse(runtime_dir.exists())

    def test_uninstall_does_not_switch_an_unrelated_current_engine(self) -> None:
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        self.harness.ibus_state.write_text("anthy\n", encoding="utf-8")
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(UNINSTALLER)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.harness.ibus_state.read_text(encoding="utf-8"), "anthy\n")
        self.assertFalse(
            any(line.startswith("ibus-set ") for line in self.harness.calls())
        )

    def test_uninstall_restores_an_unrelated_engine_if_dbus_clears_it(self) -> None:
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        self.harness.ibus_state.write_text("anthy\n", encoding="utf-8")
        self.harness.environment["MOCK_CLEAR_IBUS_ON_ENGINE_STOP"] = "1"
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(UNINSTALLER)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.harness.ibus_state.read_text(encoding="utf-8"), "anthy\n")
        self.assertEqual(
            [line for line in self.harness.calls() if line.startswith("ibus-set ")],
            ["ibus-set anthy"],
        )

    def test_uninstall_rejects_a_tampered_multiline_engine_record(self) -> None:
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        state = self.harness.data / "murmur-ime/previous-ibus-engine"
        state.write_text("rime-test\nanother-engine\n", encoding="utf-8")
        state.chmod(0o600)
        self.harness.ibus_state.write_text("murmur-voice\n", encoding="utf-8")
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(UNINSTALLER)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(
            self.harness.ibus_state.read_text(encoding="utf-8"),
            "murmur-voice\n",
        )
        self.assertFalse(
            any(line.startswith("ibus-set ") for line in self.harness.calls())
        )
        self.assertIn("ownership manifest", result.stderr)
        self.assertTrue((self.harness.data / "murmur-ime").is_dir())

    def test_uninstall_hard_fails_when_murmur_voice_cannot_be_restored(self) -> None:
        self.harness.configure_key_placeholder()
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        self.harness.ibus_state.write_text("murmur-voice\n", encoding="utf-8")
        self.harness.environment["MOCK_FAIL_IBUS_SET"] = "1"

        result = self.harness.run(UNINSTALLER)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no files were removed", result.stderr)
        self.assertTrue((self.harness.data / "murmur-ime").is_dir())
        self.assertTrue(
            (self.harness.config / "systemd/user/murmur-ime-engine.service").is_file()
        )

    def test_uninstall_shuts_down_a_live_private_socket(self) -> None:
        self.harness.configure_key_placeholder()
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        runtime_dir = self.harness.runtime / "murmur-ime"
        runtime_dir.mkdir(mode=0o700)
        socket_path = runtime_dir / "voice.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen(1)
        socket_path.chmod(0o600)
        try:
            result = self.harness.run(UNINSTALLER)
        finally:
            listener.close()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(socket_path.exists())
        self.assertFalse((self.harness.data / "murmur-ime").exists())

    def test_uninstall_refuses_a_live_daemon_that_will_not_shutdown(self) -> None:
        self.harness.configure_key_placeholder()
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        runtime_dir = self.harness.runtime / "murmur-ime"
        runtime_dir.mkdir(mode=0o700)
        socket_path = runtime_dir / "voice.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen(1)
        socket_path.chmod(0o600)
        self.harness.environment["MOCK_SHUTDOWN_FAIL"] = "1"
        try:
            result = self.harness.run(UNINSTALLER)
        finally:
            listener.close()
            socket_path.unlink(missing_ok=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refused a controlled shutdown", result.stderr)
        self.assertTrue((self.harness.data / "murmur-ime").is_dir())

    def test_uninstall_refuses_a_custom_socket_foreground_daemon(self) -> None:
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        self.harness.environment["MOCK_MANAGED_VOICE_PROCESSES"] = "1"

        result = self.harness.run(UNINSTALLER)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreground voice daemon is still running", result.stderr)
        self.assertTrue((self.harness.data / "murmur-ime").is_dir())

    def test_uninstall_requires_xdg_runtime_dir(self) -> None:
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        self.harness.environment.pop("XDG_RUNTIME_DIR")

        result = self.harness.run(UNINSTALLER)

        self.assertEqual(result.returncode, 2)
        self.assertIn("XDG_RUNTIME_DIR is required", result.stderr)
        self.assertTrue((self.harness.data / "murmur-ime").is_dir())

    def test_network_resolution_requires_explicit_flag(self) -> None:
        result = self.harness.run(INSTALLER, "--allow-network")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Developer mode", result.stderr)
        pip_call = next(
            line
            for line in self.harness.calls()
            if line.startswith("venv-python -m pip")
        )
        self.assertNotIn("--no-index", pip_call)
        self.assertIn(str(REPOSITORY / "voice"), pip_call)


if __name__ == "__main__":
    unittest.main()
