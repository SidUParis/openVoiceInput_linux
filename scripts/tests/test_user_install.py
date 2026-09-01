from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import venv
from pathlib import Path

from scripts.install_manifest import (
    ManifestError,
    managed_voice_process_count,
    move_no_clobber,
)

REPOSITORY = Path(__file__).resolve().parents[2]
INSTALLER = REPOSITORY / "scripts" / "install-user.sh"
UNINSTALLER = REPOSITORY / "scripts" / "uninstall-user.sh"
DESKTOP_ENTRY_RELATIVE = Path(
    "applications/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop"
)
SETTINGS_ICON_RELATIVE = Path(
    "icons/hicolor/scalable/apps/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg"
)


def is_venv_pip_call(line: str) -> bool:
    return line.startswith("venv-python ") and " -m pip" in line


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
            directory.chmod(0o700)
        self.wheels = tuple(
            self.wheelhouse / filename
            for filename in (
                "cffi-2.1.1-py3-none-any.whl",
                "murmur_ime_voice-0.1.0a7-py3-none-any.whl",
                "pycparser-3.0-py3-none-any.whl",
                "sounddevice-0.5.6-py3-none-any.whl",
                "websockets-17.0.1-py3-none-any.whl",
            )
        )
        for wheel in self.wheels:
            wheel.touch()
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
                "MOCK_DAEMON_RELOAD_FAILED_FILE": str(
                    self.root / "daemon-reload-failed"
                ),
                "MOCK_RACE_INJECTED_FILE": str(self.root / "race-injected"),
                "MOCK_ROLLBACK_RACE_INJECTED_FILE": str(
                    self.root / "rollback-race-injected"
                ),
                "MOCK_REMOVE_PRIVATE_TREE_FAILED_FILE": str(
                    self.root / "remove-private-tree-failed"
                ),
                "MOCK_CLEANUP_REPLACED_FILE": str(self.root / "cleanup-replaced"),
                "MOCK_CLEANUP_REPLACED_PATH_FILE": str(
                    self.root / "cleanup-replaced-path"
                ),
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

    def desktop_entry(self) -> Path:
        return self.data / DESKTOP_ENTRY_RELATIVE

    def settings_icon(self) -> Path:
        return self.data / SETTINGS_ICON_RELATIVE

    def downgrade_install_to_v1(self) -> None:
        manifest_path = self.data / "murmur-ime/install-manifest.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["version"] = 1
        del document["digests"]["desktop_entry"]
        del document["digests"]["settings_icon"]
        manifest_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.desktop_entry().unlink()
        self.settings_icon().unlink()

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
            MOCK_PYTHON_ISOLATED=0
            if [[ ${1:-} == -I ]]; then
              shift
              MOCK_PYTHON_ISOLATED=1
            fi
            if [[ ${MOCK_HOSTILE_PIP_ENV:-0} == 1 ]]; then
              [[ $MOCK_PYTHON_ISOLATED == 1 \
                && -z ${PYTHONPATH:-} && -z ${PYTHONHOME:-} ]] || exit 60
            fi
            if [[ ${1:-} == */install_manifest.py \
              && ${2:-} == verify \
              && " $* " == *" --print-version "* \
              && -n ${MOCK_REPLACE_ASSET_AFTER_VERIFY:-} \
              && ! -f ${MOCK_RACE_INJECTED_FILE:-/nonexistent} ]]; then
              "$REAL_PYTHON" "$@"
              temporary="$MOCK_REPLACE_ASSET_AFTER_VERIFY.race.$$"
              printf '%s\n' 'foreign replacement' >"$temporary"
              chmod 0644 "$temporary"
              mv -- "$temporary" "$MOCK_REPLACE_ASSET_AFTER_VERIFY"
              touch "$MOCK_RACE_INJECTED_FILE"
              exit 0
            fi
            if [[ ${1:-} == */install_manifest.py \
              && ${2:-} == verify \
              && " $* " != *" --print-version "* \
              && " $* " != *" --staged "* \
              && -n ${MOCK_REPLACE_BEFORE_FINAL_VERIFY:-} \
              && ! -f ${MOCK_RACE_INJECTED_FILE:-/nonexistent} ]]; then
              temporary="$MOCK_REPLACE_BEFORE_FINAL_VERIFY.race.$$"
              printf '%s\n' 'foreign post-commit replacement' >"$temporary"
              chmod 0644 "$temporary"
              mv -- "$temporary" "$MOCK_REPLACE_BEFORE_FINAL_VERIFY"
              touch "$MOCK_RACE_INJECTED_FILE"
            fi
            if [[ ${1:-} == */install_manifest.py \
              && ${2:-} == verify \
              && " $* " != *" --print-version "* \
              && " $* " != *" --staged "* \
              && -n ${MOCK_TAMPER_BEFORE_FINAL_VERIFY:-} \
              && ! -f ${MOCK_RACE_INJECTED_FILE:-/nonexistent} ]]; then
              printf '%s\n' 'post-rename mutation' \
                >>"$MOCK_TAMPER_BEFORE_FINAL_VERIFY"
              touch "$MOCK_RACE_INJECTED_FILE"
            fi
            if [[ ${1:-} == */install_manifest.py \
              && ${2:-} == move-no-clobber \
              && -n ${MOCK_CREATE_FOREIGN_BEFORE_ROLLBACK_MOVE:-} \
              && (${4:-} == *.murmur-ime.rollback.*/* \
                || ${4:-} == *.murmur-ime.remove.*/*) \
              && ${6:-} == "$MOCK_CREATE_FOREIGN_BEFORE_ROLLBACK_MOVE" \
              && ! -f ${MOCK_ROLLBACK_RACE_INJECTED_FILE:-/nonexistent} ]]; then
              destination=${6:-}
              if [[ ${MOCK_ROLLBACK_FOREIGN_KIND:-file} == directory ]]; then
                mkdir -- "$destination"
                printf '%s\n' 'foreign rollback arrival' \
                  >"$destination/foreign.txt"
              else
                printf '%s\n' 'foreign rollback arrival' >"$destination"
                chmod 0644 "$destination"
              fi
              touch "$MOCK_ROLLBACK_RACE_INJECTED_FILE"
            fi
            if [[ ${1:-} == */install_manifest.py \
              && ${2:-} == move-no-clobber \
              && -n ${MOCK_CREATE_FOREIGN_BEFORE_MOVE:-} \
              && ! -f ${MOCK_RACE_INJECTED_FILE:-/nonexistent} ]]; then
              destination=${6:-}
              if [[ $destination == "$MOCK_CREATE_FOREIGN_BEFORE_MOVE" ]]; then
                mkdir -p -- "$(dirname -- "$destination")"
                if [[ ${MOCK_FOREIGN_MOVE_KIND:-file} == directory ]]; then
                  mkdir -- "$destination"
                  printf '%s\n' 'foreign arrival' >"$destination/foreign.txt"
                else
                  printf '%s\n' 'foreign arrival' >"$destination"
                  chmod 0644 "$destination"
                fi
                touch "$MOCK_RACE_INJECTED_FILE"
              fi
            fi
            if [[ ${1:-} == */install_manifest.py \
              && ${2:-} == voice-process-count \
              && ${MOCK_MANAGED_VOICE_AFTER_QUARANTINE:-0} == 1 \
              && " $* " == *" --argv-root "* \
              && (${4:-} == */.murmur-ime.rollback.*/root \
                || ${4:-} == */.murmur-ime.remove.*/root) ]]; then
              printf 'voice-process-race %s\n' "$*" >>"$MOCK_LOG"
              printf '%s\n' 1
              exit 0
            fi
            if [[ ${1:-} == */install_manifest.py \
              && ${2:-} == voice-process-count \
              && ${MOCK_MANAGED_NEW_VOICE_AFTER_COMMIT:-0} == 1 \
              && " $* " == *" --argv-root "* \
              && ${4:-} == */.murmur-ime.rollback.*/new-root ]]; then
              printf 'new-voice-process-race %s\n' "$*" >>"$MOCK_LOG"
              printf '%s\n' 1
              exit 0
            fi
            if [[ ${1:-} == */install_manifest.py \
              && ${2:-} == voice-process-count \
              && ${MOCK_MANAGED_VOICE_PROCESSES:-0} != 0 ]]; then
              printf '%s\n' "$MOCK_MANAGED_VOICE_PROCESSES"
              exit 0
            fi
            if [[ ${1:-} == */verify_preview_bundle.py \
              && ${2:-} == --bundle-root \
              && ${4:-} == --check-install-wheelhouse ]]; then
              if [[ ${MOCK_HOSTILE_PIP_ENV:-0} == 1 ]]; then
                [[ ${MOCK_PYTHON_ISOLATED:-0} == 1 \
                  && -z ${PYTHONPATH:-} && -z ${PYTHONHOME:-} ]] || exit 56
              fi
              printf 'bundle-verify %s\n' "$*" >>"$MOCK_LOG"
              [[ ${MOCK_FAIL_BUNDLE_VERIFY:-0} != 1 ]] || exit 53
              if [[ ${6:-} == --check-installed-venv ]]; then
                [[ -f ${7:-}/.mock-local-wheels-installed ]] || exit 54
              fi
              exit 0
            fi
            if [[ ${1:-} == */render_systemd_units.py ]]; then
              exec "$REAL_PYTHON" "$@"
            fi
            if [[ ${1:-} == -c ]]; then
              exit 0
            fi
            if [[ ${1:-} == -m && ${2:-} == venv ]]; then
              if [[ ${MOCK_HOSTILE_PIP_ENV:-0} == 1 ]]; then
                [[ ${MOCK_PYTHON_ISOLATED:-0} == 1 ]] || exit 55
              fi
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
            printf 'venv-pythonpath %s\n' "${PYTHONPATH:-}" >>"$MOCK_LOG"
            if [[ ${1:-} == -I ]]; then
              shift
              MOCK_VENV_PYTHON_ISOLATED=1
            fi
            if [[ ${MOCK_HOSTILE_PIP_ENV:-0} == 1 \
              && ${1:-} == -c ]]; then
              [[ ${MOCK_VENV_PYTHON_ISOLATED:-0} == 1 \
                && -z ${PYTHONPATH:-} && -z ${PYTHONHOME:-} ]] || exit 61
            fi
            if [[ ${1:-} == */verify_preview_bundle.py \
              && ${2:-} == --bundle-root \
              && ${4:-} == --check-install-wheelhouse ]]; then
              if [[ ${MOCK_HOSTILE_PIP_ENV:-0} == 1 ]]; then
                [[ ${MOCK_VENV_PYTHON_ISOLATED:-0} == 1 \
                  && -z ${PYTHONPATH:-} && -z ${PYTHONHOME:-} ]] || exit 56
              fi
              printf 'bundle-verify %s\n' "$*" >>"$MOCK_LOG"
              [[ ${MOCK_FAIL_BUNDLE_VERIFY:-0} != 1 ]] || exit 53
              if [[ ${6:-} == --check-installed-venv ]]; then
                [[ -f ${7:-}/.mock-local-wheels-installed ]] || exit 54
              fi
              exit 0
            fi
            if [[ ${1:-} == -m && ${2:-} == pip ]]; then
              [[ ${MOCK_FAIL_PIP:-0} != 1 ]] || exit 47
              if [[ ${MOCK_HOSTILE_PIP_ENV:-0} == 1 \
                && (${MOCK_VENV_PYTHON_ISOLATED:-0} != 1 \
                  || " $* " != *" --isolated "*) ]]; then
                exit 0
              fi
              if [[ ${MOCK_PREINSTALLED_RUNTIME:-0} == 1 ]]; then
                [[ " $* " == *" --ignore-installed "* \
                  && " $* " == *" --no-deps "* \
                  && $* == *'/sounddevice-0.5.6-py3-none-any.whl'* \
                  && $* == *'/websockets-17.0.1-py3-none-any.whl'* \
                  && $* == *'/cffi-2.1.1-py3-none-any.whl'* \
                  && $* == *'/pycparser-3.0-py3-none-any.whl'* ]] || exit 52
              fi
              launcher=$(dirname -- "$0")/murmur-voice-daemon
              printf '%s\n' '#!/usr/bin/env bash' 'exit 0' >"$launcher"
              chmod 0755 "$launcher"
              site=$(dirname -- "$0")/../lib/python3.12/site-packages/murmur_voice
              mkdir -p "$site"
              printf '%s\n' '__version__ = "0.1.0a7"' >"$site/__init__.py"
              touch "$(dirname -- "$0")/../.mock-local-wheels-installed"
              exit 0
            fi
            if [[ ${1:-} == -c ]]; then
              if [[ ${MOCK_HOSTILE_PIP_ENV:-0} == 1 \
                && ${2:-} == *'import gi, sounddevice'* ]]; then
                [[ ${MOCK_VENV_PYTHON_ISOLATED:-0} == 1 ]] || exit 57
              fi
              if (($# >= 3)); then
                code=$2
                config=${!#}
                if [[ $code == *load_vocabulary* ]]; then
                  [[ ${MOCK_VOCABULARY_INVALID:-0} != 1 ]] || exit 1
                elif [[ $code == *load_adaptive_ledger* ]]; then
                  [[ ${MOCK_ADAPTIVE_CORRECTIONS_INVALID:-0} != 1 ]] || exit 1
                elif [[ $code == *load_corrections* ]]; then
                  [[ ${MOCK_CORRECTIONS_INVALID:-0} != 1 ]] || exit 1
                elif [[ $code == *load_data_collection_config* ]]; then
                  [[ ${MOCK_DATA_COLLECTION_INVALID:-0} != 1 ]] || exit 1
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
            "rm",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ ${MOCK_FAIL_PRIVATE_TREE_REMOVE_ONCE:-0} == 1 \
              && -n ${MOCK_FAIL_PRIVATE_TREE_REMOVE_PARENT:-} \
              && " $* " == *"$MOCK_FAIL_PRIVATE_TREE_REMOVE_PARENT/.murmur-ime.cleanup."* \
              && ! -f $MOCK_REMOVE_PRIVATE_TREE_FAILED_FILE ]]; then
              touch "$MOCK_REMOVE_PRIVATE_TREE_FAILED_FILE"
              exit 58
            fi
            exec /usr/bin/rm "$@"
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
            if [[ ${1:-} == daemon-reload \
              && ${MOCK_FAIL_DAEMON_RELOAD_ONCE:-0} == 1 \
              && ! -f $MOCK_DAEMON_RELOAD_FAILED_FILE ]]; then
              touch "$MOCK_DAEMON_RELOAD_FAILED_FILE"
              exit 51
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
              if [[ $service == murmur-ime-voice.service ]]; then
                mkdir -p -- \
                  "$XDG_RUNTIME_DIR/murmur-ime" \
                  "$XDG_RUNTIME_DIR/murmur-ime-private"
                chmod 0700 \
                  "$XDG_RUNTIME_DIR/murmur-ime" \
                  "$XDG_RUNTIME_DIR/murmur-ime-private"
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
              if [[ $command == stop \
                && $service == murmur-ime-voice.service ]]; then
                voice_unit="$XDG_CONFIG_HOME/systemd/user/$service"
                if [[ ! -f $voice_unit ]] \
                  || ! grep -Fqx -- 'RuntimeDirectoryPreserve=yes' "$voice_unit"; then
                  /usr/bin/rm -rf -- \
                    "$XDG_RUNTIME_DIR/murmur-ime" \
                    "$XDG_RUNTIME_DIR/murmur-ime-private"
                fi
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
            if [[ $command == start \
              && $service == murmur-ime-engine.service \
              && -n ${MOCK_REPLACE_INSTALL_CLEANUP_KIND:-} \
              && ! -f $MOCK_CLEANUP_REPLACED_FILE ]]; then
              shopt -s nullglob
              candidates=("$XDG_CONFIG_HOME"/systemd/user/.murmur-ime.stage.*)
              ((${#candidates[@]} == 1)) || exit 59
              cleanup_path=${candidates[0]}
              mv -- "$cleanup_path" "$cleanup_path.before-replacement"
              if [[ $MOCK_REPLACE_INSTALL_CLEANUP_KIND == symlink ]]; then
                ln -s -- "$MOCK_CLEANUP_REPLACEMENT_TARGET" "$cleanup_path"
              else
                printf '%s\n' 'foreign cleanup replacement' >"$cleanup_path"
                chmod 0644 "$cleanup_path"
              fi
              printf '%s\n' "$cleanup_path" >"$MOCK_CLEANUP_REPLACED_PATH_FILE"
              touch "$MOCK_CLEANUP_REPLACED_FILE"
            fi
            exit 0
            """,
        )
        self._write_executable(
            "flatpak",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'flatpak %s\n' "$*" >>"$MOCK_LOG"
            if [[ ${1:-} == ps && ${2:-} == --columns=application ]]; then
              if [[ ${MOCK_FLATPAK_CONTROLLER_RUNNING:-0} == 1 ]]; then
                printf '%s\n' 'com.doubao.Murmur'
              fi
              exit 0
            fi
            exit 1
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

    def test_harness_is_hermetic_under_group_writable_umask(self) -> None:
        previous_umask = os.umask(0o002)
        permissive_harness = None
        try:
            permissive_harness = InstallerHarness()
            result = permissive_harness.run(
                INSTALLER, "--wheelhouse", str(permissive_harness.wheelhouse)
            )
        finally:
            os.umask(previous_umask)

        assert permissive_harness is not None
        try:
            self.assertEqual(result.returncode, 0, result.stderr)
            for directory in (
                permissive_harness.home,
                permissive_harness.data,
                permissive_harness.config,
                permissive_harness.fake_bin,
                permissive_harness.wheelhouse,
                permissive_harness.runtime,
            ):
                with self.subTest(directory=directory.name):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            install_root = permissive_harness.data / "murmur-ime"
            managed_package = (
                install_root / "voice-venv/lib/python3.12/site-packages/murmur_voice"
            )
            for directory in (
                install_root / "voice-venv",
                install_root / "voice-venv/bin",
                managed_package,
            ):
                with self.subTest(managed_directory=directory.name):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((managed_package / "__init__.py").stat().st_mode),
                0o600,
            )
        finally:
            permissive_harness.close()

    def test_uninstall_help_is_read_only(self) -> None:
        result = self.harness.run(UNINSTALLER, "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: scripts/uninstall-user.sh", result.stdout)
        self.assertEqual(self.harness.calls(), [])

    def test_uninstall_rejects_unknown_arguments_before_any_action(self) -> None:
        result = self.harness.run(UNINSTALLER, "--definitely-not-supported")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown option", result.stderr)
        self.assertEqual(self.harness.calls(), [])

    def test_system_python_and_installed_config_probes_are_isolated(self) -> None:
        for script in (INSTALLER, UNINSTALLER):
            text = script.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if "python3" in line:
                    self.assertIn(
                        "python3 -I",
                        line,
                        f"unisolated system Python at {script}:{line_number}",
                    )
        uninstall_prefix = UNINSTALLER.read_text(encoding="utf-8").splitlines()[:4]
        self.assertIn("unset PYTHONHOME PYTHONPATH", uninstall_prefix)
        install_text = INSTALLER.read_text(encoding="utf-8")
        self.assertEqual(
            install_text.count('"$install_root/voice-venv/bin/python" -I -c'),
            5,
        )

    def test_no_clobber_helper_commits_a_directory_atomically(self) -> None:
        source = self.harness.data / "directory-stage"
        source.mkdir(mode=0o700)
        (source / "payload").write_text("managed\n", encoding="utf-8")
        destination = self.harness.data / "directory-final"

        identity = move_no_clobber(source, destination)

        self.assertFalse(source.exists())
        self.assertEqual(
            identity,
            f"{destination.stat().st_dev}:{destination.stat().st_ino}",
        )
        self.assertEqual(
            (destination / "payload").read_text(encoding="utf-8"), "managed\n"
        )
        second_source = self.harness.data / "second-directory-stage"
        second_source.mkdir(mode=0o700)
        with self.assertRaises(ManifestError):
            move_no_clobber(second_source, destination)
        self.assertTrue(second_source.is_dir())

    def test_managed_voice_process_count_matches_current_and_legacy_argv(
        self,
    ) -> None:
        install_root = self.harness.data / "proc-match-root"
        expected_python = install_root / "voice-venv/bin/python"
        venv.EnvBuilder(with_pip=False).create(install_root / "voice-venv")
        site_packages = Path(
            subprocess.check_output(
                [
                    str(expected_python),
                    "-I",
                    "-c",
                    "import sysconfig; print(sysconfig.get_path('purelib'))",
                ],
                text=True,
            ).strip()
        )
        test_module = site_packages / "murmur_voice"
        test_module.mkdir()
        (test_module / "__init__.py").write_text("", encoding="utf-8")
        (test_module / "__main__.py").write_text(
            "import os\n"
            "import pathlib\n"
            "import time\n"
            'pathlib.Path(os.environ["MOCK_VOICE_READY"]).touch()\n'
            "time.sleep(30)\n",
            encoding="utf-8",
        )

        def spawn(flag: str, ready: Path) -> subprocess.Popen[bytes]:
            # Run the actual venv interpreter with the production argv spelling
            # so the matcher exercises the host's real /proc entries.
            process_environment = os.environ.copy()
            process_environment["MOCK_VOICE_READY"] = str(ready)
            return subprocess.Popen(
                [
                    str(expected_python),
                    flag,
                    "-m",
                    "murmur_voice",
                    "run",
                    "--socket",
                    str(self.harness.runtime / "custom.sock"),
                ],
                env=process_environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        ready_paths = [
            self.harness.root / f"voice-process-{index}.ready" for index in range(3)
        ]
        processes = [
            spawn(flag, ready)
            for flag, ready in zip(("-I", "-s", "-B"), ready_paths, strict=True)
        ]
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if all(ready.exists() for ready in ready_paths):
                    break
                if any(process.poll() is not None for process in processes):
                    break
                time.sleep(0.01)
            self.assertTrue(
                all(ready.exists() for ready in ready_paths),
                "voice test processes did not reach their stable run state: "
                f"{[process.poll() for process in processes]}",
            )
            self.assertTrue(
                all(process.poll() is None for process in processes),
                "voice test process exited after signaling readiness",
            )
            self.assertEqual(managed_voice_process_count(install_root), 2)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                process.wait(timeout=2)

    def test_process_count_keeps_a_canonical_argv_after_root_quarantine(
        self,
    ) -> None:
        real_data = self.harness.root / "canonical-data"
        real_data.mkdir(mode=0o700)
        alias_data = self.harness.root / "data-alias"
        alias_data.symlink_to(real_data, target_is_directory=True)
        published_root = alias_data / "murmur-ime"
        venv.EnvBuilder(with_pip=False).create(published_root / "voice-venv")
        published_python = published_root / "voice-venv/bin/python"
        site_packages = Path(
            subprocess.check_output(
                [
                    str(published_python),
                    "-I",
                    "-c",
                    "import sysconfig; print(sysconfig.get_path('purelib'))",
                ],
                text=True,
            ).strip()
        )
        test_module = site_packages / "murmur_voice"
        test_module.mkdir()
        (test_module / "__init__.py").write_text("", encoding="utf-8")
        (test_module / "__main__.py").write_text(
            "import os\n"
            "import pathlib\n"
            "import time\n"
            'pathlib.Path(os.environ["MOCK_VOICE_READY"]).touch()\n'
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        # Resolve the symlinked ancestor but deliberately preserve the final
        # venv interpreter component, just as an absolute canonical launcher
        # path would appear in argv[0].
        canonical_python = (
            published_python.parent.resolve(strict=True) / published_python.name
        )
        ready = self.harness.root / "canonical-process-ready"
        process_environment = os.environ.copy()
        process_environment["MOCK_VOICE_READY"] = str(ready)
        process = subprocess.Popen(
            [str(canonical_python), "-I", "-m", "murmur_voice", "run"],
            env=process_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        quarantine = real_data / "quarantined-root"
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if ready.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            self.assertEqual(managed_voice_process_count(published_root), 1)
            published_root.rename(quarantine)
            self.assertFalse(published_root.exists())
            self.assertEqual(managed_voice_process_count(quarantine, published_root), 1)
        finally:
            process.terminate()
            process.wait(timeout=2)

    def test_default_never_falls_back_to_network_without_a_wheelhouse(self) -> None:
        if (REPOSITORY / "wheelhouse").exists():
            self.skipTest("the source bundle includes its default wheelhouse")

        result = self.harness.run(INSTALLER)

        self.assertEqual(result.returncode, 2)
        self.assertIn("No offline wheelhouse", result.stderr)
        self.assertFalse(any(is_venv_pip_call(line) for line in self.harness.calls()))
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
        pip_call = next(line for line in calls if is_venv_pip_call(line))
        self.assertIn("--no-index", pip_call)
        self.assertIn("--isolated", pip_call)
        self.assertIn("--ignore-installed", pip_call)
        self.assertIn("--no-deps", pip_call)
        self.assertIn("--find-links", pip_call)
        self.assertIn("/install-wheelhouse", pip_call)
        for wheel in self.harness.wheels:
            self.assertIn(f"/install-wheelhouse/{wheel.name}", pip_call)
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
        pip_log_index = calls.index(pip_call)
        self.assertEqual(calls[pip_log_index + 2], "venv-pythonpath ")
        voice_stop = calls.index("systemctl --user stop murmur-ime-voice.service")
        engine_stop = calls.index("systemctl --user stop murmur-ime-engine.service")
        bundle_verifies = [
            index
            for index, line in enumerate(calls)
            if line.startswith("bundle-verify ")
        ]
        self.assertEqual(len(bundle_verifies), 3)
        self.assertIn("--check-installed-venv", calls[bundle_verifies[2]])
        pip_install = next(
            index for index, line in enumerate(calls) if is_venv_pip_call(line)
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
        self.assertLess(bundle_verifies[0], bundle_verifies[1])
        self.assertLess(bundle_verifies[1], venv_create)
        self.assertLess(venv_create, engine_stop)
        self.assertLess(venv_create, pip_install)
        self.assertLess(pip_install, bundle_verifies[2])
        self.assertLess(bundle_verifies[2], engine_restart)
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
        self.assertFalse((install_root / "install-wheelhouse").exists())
        manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest_document["version"], 2)
        self.assertIn("desktop_entry", manifest_document["digests"])
        self.assertIn("settings_icon", manifest_document["digests"])
        desktop_entry = self.harness.desktop_entry()
        settings_icon = self.harness.settings_icon()
        self.assertEqual(stat.S_IMODE(desktop_entry.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(settings_icon.stat().st_mode), 0o644)
        desktop_text = desktop_entry.read_text(encoding="utf-8")
        self.assertIn(
            f'Exec=/usr/bin/env -- "{str(install_root).replace("%", "%%").replace("$", r"\\$")}'
            '/open-voice-input-settings"',
            desktop_text,
        )
        self.assertIn(
            "Icon=io.github.SidUParis.OpenVoiceInputLinux.Settings", desktop_text
        )
        unit = (
            self.harness.config / "systemd/user/murmur-ime-voice.service"
        ).read_text(encoding="utf-8")
        self.assertIn("UMask=0077", unit)
        self.assertNotIn("ConditionPathExists=", unit)
        self.assertIn("RuntimeDirectoryPreserve=yes", unit)
        self.assertIn(
            "RuntimeDirectory=murmur-ime murmur-ime-private",
            unit,
        )
        self.assertNotIn("RuntimeDirectoryPreserve=restart", unit)
        self.assertIn("Environment=PYTHONNOUSERSITE=1", unit)
        self.assertIn("%%literal$$", unit)
        self.assertIn("--vocabulary", unit)
        self.assertIn(str(self.harness.config / "murmur-ime/vocabulary.json"), unit)
        self.assertIn("--corrections", unit)
        self.assertIn(str(self.harness.config / "murmur-ime/corrections.json"), unit)
        self.assertIn("--adaptive-corrections", unit)
        self.assertIn(
            str(self.harness.config / "murmur-ime/adaptive-corrections.json"),
            unit,
        )
        self.assertIn("--data-collection", unit)
        self.assertIn(
            str(self.harness.config / "murmur-ime/data-collection.json"),
            unit,
        )
        self.assertIn("--microphone-priority", unit)
        self.assertIn(
            str(self.harness.config / "murmur-ime/microphone-priority.json"),
            unit,
        )
        self.assertIn("--interaction", unit)
        self.assertIn(
            str(self.harness.config / "murmur-ime/interaction.json"),
            unit,
        )
        self.assertIn("--output-style", unit)
        self.assertIn(
            str(self.harness.config / "murmur-ime/output-style.json"),
            unit,
        )
        self.assertIn("--output-target", unit)
        self.assertIn(
            str(self.harness.config / "murmur-ime/output-target.json"),
            unit,
        )
        engine_unit = (
            self.harness.config / "systemd/user/murmur-ime-engine.service"
        ).read_text(encoding="utf-8")
        self.assertIn("UMask=0077", engine_unit)
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", engine_unit)
        engine_launcher = (install_root / "murmur-ime-engine").read_text(
            encoding="utf-8"
        )
        self.assertTrue(engine_launcher.startswith("#!/usr/bin/python3 -B\n"))
        self.assertTrue(config.exists())
        self.assertFalse((self.harness.config / "ibus/rime").exists())
        launcher = (install_root / "murmur-voice-daemon").read_text(encoding="utf-8")
        self.assertIn("PYTHONNOUSERSITE=1", launcher)
        self.assertIn(' -I -B -m murmur_voice "$@"', launcher)
        settings_launcher = (install_root / "open-voice-input-settings").read_text(
            encoding="utf-8"
        )
        self.assertIn("PYTHONNOUSERSITE=1", settings_launcher)
        self.assertIn(" -I -B -m murmur_voice.settings_app", settings_launcher)

    def test_installed_voice_unit_preserves_runtime_inode_across_stop_start(
        self,
    ) -> None:
        self.harness.configure_key_placeholder()
        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        runtime_dir = self.harness.runtime / "murmur-ime"
        private_runtime_dir = self.harness.runtime / "murmur-ime-private"
        self.assertTrue(runtime_dir.is_dir())
        self.assertTrue(private_runtime_dir.is_dir())
        original_inode = runtime_dir.stat().st_ino
        private_original_inode = private_runtime_dir.stat().st_ino
        controller_view = runtime_dir / "controller-bind-sentinel"
        controller_view.write_text("same-runtime-directory\n", encoding="utf-8")

        for command in ("stop", "start"):
            with self.subTest(command=command):
                service_result = subprocess.run(
                    [
                        str(self.harness.fake_bin / "systemctl"),
                        "--user",
                        command,
                        "murmur-ime-voice.service",
                    ],
                    env=self.harness.environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(service_result.returncode, 0, service_result.stderr)
                self.assertTrue(runtime_dir.is_dir())
                self.assertTrue(private_runtime_dir.is_dir())
                self.assertEqual(runtime_dir.stat().st_ino, original_inode)
                self.assertEqual(
                    private_runtime_dir.stat().st_ino,
                    private_original_inode,
                )
                self.assertEqual(
                    controller_view.read_text(encoding="utf-8"),
                    "same-runtime-directory\n",
                )

    def test_legacy_runtime_policy_reports_one_time_controller_refresh(
        self,
    ) -> None:
        self.harness.configure_key_placeholder()
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        voice_unit = self.harness.config / "systemd/user/murmur-ime-voice.service"
        legacy_unit = voice_unit.read_text(encoding="utf-8").replace(
            "RuntimeDirectoryPreserve=yes",
            "RuntimeDirectoryPreserve=restart",
        )
        self.assertIn("RuntimeDirectoryPreserve=restart", legacy_unit)
        voice_unit.write_text(legacy_unit, encoding="utf-8")
        manifest = self.harness.data / "murmur-ime/install-manifest.json"
        manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_document["digests"]["voice_unit"] = hashlib.sha256(
            voice_unit.read_bytes()
        ).hexdigest()
        manifest.write_text(
            json.dumps(manifest_document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        old_controller_view = self.harness.runtime / "murmur-ime/old-controller-view"
        old_controller_view.write_text("legacy-bind\n", encoding="utf-8")
        self.harness.environment["MOCK_FLATPAK_CONTROLLER_RUNNING"] = "1"
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old_controller_view.exists())
        self.assertIn(
            "One-time controller refresh required after upgrading the "
            "runtime-directory policy.",
            result.stdout,
        )
        self.assertIn("flatpak kill com.doubao.Murmur", result.stdout)
        self.assertIn("flatpak run com.doubao.Murmur", result.stdout)
        self.assertIn("flatpak ps --columns=application", self.harness.calls())
        installed_unit = voice_unit.read_text(encoding="utf-8")
        self.assertIn("RuntimeDirectoryPreserve=yes", installed_unit)
        self.assertNotIn("RuntimeDirectoryPreserve=restart", installed_unit)

    def test_satisfying_preinstalled_runtime_cannot_bypass_wheelhouse(self) -> None:
        self.harness.environment["MOCK_PREINSTALLED_RUNTIME"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        pip_calls = [line for line in self.harness.calls() if is_venv_pip_call(line)]
        self.assertEqual(len(pip_calls), 1)
        self.assertIn("--ignore-installed", pip_calls[0])
        for wheel in self.harness.wheels:
            self.assertIn(f"/install-wheelhouse/{wheel.name}", pip_calls[0])

    def test_successful_install_reports_private_tree_cleanup_failure(self) -> None:
        self.harness.environment.update(
            {
                "MOCK_FAIL_PRIVATE_TREE_REMOVE_ONCE": "1",
                "MOCK_FAIL_PRIVATE_TREE_REMOVE_PARENT": str(
                    self.harness.config / "systemd/user"
                ),
            }
        )

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Installation committed, but cleanup was incomplete", result.stderr
        )
        self.assertIn("Cleanup material was retained at:", result.stderr)
        self.assertTrue((self.harness.data / "murmur-ime").is_dir())
        retained = [
            path
            for root in (self.harness.data, self.harness.config)
            for path in root.rglob(".murmur-ime.cleanup.*")
        ]
        self.assertEqual(len(retained), 1)
        self.assertTrue((retained[0] / "tree").is_dir())

    def test_install_cleanup_refuses_a_symlink_replacement(self) -> None:
        replacement_target = self.harness.root / "foreign-cleanup-target"
        replacement_target.mkdir()
        sentinel = replacement_target / "do-not-delete"
        sentinel.write_text("foreign\n", encoding="utf-8")
        self.harness.environment.update(
            {
                "MOCK_REPLACE_INSTALL_CLEANUP_KIND": "symlink",
                "MOCK_CLEANUP_REPLACEMENT_TARGET": str(replacement_target),
            }
        )

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        replaced_path = Path(
            Path(self.harness.environment["MOCK_CLEANUP_REPLACED_PATH_FILE"])
            .read_text(encoding="utf-8")
            .strip()
        )
        self.assertTrue(replaced_path.is_symlink())
        self.assertEqual(replaced_path.resolve(), replacement_target)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "foreign\n")
        self.assertTrue(Path(f"{replaced_path}.before-replacement").is_dir())
        self.assertIn(str(replaced_path), result.stderr)
        self.assertIn("changed or non-directory path retained", result.stderr)
        self.assertIn(
            "Installation committed, but cleanup was incomplete", result.stderr
        )

    def test_install_cleanup_refuses_a_regular_file_replacement(self) -> None:
        self.harness.environment.update(
            {
                "MOCK_REPLACE_INSTALL_CLEANUP_KIND": "file",
                "MOCK_CLEANUP_REPLACEMENT_TARGET": str(
                    self.harness.root / "unused-cleanup-target"
                ),
            }
        )

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        replaced_path = Path(
            Path(self.harness.environment["MOCK_CLEANUP_REPLACED_PATH_FILE"])
            .read_text(encoding="utf-8")
            .strip()
        )
        self.assertTrue(replaced_path.is_file())
        self.assertEqual(
            replaced_path.read_text(encoding="utf-8"),
            "foreign cleanup replacement\n",
        )
        self.assertTrue(Path(f"{replaced_path}.before-replacement").is_dir())
        self.assertIn(str(replaced_path), result.stderr)
        self.assertIn("changed or non-directory path retained", result.stderr)
        self.assertIn(
            "Installation committed, but cleanup was incomplete", result.stderr
        )

    def test_hostile_pip_environment_cannot_skip_local_wheel_install(self) -> None:
        self.harness.environment.update(
            {
                "MOCK_HOSTILE_PIP_ENV": "1",
                "PIP_DRY_RUN": "1",
                "PIP_TARGET": str(self.harness.root / "wrong-target"),
                "PYTHONPATH": str(self.harness.root / "host-shadow"),
                "PYTHONHOME": str(self.harness.root / "host-python-home"),
            }
        )

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.harness.calls()
        pip_call = next(line for line in calls if is_venv_pip_call(line))
        self.assertIn("venv-python -I -m pip --isolated", pip_call)
        self.assertIn("venv-pythonpath ", calls)
        probe_calls = [
            line for line in calls if line.startswith("venv-python ") and " -c " in line
        ]
        self.assertGreaterEqual(len(probe_calls), 3)
        self.assertTrue(
            all(line.startswith("venv-python -I -c ") for line in probe_calls)
        )
        post_verify = next(
            line
            for line in calls
            if line.startswith("bundle-verify ") and "--check-installed-venv" in line
        )
        self.assertIn("/install-wheelhouse", post_verify)
        install_root = self.harness.data / "murmur-ime"
        for launcher, module in (
            ("murmur-voice-daemon", "murmur_voice"),
            ("open-voice-input-settings", "murmur_voice.settings_app"),
        ):
            launched = subprocess.run(
                [str(install_root / launcher), "--help"],
                check=False,
                env=self.harness.environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(launched.returncode, 0, launched.stderr)
            self.assertIn(f"venv-python -I -B -m {module} --help", self.harness.calls())

    def test_uninstall_ignores_a_hostile_python_environment(self) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.harness.environment.update(
            {
                "MOCK_HOSTILE_PIP_ENV": "1",
                "PYTHONPATH": str(self.harness.root / "host-shadow"),
                "PYTHONHOME": str(self.harness.root / "host-python-home"),
            }
        )

        result = self.harness.run(UNINSTALLER)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.harness.data / "murmur-ime").exists())

    def test_offline_bundle_mismatch_is_rejected_before_venv_creation(self) -> None:
        self.harness.environment["MOCK_FAIL_BUNDLE_VERIFY"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            any(line.startswith("bundle-verify ") for line in self.harness.calls())
        )
        self.assertFalse(
            any(
                line.startswith("python3-venv ") or is_venv_pip_call(line)
                for line in self.harness.calls()
            )
        )

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

    def test_upgrade_shuts_down_a_live_fixed_private_socket(self) -> None:
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        runtime_dir = self.harness.runtime / "murmur-ime"
        runtime_dir.mkdir(mode=0o700)
        socket_path = runtime_dir / "voice.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen(4)
        socket_path.chmod(0o600)
        self.harness.log.write_text("", encoding="utf-8")
        try:
            result = self.harness.run(
                INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
            )
        finally:
            listener.close()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(socket_path.exists())
        self.assertIn(
            f"venv-python -I -B -m murmur_voice shutdown --socket {socket_path}",
            self.harness.calls(),
        )

    def test_upgrade_refuses_a_live_fixed_daemon_that_will_not_shutdown(
        self,
    ) -> None:
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        runtime_dir = self.harness.runtime / "murmur-ime"
        runtime_dir.mkdir(mode=0o700)
        socket_path = runtime_dir / "voice.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen(4)
        socket_path.chmod(0o600)
        self.harness.environment["MOCK_SHUTDOWN_FAIL"] = "1"
        try:
            result = self.harness.run(
                INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
            )
        finally:
            listener.close()
            socket_path.unlink(missing_ok=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refused a controlled shutdown before upgrade", result.stderr)
        self.assertEqual(install_root.stat().st_ino, original_inode)

    def test_upgrade_refuses_a_custom_socket_foreground_daemon(self) -> None:
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        self.harness.environment["MOCK_MANAGED_VOICE_PROCESSES"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreground voice daemon is still running", result.stderr)
        self.assertEqual(install_root.stat().st_ino, original_inode)

    def test_upgrade_closes_the_process_race_after_root_quarantine(self) -> None:
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        self.harness.environment["MOCK_MANAGED_VOICE_AFTER_QUARANTINE"] = "1"
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("raced with upgrade", result.stderr)
        self.assertEqual(install_root.stat().st_ino, original_inode)
        self.assertTrue(
            any(line.startswith("voice-process-race ") for line in self.harness.calls())
        )
        self.assertEqual(list(self.harness.data.glob(".murmur-ime.rollback.*")), [])

    def test_rollback_retains_a_published_tree_started_after_commit(self) -> None:
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.harness.environment["MOCK_FAIL_ENGINE_START_ONCE"] = "1"
        self.harness.environment["MOCK_MANAGED_NEW_VOICE_AFTER_COMMIT"] = "1"
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retained both runtime quarantines", result.stderr)
        self.assertIn("rollback was incomplete", result.stderr)
        self.assertFalse((self.harness.data / "murmur-ime").exists())
        retained = list(self.harness.data.glob(".murmur-ime.rollback.*"))
        self.assertEqual(len(retained), 1)
        self.assertTrue((retained[0] / "root").is_dir())
        self.assertTrue((retained[0] / "new-root").is_dir())
        calls = self.harness.calls()
        self.assertTrue(
            any(line.startswith("new-voice-process-race ") for line in calls)
        )
        self.assertIn("systemctl --user stop murmur-ime-voice.service", calls)
        self.assertIn("systemctl --user disable murmur-ime-voice.service", calls)

    def test_v1_install_upgrades_to_v2_and_claims_new_desktop_assets(self) -> None:
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.harness.downgrade_install_to_v1()

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(
            (self.harness.data / "murmur-ime/install-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["version"], 2)
        self.assertTrue(self.harness.desktop_entry().is_file())
        self.assertTrue(self.harness.settings_icon().is_file())

    def test_v1_upgrade_refuses_foreign_desktop_asset_without_side_effects(
        self,
    ) -> None:
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.harness.downgrade_install_to_v1()
        foreign = self.harness.desktop_entry()
        foreign.write_text("foreign desktop\n", encoding="utf-8")
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("not owned by the v1 installation", result.stderr)
        self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign desktop\n")
        self.assertEqual(install_root.stat().st_ino, original_inode)
        self.assertFalse(any(" stop " in f" {line} " for line in self.harness.calls()))

    def test_failed_v1_to_v2_upgrade_rolls_back_without_claiming_assets(self) -> None:
        first = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.harness.downgrade_install_to_v1()
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        self.harness.environment["MOCK_FAIL_ENGINE_START_ONCE"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(install_root.stat().st_ino, original_inode)
        manifest = json.loads(
            (install_root / "install-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], 1)
        self.assertFalse(self.harness.desktop_entry().exists())
        self.assertFalse(self.harness.settings_icon().exists())

        uninstall = self.harness.run(UNINSTALLER)
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertFalse(install_root.exists())

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
        desktop_inode = self.harness.desktop_entry().stat().st_ino
        icon_inode = self.harness.settings_icon().stat().st_ino
        self.harness.log.write_text("", encoding="utf-8")
        self.harness.environment["MOCK_FAIL_ENGINE_START_ONCE"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restoring the previous managed runtime", result.stderr)
        self.assertEqual(install_root.stat().st_ino, original_inode)
        self.assertEqual(self.harness.desktop_entry().stat().st_ino, desktop_inode)
        self.assertEqual(self.harness.settings_icon().stat().st_ino, icon_inode)
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

    def test_final_manifest_failure_rolls_back_every_committed_path(self) -> None:
        self.harness.environment["MOCK_TAMPER_BEFORE_FINAL_VERIFY"] = str(
            self.harness.desktop_entry()
        )

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restoring the previous managed runtime", result.stderr)
        self.assertFalse((self.harness.data / "murmur-ime").exists())
        self.assertFalse(self.harness.desktop_entry().exists())
        self.assertFalse(self.harness.settings_icon().exists())
        self.assertFalse(
            (self.harness.config / "systemd/user/murmur-ime-engine.service").exists()
        )
        self.assertFalse(
            (self.harness.config / "systemd/user/murmur-ime-voice.service").exists()
        )
        self.assertNotIn(
            "systemctl --user start murmur-ime-engine.service", self.harness.calls()
        )

    def test_rollback_does_not_delete_a_post_commit_foreign_replacement(self) -> None:
        self.harness.environment["MOCK_REPLACE_BEFORE_FINAL_VERIFY"] = str(
            self.harness.desktop_entry()
        )

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback was incomplete", result.stderr)
        self.assertEqual(
            self.harness.desktop_entry().read_text(encoding="utf-8"),
            "foreign post-commit replacement\n",
        )
        self.assertFalse((self.harness.data / "murmur-ime").exists())

    def test_foreign_unit_replacement_keeps_rollback_services_stopped(self) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        engine_unit = self.harness.config / "systemd/user/murmur-ime-engine.service"
        self.harness.environment["MOCK_REPLACE_BEFORE_FINAL_VERIFY"] = str(engine_unit)
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback was incomplete", result.stderr)
        self.assertEqual(
            engine_unit.read_text(encoding="utf-8"),
            "foreign post-commit replacement\n",
        )
        calls = self.harness.calls()
        self.assertNotIn("systemctl --user enable murmur-ime-engine.service", calls)
        self.assertNotIn("systemctl --user start murmur-ime-engine.service", calls)
        active = (
            self.harness.active.read_text(encoding="utf-8").splitlines()
            if self.harness.active.exists()
            else []
        )
        self.assertNotIn("murmur-ime-engine.service", active)

    def test_install_rollback_does_not_clobber_a_late_core_directory(self) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        self.harness.environment["MOCK_TAMPER_BEFORE_FINAL_VERIFY"] = str(
            self.harness.desktop_entry()
        )
        self.harness.environment["MOCK_CREATE_FOREIGN_BEFORE_ROLLBACK_MOVE"] = str(
            install_root
        )
        self.harness.environment["MOCK_ROLLBACK_FOREIGN_KIND"] = "directory"
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback was incomplete", result.stderr)
        self.assertEqual(
            (install_root / "foreign.txt").read_text(encoding="utf-8"),
            "foreign rollback arrival\n",
        )
        retained_roots = list(self.harness.data.glob(".murmur-ime.rollback.*/root"))
        self.assertEqual(len(retained_roots), 1)
        self.assertEqual(retained_roots[0].stat().st_ino, original_inode)
        self.assertNotIn(
            "systemctl --user start murmur-ime-engine.service", self.harness.calls()
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

    def test_install_refuses_foreign_desktop_assets_without_runtime_side_effects(
        self,
    ) -> None:
        self.harness.desktop_entry().parent.mkdir(parents=True)
        self.harness.desktop_entry().write_text("foreign desktop\n", encoding="utf-8")

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            self.harness.desktop_entry().read_text(encoding="utf-8"),
            "foreign desktop\n",
        )
        self.assertFalse((self.harness.data / "icons").exists())
        self.assertFalse((self.harness.data / "murmur-ime").exists())
        self.assertFalse(any(" stop " in f" {line} " for line in self.harness.calls()))
        self.assertFalse(list(self.harness.data.rglob(".murmur-ime.stage.*")))

    def test_first_install_does_not_clobber_asset_created_after_final_check(
        self,
    ) -> None:
        self.harness.environment["MOCK_CREATE_FOREIGN_BEFORE_MOVE"] = str(
            self.harness.desktop_entry()
        )

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.harness.desktop_entry().read_text(encoding="utf-8"),
            "foreign arrival\n",
        )
        self.assertFalse((self.harness.data / "murmur-ime").exists())
        self.assertFalse(self.harness.settings_icon().exists())

    def test_first_install_does_not_clobber_late_core_directory(self) -> None:
        install_root = self.harness.data / "murmur-ime"
        self.harness.environment["MOCK_CREATE_FOREIGN_BEFORE_MOVE"] = str(install_root)
        self.harness.environment["MOCK_FOREIGN_MOVE_KIND"] = "directory"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (install_root / "foreign.txt").read_text(encoding="utf-8"),
            "foreign arrival\n",
        )
        self.assertFalse((install_root / "murmur-ime-engine").exists())

    def test_v1_upgrade_does_not_clobber_late_unit_file(self) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.harness.downgrade_install_to_v1()
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        engine_unit = self.harness.config / "systemd/user/murmur-ime-engine.service"
        original_unit_inode = engine_unit.stat().st_ino
        self.harness.environment["MOCK_CREATE_FOREIGN_BEFORE_MOVE"] = str(engine_unit)

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(engine_unit.read_text(encoding="utf-8"), "foreign arrival\n")
        self.assertFalse(install_root.exists())
        retained_roots = list(self.harness.data.glob(".murmur-ime.rollback.*/root"))
        self.assertEqual(len(retained_roots), 1)
        self.assertEqual(retained_roots[0].stat().st_ino, original_inode)
        retained_units = list(
            (self.harness.config / "systemd/user").glob(
                ".murmur-ime.rollback.*/engine.service"
            )
        )
        self.assertEqual(len(retained_units), 1)
        self.assertEqual(retained_units[0].stat().st_ino, original_unit_inode)

    def test_v1_upgrade_does_not_clobber_asset_created_after_final_check(
        self,
    ) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.harness.downgrade_install_to_v1()
        self.harness.environment["MOCK_CREATE_FOREIGN_BEFORE_MOVE"] = str(
            self.harness.desktop_entry()
        )

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.harness.desktop_entry().read_text(encoding="utf-8"),
            "foreign arrival\n",
        )
        manifest = json.loads(
            (self.harness.data / "murmur-ime/install-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["version"], 1)
        self.assertFalse(self.harness.settings_icon().exists())

    def test_v2_tampered_desktop_assets_block_install_and_uninstall(self) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        for path in (self.harness.desktop_entry(), self.harness.settings_icon()):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"\nforeign mutation\n")
                self.harness.log.write_text("", encoding="utf-8")

                upgrade = self.harness.run(
                    INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
                )
                uninstall = self.harness.run(UNINSTALLER)

                self.assertEqual(upgrade.returncode, 2)
                self.assertEqual(uninstall.returncode, 2)
                self.assertEqual(path.read_bytes(), original + b"\nforeign mutation\n")
                self.assertFalse(
                    any(" stop " in f" {line} " for line in self.harness.calls())
                )
                path.write_bytes(original)

    def test_uninstall_restores_asset_replaced_after_initial_verification(
        self,
    ) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.harness.environment["MOCK_REPLACE_ASSET_AFTER_VERIFY"] = str(
            self.harness.desktop_entry()
        )

        result = self.harness.run(UNINSTALLER)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restoring the trusted installation", result.stderr)
        self.assertEqual(
            self.harness.desktop_entry().read_text(encoding="utf-8"),
            "foreign replacement\n",
        )
        self.assertTrue((self.harness.data / "murmur-ime").is_dir())
        self.assertTrue(self.harness.settings_icon().is_file())

    def test_v2_upgrade_restores_asset_replaced_after_initial_verification(
        self,
    ) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        install_root = self.harness.data / "murmur-ime"
        original_root_inode = install_root.stat().st_ino
        self.harness.environment["MOCK_REPLACE_ASSET_AFTER_VERIFY"] = str(
            self.harness.desktop_entry()
        )

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restoring the previous managed runtime", result.stderr)
        self.assertEqual(install_root.stat().st_ino, original_root_inode)
        self.assertEqual(
            self.harness.desktop_entry().read_text(encoding="utf-8"),
            "foreign replacement\n",
        )
        self.assertTrue(self.harness.settings_icon().is_file())

    def test_install_and_uninstall_share_a_nonblocking_data_root_lock(self) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        descriptor = os.open(
            self.harness.data,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            upgrade = self.harness.run(
                INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
            )
            uninstall = self.harness.run(UNINSTALLER)
        finally:
            os.close(descriptor)

        self.assertEqual(upgrade.returncode, 2)
        self.assertEqual(uninstall.returncode, 2)
        self.assertIn("already in progress", upgrade.stderr)
        self.assertIn("already in progress", uninstall.stderr)
        self.assertTrue((self.harness.data / "murmur-ime").is_dir())

    def test_shared_config_lock_rejects_install_with_a_different_data_root(
        self,
    ) -> None:
        alternate_data = self.harness.root / "alternate-data"
        alternate_data.mkdir()
        descriptors = [
            os.open(
                path,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            for path in (self.harness.data, self.harness.config)
        ]
        try:
            for descriptor in descriptors:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.harness.environment["XDG_DATA_HOME"] = str(alternate_data)
            result = self.harness.run(
                INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
            )
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

        self.assertEqual(result.returncode, 2)
        self.assertIn("already in progress", result.stderr)
        self.assertFalse((alternate_data / "murmur-ime").exists())

    def test_shared_config_install_lock_rejects_uninstall_from_original_data(
        self,
    ) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        alternate_data = self.harness.root / "alternate-data"
        alternate_data.mkdir()
        descriptors = [
            os.open(
                path,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            for path in (alternate_data, self.harness.config)
        ]
        try:
            for descriptor in descriptors:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # The held config lock represents an install using alternate_data;
            # uninstall from the original data root must share and reject it.
            self.harness.environment["XDG_DATA_HOME"] = str(self.harness.data)
            result = self.harness.run(UNINSTALLER)
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

        self.assertEqual(result.returncode, 2)
        self.assertIn("already in progress", result.stderr)
        self.assertTrue((self.harness.data / "murmur-ime").is_dir())

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

    def test_invalid_adaptive_corrections_do_not_enable_or_start_voice(self) -> None:
        self.harness.configure_key_placeholder()
        self.harness.environment["MOCK_ADAPTIVE_CORRECTIONS_INVALID"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.harness.calls()
        self.assertIn("systemctl --user disable murmur-ime-voice.service", calls)
        self.assertNotIn("systemctl --user enable murmur-ime-voice.service", calls)
        self.assertNotIn("systemctl --user start murmur-ime-voice.service", calls)
        self.assertIn("adaptive correction ledger", result.stdout)
        self.assertIn("enable --now murmur-ime-voice.service", result.stdout)

    def test_invalid_data_collection_setting_never_blocks_voice_service(self) -> None:
        self.harness.configure_key_placeholder()
        self.harness.environment["MOCK_DATA_COLLECTION_INVALID"] = "1"

        result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.harness.calls()
        self.assertIn("systemctl --user enable murmur-ime-voice.service", calls)
        self.assertIn("systemctl --user start murmur-ime-voice.service", calls)
        self.assertLess(
            calls.index("systemctl --user disable murmur-ime-voice.service"),
            calls.index("systemctl --user enable murmur-ime-voice.service"),
        )
        self.assertIn("Voice input remains enabled", result.stdout)

    def test_uninstall_restores_only_recorded_engine_and_retains_key(self) -> None:
        config = self.harness.configure_key_placeholder()
        corrections = config.parent / "corrections.json"
        corrections.write_text(
            '{"version":1,"pairs":[{"wrong":"test-wrong","canonical":"TestRight"}]}\n',
            encoding="utf-8",
        )
        corrections.chmod(0o600)
        adaptive_corrections = config.parent / "adaptive-corrections.json"
        adaptive_payload = (
            '{"version":1,"entries":[{"wrong":"bench mark",'
            '"canonical":"benchmark","state":"active","support":1}]}\n'
        )
        adaptive_corrections.write_text(adaptive_payload, encoding="utf-8")
        adaptive_corrections.chmod(0o600)
        external_dataset = self.harness.root / "selected training storage"
        utterance = external_dataset / "openvoiceinput-dataset-v1/utterances/test-id"
        utterance.mkdir(parents=True)
        audio = utterance / "audio.wav"
        audio.write_bytes(b"unchanged-dataset-sentinel")
        data_collection = config.parent / "data-collection.json"
        data_collection_payload = (
            json.dumps(
                {
                    "version": 1,
                    "enabled": True,
                    "directory": str(external_dataset),
                    "dataset_id": "test-dataset-id",
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        data_collection.write_text(data_collection_payload, encoding="utf-8")
        data_collection.chmod(0o600)
        microphone_priority = config.parent / "microphone-priority.json"
        microphone_priority_payload = (
            '{"version":1,"priority":["headset","dji","external",'
            '"built-in"],"preferred_sources":{}}\n'
        )
        microphone_priority.write_text(
            microphone_priority_payload,
            encoding="utf-8",
        )
        microphone_priority.chmod(0o600)
        interaction = config.parent / "interaction.json"
        interaction_payload = (
            '{"version":1,"interaction_mode":"toggle",'
            '"minimum_hold_milliseconds":180,"release_timeout_seconds":120}\n'
        )
        interaction.write_text(interaction_payload, encoding="utf-8")
        interaction.chmod(0o600)
        output_style = config.parent / "output-style.json"
        output_style_payload = '{"version":1,"mode":"clean"}\n'
        output_style.write_text(output_style_payload, encoding="utf-8")
        output_style.chmod(0o600)
        output_target = config.parent / "output-target.json"
        output_target_payload = '{"version":1,"target":"clipboard"}\n'
        output_target.write_text(output_target_payload, encoding="utf-8")
        output_target.chmod(0o600)
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        self.harness.ibus_state.write_text("murmur-voice\n", encoding="utf-8")
        runtime_dir = self.harness.runtime / "murmur-ime"
        runtime_dir.mkdir(mode=0o700, exist_ok=True)
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
        self.assertEqual(
            adaptive_corrections.read_text(encoding="utf-8"), adaptive_payload
        )
        self.assertEqual(adaptive_corrections.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            data_collection.read_text(encoding="utf-8"), data_collection_payload
        )
        self.assertEqual(
            microphone_priority.read_text(encoding="utf-8"),
            microphone_priority_payload,
        )
        self.assertEqual(microphone_priority.stat().st_mode & 0o777, 0o600)
        self.assertEqual(interaction.read_text(encoding="utf-8"), interaction_payload)
        self.assertEqual(interaction.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output_style.read_text(encoding="utf-8"), output_style_payload)
        self.assertEqual(output_style.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            output_target.read_text(encoding="utf-8"), output_target_payload
        )
        self.assertEqual(output_target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(audio.read_bytes(), b"unchanged-dataset-sentinel")
        self.assertIn("dataset", result.stdout)
        self.assertIn("output-style", result.stdout)
        self.assertIn("output-target", result.stdout)
        self.assertFalse((self.harness.data / "murmur-ime/voice-venv").exists())
        self.assertFalse(self.harness.desktop_entry().exists())
        self.assertFalse(self.harness.settings_icon().exists())
        self.assertFalse((self.harness.config / "ibus/rime").exists())
        self.assertFalse(socket_path.exists())
        self.assertFalse(runtime_dir.exists())
        self.assertFalse((self.harness.runtime / "murmur-ime-private").exists())

    def test_successful_uninstall_reports_quarantine_cleanup_failure(self) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.harness.environment.update(
            {
                "MOCK_FAIL_PRIVATE_TREE_REMOVE_ONCE": "1",
                "MOCK_FAIL_PRIVATE_TREE_REMOVE_PARENT": str(self.harness.data),
            }
        )
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(UNINSTALLER)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Uninstall committed, but cleanup was incomplete", result.stderr)
        self.assertIn("Cleanup material was retained at:", result.stderr)
        self.assertFalse((self.harness.data / "murmur-ime").exists())
        self.assertFalse(self.harness.desktop_entry().exists())
        self.assertFalse(self.harness.settings_icon().exists())
        retained = [
            path
            for root in (self.harness.data, self.harness.config)
            for path in root.rglob(".murmur-ime.cleanup.*")
        ]
        self.assertEqual(len(retained), 1)
        self.assertTrue((retained[0] / "tree/root").is_dir())

    def test_v1_uninstall_preserves_same_name_unowned_desktop_assets(self) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.harness.downgrade_install_to_v1()
        self.harness.desktop_entry().write_text("foreign desktop\n", encoding="utf-8")
        self.harness.settings_icon().write_text("foreign icon\n", encoding="utf-8")

        result = self.harness.run(UNINSTALLER)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.harness.data / "murmur-ime").exists())
        self.assertEqual(
            self.harness.desktop_entry().read_text(encoding="utf-8"),
            "foreign desktop\n",
        )
        self.assertEqual(
            self.harness.settings_icon().read_text(encoding="utf-8"),
            "foreign icon\n",
        )

    def test_uninstall_failure_rolls_back_desktop_assets(self) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        desktop_inode = self.harness.desktop_entry().stat().st_ino
        icon_inode = self.harness.settings_icon().stat().st_ino
        self.harness.environment["MOCK_FAIL_DAEMON_RELOAD_ONCE"] = "1"

        result = self.harness.run(UNINSTALLER)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restoring the trusted installation", result.stderr)
        self.assertTrue((self.harness.data / "murmur-ime").is_dir())
        self.assertEqual(self.harness.desktop_entry().stat().st_ino, desktop_inode)
        self.assertEqual(self.harness.settings_icon().stat().st_ino, icon_inode)

    def test_uninstall_rollback_does_not_clobber_a_late_unit_file(self) -> None:
        installed = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        engine_unit = self.harness.config / "systemd/user/murmur-ime-engine.service"
        original_inode = engine_unit.stat().st_ino
        self.harness.environment["MOCK_FAIL_DAEMON_RELOAD_ONCE"] = "1"
        self.harness.environment["MOCK_CREATE_FOREIGN_BEFORE_ROLLBACK_MOVE"] = str(
            engine_unit
        )
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(UNINSTALLER)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback was incomplete", result.stderr)
        self.assertEqual(
            engine_unit.read_text(encoding="utf-8"),
            "foreign rollback arrival\n",
        )
        retained_units = list(
            (self.harness.config / "systemd/user").glob(
                ".murmur-ime.remove.*/engine.service"
            )
        )
        self.assertEqual(len(retained_units), 1)
        self.assertEqual(retained_units[0].stat().st_ino, original_inode)
        calls = self.harness.calls()
        self.assertNotIn("systemctl --user enable murmur-ime-engine.service", calls)
        self.assertNotIn("systemctl --user start murmur-ime-engine.service", calls)

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
        runtime_dir.mkdir(mode=0o700, exist_ok=True)
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
        runtime_dir.mkdir(mode=0o700, exist_ok=True)
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

    def test_uninstall_closes_the_process_race_after_root_quarantine(self) -> None:
        install_result = self.harness.run(
            INSTALLER, "--wheelhouse", str(self.harness.wheelhouse)
        )
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        install_root = self.harness.data / "murmur-ime"
        original_inode = install_root.stat().st_ino
        self.harness.environment["MOCK_MANAGED_VOICE_AFTER_QUARANTINE"] = "1"
        self.harness.log.write_text("", encoding="utf-8")

        result = self.harness.run(UNINSTALLER)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("raced with uninstall", result.stderr)
        self.assertEqual(install_root.stat().st_ino, original_inode)
        self.assertTrue(
            any(line.startswith("voice-process-race ") for line in self.harness.calls())
        )
        self.assertEqual(list(self.harness.data.glob(".murmur-ime.remove.*")), [])

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
        pip_call = next(line for line in self.harness.calls() if is_venv_pip_call(line))
        self.assertNotIn("--no-index", pip_call)
        self.assertIn(str(REPOSITORY / "voice"), pip_call)


if __name__ == "__main__":
    unittest.main()
