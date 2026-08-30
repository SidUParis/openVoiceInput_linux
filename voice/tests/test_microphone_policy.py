from __future__ import annotations

import json
import stat

import pytest

from murmur_voice.config import ConfigError
from murmur_voice.microphone_policy import (
    DEFAULT_MICROPHONE_PRIORITY,
    MICROPHONE_CATEGORIES,
    MICROPHONE_POLICY_SCHEMA_VERSION,
    MAX_MICROPHONE_POLICY_BYTES,
    MicrophonePolicyConfig,
    MicrophoneSourcePreference,
    default_microphone_policy_config_path,
    load_microphone_policy_config,
    normalize_microphone_priority,
    normalize_microphone_source_preferences,
    save_microphone_policy_config,
)


def _write(path, document, mode=0o600):
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(mode)


def _document(*, priority=None, preferred_sources=None):
    return {
        "version": MICROPHONE_POLICY_SCHEMA_VERSION,
        "priority": list(priority or DEFAULT_MICROPHONE_PRIORITY),
        "preferred_sources": preferred_sources or {},
    }


def test_missing_policy_uses_reviewed_four_category_default(tmp_path):
    config = load_microphone_policy_config(tmp_path / "missing.json")

    assert config.priority == ("dji", "headset", "external", "built-in")
    assert config.priority == MICROPHONE_CATEGORIES
    assert config.preferred_sources == ()


def test_default_path_honors_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert default_microphone_policy_config_path() == (
        tmp_path / "murmur-ime" / "microphone-priority.json"
    )


def test_save_and_load_private_policy_with_exact_source_preferences(tmp_path):
    path = tmp_path / "private" / "microphone-priority.json"
    priority = ("headset", "dji", "built-in", "external")
    sources = {
        "headset": "bluez_input.poly.headset-head-unit",
        "dji": "alsa_input.usb-DJI_Wireless_Mic_Rx.analog-stereo",
    }

    result = save_microphone_policy_config(
        priority,
        path,
        preferred_sources=sources,
    )
    loaded = load_microphone_policy_config(path)

    assert result == path
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert loaded.priority == priority
    assert loaded.preferred_source_for("headset") == sources["headset"]
    assert loaded.preferred_source_for("dji") == sources["dji"]
    assert loaded.preferred_source_for("external") is None
    assert sources["dji"] not in repr(loaded)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "priority": list(priority),
        "preferred_sources": {
            "dji": sources["dji"],
            "headset": sources["headset"],
        },
    }
    assert not list(path.parent.glob(".microphone-priority.json.*"))


def test_priority_only_save_preserves_existing_exact_choices(tmp_path):
    path = tmp_path / "microphone-priority.json"
    source = "bluez_input.exact.headset-head-unit"
    save_microphone_policy_config(
        DEFAULT_MICROPHONE_PRIORITY,
        path,
        preferred_sources={"headset": source},
    )

    save_microphone_policy_config(
        ("built-in", "headset", "external", "dji"),
        path,
    )

    loaded = load_microphone_policy_config(path)
    assert loaded.priority == ("built-in", "headset", "external", "dji")
    assert loaded.preferred_source_for("headset") == source


def test_explicit_empty_preferences_clear_existing_choices(tmp_path):
    path = tmp_path / "microphone-priority.json"
    save_microphone_policy_config(
        DEFAULT_MICROPHONE_PRIORITY,
        path,
        preferred_sources={"headset": "bluez_input.old"},
    )

    save_microphone_policy_config(
        DEFAULT_MICROPHONE_PRIORITY,
        path,
        preferred_sources={},
    )

    assert load_microphone_policy_config(path).preferred_sources == ()


@pytest.mark.parametrize(
    "priority",
    (
        ["dji", "headset", "external"],
        ["dji", "headset", "external", "external"],
        ["dji", "headset", "external", "unknown"],
        ["dji", "headset", "external", 7],
        "dji,headset,external,built-in",
        None,
    ),
)
def test_priority_rejects_missing_duplicate_unknown_and_wrong_types(priority):
    with pytest.raises(ConfigError):
        normalize_microphone_priority(priority)


@pytest.mark.parametrize(
    "preferences",
    (
        {"unknown": "alsa_input.test"},
        {"headset": ""},
        {"headset": "bad\nsource"},
        {"headset": 42},
        [
            MicrophoneSourcePreference("dji", "alsa_input.first"),
            MicrophoneSourcePreference("dji", "alsa_input.second"),
        ],
        [{"category": "dji"}],
        "not-an-object",
    ),
)
def test_exact_preferences_reject_invalid_values(preferences):
    with pytest.raises(ConfigError):
        normalize_microphone_source_preferences(preferences)


@pytest.mark.parametrize(
    "document",
    (
        {"version": 1, "priority": list(DEFAULT_MICROPHONE_PRIORITY)},
        {
            "version": 1,
            "priority": list(DEFAULT_MICROPHONE_PRIORITY),
            "preferred_sources": {},
            "extra": True,
        },
        {
            "version": True,
            "priority": list(DEFAULT_MICROPHONE_PRIORITY),
            "preferred_sources": {},
        },
        {
            "version": 2,
            "priority": list(DEFAULT_MICROPHONE_PRIORITY),
            "preferred_sources": {},
        },
    ),
)
def test_load_rejects_unsupported_schema_or_fields(tmp_path, document):
    path = tmp_path / "microphone-priority.json"
    _write(path, document)

    with pytest.raises(ConfigError):
        load_microphone_policy_config(path)


def test_load_rejects_duplicate_json_fields(tmp_path):
    path = tmp_path / "microphone-priority.json"
    path.write_text(
        '{"version":1,"version":1,"priority":'
        '["dji","headset","external","built-in"],"preferred_sources":{}}',
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(ConfigError, match="not valid"):
        load_microphone_policy_config(path)


def test_load_rejects_public_oversized_and_symlink_files(tmp_path):
    path = tmp_path / "microphone-priority.json"
    _write(path, _document(), mode=0o644)
    with pytest.raises(ConfigError, match="permissions"):
        load_microphone_policy_config(path)

    path.write_bytes(b"x" * (MAX_MICROPHONE_POLICY_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(ConfigError, match="too large"):
        load_microphone_policy_config(path)

    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    _write(target, _document())
    link.symlink_to(target)
    with pytest.raises(ConfigError, match="safely"):
        load_microphone_policy_config(link)


def test_save_refuses_symlink_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ConfigError, match="user-owned"):
        save_microphone_policy_config(
            DEFAULT_MICROPHONE_PRIORITY,
            link / "microphone-priority.json",
            preferred_sources={},
        )


def test_dataclass_validates_direct_construction():
    with pytest.raises(ConfigError):
        MicrophonePolicyConfig(priority=("dji", "headset", "external", "dji"))
