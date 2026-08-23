from __future__ import annotations

import logging
import os
import stat
from types import SimpleNamespace

import pytest

from murmur_voice import engine_restore
from murmur_voice.engine_restore import (
    PREEDIT_ENGINE,
    EngineRestoreState,
    IBusEngineCommands,
    RestoreError,
    parse_engine_name,
    restore_saved_engine,
)


class _IBusRunner:
    def __init__(self, engine=PREEDIT_ENGINE, *, fail_switch=False):
        self.engine = engine
        self.fail_switch = fail_switch
        self.calls = []

    def __call__(self, command, **kwargs):
        del kwargs
        self.calls.append(list(command))
        arguments = list(command)[list(command).index("ibus") + 1 :]
        if arguments == ["engine"]:
            return SimpleNamespace(stdout=f"{self.engine}\n", returncode=0)
        if len(arguments) == 2 and arguments[0] == "engine":
            if not self.fail_switch:
                self.engine = arguments[1]
            return SimpleNamespace(stdout="", returncode=1)
        raise AssertionError(arguments)


def _state(tmp_path) -> EngineRestoreState:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    return EngineRestoreState(runtime / "murmur-ime" / "previous-ibus-engine")


def test_record_then_crash_helper_restores_exact_engine_and_clears_state(tmp_path):
    state = _state(tmp_path)
    state.record("libpinyin")

    assert state.load() == "libpinyin"
    assert stat.S_IMODE(state.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state.path.stat().st_mode) == 0o600

    active = [PREEDIT_ENGINE]

    def switch(engine):
        active[0] = engine
        return True

    assert restore_saved_engine(
        state,
        current_engine=lambda: active[0],
        set_engine=switch,
    )
    assert active[0] == "libpinyin"
    assert not state.path.exists()
    assert restore_saved_engine(
        state,
        current_engine=lambda: active[0],
        set_engine=switch,
    )


def test_failed_crash_restore_preserves_record_for_a_later_retry(tmp_path):
    state = _state(tmp_path)
    state.record("anthy")

    assert not restore_saved_engine(
        state,
        current_engine=lambda: PREEDIT_ENGINE,
        set_engine=lambda engine: False,
    )
    assert state.load() == "anthy"


def test_independent_command_helper_restores_without_daemon_state(tmp_path):
    state = _state(tmp_path)
    state.record("xkb:us::eng")
    runner = _IBusRunner()
    commands = IBusEngineCommands(
        command_provider=lambda tool: [[tool]],
        command_runner=runner,
    )

    assert restore_saved_engine(
        state,
        current_engine=commands.current_engine,
        set_engine=commands.set_engine,
    )
    assert runner.engine == "xkb:us::eng"
    assert not state.path.exists()
    assert ["ibus", "engine", "xkb:us::eng"] in runner.calls


def test_stale_record_never_overrides_a_newer_user_engine_choice(tmp_path):
    state = _state(tmp_path)
    state.record("libpinyin")
    switches = []

    assert restore_saved_engine(
        state,
        current_engine=lambda: "anthy",
        set_engine=lambda engine: switches.append(engine) or True,
    )
    assert switches == []
    assert not state.path.exists()


def test_symlink_state_is_refused_without_touching_target_or_logging_name(
    tmp_path, caplog
):
    state = _state(tmp_path)
    state.path.parent.mkdir(mode=0o700)
    external = tmp_path / "external"
    external.write_text("private-engine-name\n", encoding="ascii")
    external.chmod(0o600)
    state.path.symlink_to(external)

    with caplog.at_level(logging.WARNING):
        assert not restore_saved_engine(
            state,
            current_engine=lambda: PREEDIT_ENGINE,
            set_engine=lambda engine: True,
        )

    assert external.read_text(encoding="ascii") == "private-engine-name\n"
    assert state.path.is_symlink()
    assert "private-engine-name" not in caplog.text


def test_symlink_parent_and_public_state_permissions_are_refused(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (runtime / "murmur-ime").symlink_to(target, target_is_directory=True)
    linked_state = EngineRestoreState(runtime / "murmur-ime" / "previous-ibus-engine")

    with pytest.raises(RestoreError):
        linked_state.load()

    (runtime / "murmur-ime").unlink()
    (runtime / "murmur-ime").mkdir(mode=0o700)
    linked_state.path.write_text("libpinyin\n", encoding="ascii")
    linked_state.path.chmod(0o644)
    with pytest.raises(RestoreError):
        linked_state.load()


def test_wrong_owner_and_invalid_or_existing_records_are_refused(tmp_path, monkeypatch):
    state = _state(tmp_path)
    with pytest.raises(RestoreError):
        state.record("engine name with spaces")

    state.record("libpinyin")
    with pytest.raises(RestoreError):
        state.record("anthy")

    real_uid = os.getuid()
    monkeypatch.setattr(engine_restore.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(RestoreError):
        state.load()


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("xkb:us::eng\n", "xkb:us::eng"),
        ("rime\nextra\n", None),
        ("rime with spaces\n", None),
        ("../rime\n", None),
        ("é\n", None),
        ("x" * 257 + "\n", None),
    ],
)
def test_engine_name_parser_is_strict(output, expected):
    assert parse_engine_name(output) == expected
