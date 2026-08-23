from __future__ import annotations

import logging
from types import SimpleNamespace

from gi.repository import GLib

from murmur_voice.engine_restore import EngineRestoreState
from murmur_voice.preedit import AcquireResult, PREEDIT_ENGINE, PreeditClient


class FakeRunner:
    def __init__(self, engine="rime"):
        self.engine = engine
        self.calls = []

    def __call__(self, command, **kwargs):
        del kwargs
        self.calls.append(list(command))
        arguments = list(command)[list(command).index("ibus") + 1 :]
        if arguments == ["engine"]:
            return SimpleNamespace(stdout=self.engine + "\n", returncode=0)
        if len(arguments) == 2 and arguments[0] == "engine":
            self.engine = arguments[1]
            return SimpleNamespace(stdout="", returncode=1)
        raise AssertionError(arguments)


class FakeProxy:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.fail_methods = set()
        self.name_owner = ":1.50"

    def call_sync(self, method, parameters, *args):
        del args
        self.calls.append((method, parameters.unpack()))
        if method in self.fail_methods:
            raise RuntimeError("remote error containing TOP-SECRET-TEXT")
        return GLib.Variant("(b)", (self.responses.get(method, True),))

    def get_name_owner(self):
        return self.name_owner


def _client(proxy=None, runner=None, **kwargs):
    proxy = proxy or FakeProxy()
    runner = runner or FakeRunner()
    client = PreeditClient(
        proxy_factory=lambda: proxy,
        command_provider=lambda tool: [[tool]],
        command_runner=runner,
        acquire_retry_seconds=0,
        **kwargs,
    )
    return client, runner, proxy


def _restore_state(tmp_path):
    parent = tmp_path / "runtime" / "murmur-ime"
    parent.mkdir(parents=True, mode=0o700)
    parent.chmod(0o700)
    return EngineRestoreState(parent / "previous-ibus-engine")


def test_acquire_partial_final_reuses_proxy_and_restores_rime(tmp_path):
    state = _restore_state(tmp_path)
    client, runner, proxy = _client(restore_state=state)

    assert client.acquire_result("utterance-1") is AcquireResult.ACQUIRED
    assert runner.engine == PREEDIT_ENGINE
    assert state.load() == "rime"
    assert client.partial("utterance-1", 1, "草稿")
    assert not client.partial("utterance-1", 1, "重复")
    assert client.final("utterance-1", 2, "最终")

    assert runner.engine == "rime"
    assert [method for method, _ in proxy.calls] == [
        "Acquire",
        "Partial",
        "Final",
    ]
    assert not client.active
    assert state.load() is None


def test_missing_service_is_unavailable_and_restores_engine(tmp_path):
    proxy = FakeProxy()
    proxy.name_owner = None
    proxy.fail_methods.add("Acquire")
    state = _restore_state(tmp_path)
    client, runner, _ = _client(proxy=proxy, restore_state=state)

    assert client.acquire_result("utterance-1") is AcquireResult.UNAVAILABLE
    assert runner.engine == "rime"
    assert state.load() is None


def test_explicit_rejection_is_not_reported_as_unavailable(tmp_path):
    proxy = FakeProxy()
    proxy.responses["Acquire"] = False
    state = _restore_state(tmp_path)
    client, runner, _ = _client(proxy=proxy, restore_state=state)

    assert client.acquire_result("utterance-1") is AcquireResult.REJECTED
    assert runner.engine == "rime"
    assert state.load() is None


def test_acquire_retries_fake_focus_then_succeeds(tmp_path):
    proxy = FakeProxy()
    outcomes = iter((False, False, True))

    def call_sync(method, parameters, *args):
        del args
        proxy.calls.append((method, parameters.unpack()))
        return GLib.Variant("(b)", (next(outcomes),))

    proxy.call_sync = call_sync
    now = [0.0]
    runner = FakeRunner()
    client = PreeditClient(
        proxy_factory=lambda: proxy,
        command_provider=lambda tool: [[tool]],
        command_runner=runner,
        acquire_retry_seconds=0.2,
        acquire_retry_interval=0.05,
        monotonic=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        restore_state=_restore_state(tmp_path),
    )

    assert client.acquire_result("utterance-1") is AcquireResult.ACQUIRED
    assert [method for method, _ in proxy.calls] == [
        "Acquire",
        "Acquire",
        "Acquire",
    ]


def test_transcript_and_remote_exception_are_not_logged(tmp_path, caplog):
    proxy = FakeProxy()
    client, runner, _ = _client(
        proxy=proxy,
        restore_state=_restore_state(tmp_path),
    )
    assert client.acquire("utterance-1")
    proxy.fail_methods.add("Partial")

    with caplog.at_level(logging.WARNING):
        assert not client.partial("utterance-1", 1, "TOP-SECRET-TEXT")
    assert "TOP-SECRET-TEXT" not in caplog.text
    client.cancel("utterance-1")
    assert runner.engine == "rime"
