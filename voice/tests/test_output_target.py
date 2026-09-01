from __future__ import annotations

import json
import os
import stat
import subprocess
from types import SimpleNamespace

import pytest

from murmur_voice.config import ConfigError
from murmur_voice.output_target import (
    MAX_CLIPBOARD_TEXT_UTF8_BYTES,
    ClipboardError,
    ClipboardWriter,
    OutputTargetConfig,
    load_output_target_config,
    save_output_target_config,
)


def _trusted_binary(_path):
    return SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)


_TEST_UID = 1000


def _trusted_display_socket(path):
    value = os.fspath(path)
    if value == "/run/user/1000":
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=_TEST_UID)
    if value == "/tmp/.X11-unix":
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o1777, st_uid=0)
    if value in {"/run/user/1000/wayland-0", "/tmp/.X11-unix/X0"}:
        return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o700, st_uid=_TEST_UID)
    raise FileNotFoundError


def _successful_socket_probe(_path, timeout):
    assert 0 < timeout <= 1


def _writer(**options):
    defaults = {
        "socket_metadata_reader": _trusted_display_socket,
        "socket_probe": _successful_socket_probe,
        "uid_reader": lambda: _TEST_UID,
    }
    defaults.update(options)
    return ClipboardWriter(**defaults)


def test_missing_private_config_defaults_to_caret_without_creating_a_file(tmp_path):
    path = tmp_path / "missing" / "output-target.json"

    assert load_output_target_config(path) == OutputTargetConfig("caret")
    assert not path.exists()


@pytest.mark.parametrize("target", ("caret", "clipboard"))
def test_private_config_round_trip_is_strict_atomic_and_private(tmp_path, target):
    path = tmp_path / "private" / "output-target.json"

    destination = save_output_target_config(target, path)

    assert destination == path
    assert load_output_target_config(path) == OutputTargetConfig(target)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "target": target,
    }
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".output-target.json.*"))


@pytest.mark.parametrize(
    "document",
    (
        {"version": 2, "target": "caret"},
        {"version": 1, "target": "remote"},
        {"version": 1, "target": "clipboard", "extra": True},
        {"version": True, "target": "clipboard"},
        {"version": 1, "mode": "clipboard"},
    ),
)
def test_existing_config_is_strict_and_never_coerced(tmp_path, document):
    path = tmp_path / "output-target.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ConfigError):
        load_output_target_config(path)


def test_output_target_rejects_public_or_linked_private_file(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"target":"clipboard"}\n', encoding="utf-8")
    target.chmod(0o600)
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)

    with pytest.raises(ConfigError):
        load_output_target_config(linked)

    target.chmod(0o644)
    with pytest.raises(ConfigError):
        load_output_target_config(target)


def test_clipboard_preflight_selects_a_trusted_absolute_backend_without_running_it():
    calls = []
    writer = _writer(
        runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        metadata_reader=_trusted_binary,
        environment={
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        },
    )

    writer.preflight()

    assert writer.backend == "wl-copy"
    assert calls == []


@pytest.mark.parametrize(
    "metadata",
    (
        SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=1000),
        SimpleNamespace(st_mode=stat.S_IFREG | 0o775, st_uid=0),
        SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0),
        SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0),
        SimpleNamespace(st_mode=stat.S_IFREG | 0o700, st_uid=0),
    ),
)
def test_clipboard_preflight_rejects_untrusted_or_non_executable_backends(metadata):
    writer = _writer(
        metadata_reader=lambda _path: metadata,
        environment={"DISPLAY": ":0"},
    )

    with pytest.raises(ClipboardError, match="unavailable"):
        writer.preflight()


def test_clipboard_preflight_requires_a_matching_local_display_and_binary():
    missing = _writer(
        metadata_reader=lambda _path: (_ for _ in ()).throw(FileNotFoundError()),
        environment={"DISPLAY": ":0"},
    )
    headless = _writer(
        metadata_reader=_trusted_binary,
        environment={},
    )

    with pytest.raises(ClipboardError, match="unavailable"):
        missing.preflight()
    with pytest.raises(ClipboardError, match="unavailable"):
        headless.preflight()


@pytest.mark.parametrize(
    "environment",
    (
        {"DISPLAY": "localhost:10.0"},
        {"DISPLAY": "example.com:0"},
        {"DISPLAY": ":0/../../private"},
        {
            "WAYLAND_DISPLAY": "../wayland-0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        },
        {
            "WAYLAND_DISPLAY": "/tmp/foreign.sock",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        },
        {
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_RUNTIME_DIR": "/run/user/1000/../1000",
        },
    ),
)
def test_clipboard_preflight_rejects_remote_or_traversing_display_values(environment):
    writer = _writer(
        metadata_reader=_trusted_binary,
        environment=environment,
    )

    with pytest.raises(ClipboardError, match="unavailable"):
        writer.preflight()


@pytest.mark.parametrize("failure", ("missing", "wrong-owner", "not-socket"))
def test_clipboard_preflight_rejects_missing_or_untrusted_display_socket(failure):
    def metadata(path):
        value = os.fspath(path)
        if value != "/tmp/.X11-unix/X0":
            return _trusted_display_socket(path)
        if failure == "missing":
            raise FileNotFoundError
        if failure == "wrong-owner":
            return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o700, st_uid=2000)
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o700, st_uid=_TEST_UID)

    writer = _writer(
        metadata_reader=_trusted_binary,
        socket_metadata_reader=metadata,
        environment={"DISPLAY": ":0"},
    )

    with pytest.raises(ClipboardError, match="unavailable"):
        writer.preflight()


def test_clipboard_preflight_rejects_stale_unconnectable_socket_with_bounded_probe():
    probes = []

    def fail_probe(path, timeout):
        probes.append((os.fspath(path), timeout))
        raise ConnectionRefusedError

    writer = _writer(
        metadata_reader=_trusted_binary,
        socket_probe=fail_probe,
        environment={"DISPLAY": ":0"},
    )

    with pytest.raises(ClipboardError, match="unavailable"):
        writer.preflight()
    assert probes == [("/tmp/.X11-unix/X0", pytest.approx(0.25))]


def test_clipboard_preflight_falls_back_from_stale_wayland_to_live_local_x11():
    probes = []

    def probe(path, timeout):
        value = os.fspath(path)
        probes.append((value, timeout))
        if value.endswith("/wayland-0"):
            raise ConnectionRefusedError

    writer = _writer(
        metadata_reader=_trusted_binary,
        socket_probe=probe,
        environment={
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "DISPLAY": ":0.0",
        },
    )

    writer.preflight()

    assert writer.backend == "xclip"
    assert [path for path, _timeout in probes] == [
        "/run/user/1000/wayland-0",
        "/tmp/.X11-unix/X0",
    ]
    assert all(0 < timeout <= 1 for _path, timeout in probes)


def test_clipboard_preflight_accepts_host_root_mapped_to_overflow_uid():
    def namespace_binary(_path):
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=65534)

    def namespace_socket(path):
        value = os.fspath(path)
        if value == "/tmp/.X11-unix":
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o1777, st_uid=65534)
        if value == "/tmp/.X11-unix/X0":
            return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o777, st_uid=1000)
        raise FileNotFoundError

    writer = ClipboardWriter(
        metadata_reader=namespace_binary,
        socket_metadata_reader=namespace_socket,
        socket_probe=_successful_socket_probe,
        uid_reader=lambda: 1000,
        uid_map_reader=lambda: "1000 1000 1\n",
        overflow_uid_reader=lambda: 65534,
        environment={"DISPLAY": ":0"},
    )

    writer.preflight()

    assert writer.backend == "xclip"


def test_clipboard_preflight_rejects_overflow_owner_without_user_namespace():
    writer = ClipboardWriter(
        metadata_reader=lambda _path: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o755,
            st_uid=65534,
        ),
        socket_metadata_reader=_trusted_display_socket,
        socket_probe=_successful_socket_probe,
        uid_reader=lambda: 1000,
        uid_map_reader=lambda: "0 0 4294967295\n",
        overflow_uid_reader=lambda: 65534,
        environment={"DISPLAY": ":0"},
    )

    with pytest.raises(ClipboardError, match="unavailable"):
        writer.preflight()


def test_clipboard_write_uses_only_bounded_stdin_and_never_transcript_argv():
    calls = []
    private_text = "远程 private transcript"

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    writer = _writer(
        runner=runner,
        metadata_reader=_trusted_binary,
        environment={
            "DISPLAY": ":0",
            "HOME": "/home/example",
            "XAUTHORITY": "/home/example/.Xauthority",
            "UNRELATED_SECRET": "must-not-reach-helper",
        },
    )
    writer.preflight()

    writer.write(private_text)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["/usr/bin/xclip", "-selection", "clipboard", "-in"]
    assert private_text not in " ".join(command)
    assert kwargs["input"] == private_text.encode("utf-8")
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["timeout"] > 0
    assert kwargs["check"] is False
    assert kwargs["close_fds"] is True
    assert kwargs["env"] == {
        "DISPLAY": ":0",
        "HOME": "/home/example",
        "XAUTHORITY": "/home/example/.Xauthority",
    }


def test_clipboard_write_is_bounded_and_reports_only_content_free_errors():
    private_text = "x" * (MAX_CLIPBOARD_TEXT_UTF8_BYTES + 1)
    writer = _writer(
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("private transcript", 1)
        ),
        metadata_reader=_trusted_binary,
        environment={"DISPLAY": ":0"},
    )
    writer.preflight()

    with pytest.raises(ClipboardError, match="too large") as oversized:
        writer.write(private_text)
    assert private_text not in str(oversized.value)

    with pytest.raises(ClipboardError, match="failed") as timed_out:
        writer.write("private transcript")
    assert "private transcript" not in str(timed_out.value)

    with pytest.raises(ClipboardError, match="invalid"):
        writer.write("\ud800")


def test_clipboard_write_requires_preflight_and_a_nonempty_string():
    writer = _writer(
        metadata_reader=_trusted_binary,
        environment={"DISPLAY": ":0"},
    )

    with pytest.raises(ClipboardError, match="preflight"):
        writer.write("private")
    writer.preflight()
    with pytest.raises(ClipboardError, match="empty"):
        writer.write("")
    with pytest.raises(TypeError):
        writer.write(b"private")  # type: ignore[arg-type]
