from __future__ import annotations

import json
import stat

import pytest

from murmur_voice.config import ConfigError, load_config, save_api_key


def _write(path, document, mode=0o600):
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(mode)


def test_load_key_only_config_uses_reviewed_defaults(tmp_path):
    path = tmp_path / "voice.json"
    _write(path, {"api_key": "test-key"})

    config = load_config(path)
    settings = config.provider_settings()

    assert config.api_key == "test-key"
    assert settings["endpoint"].endswith("/bigmodel_async")
    assert settings["resource_id"] == "volc.seedasr.sauc.duration"
    assert settings["enable_nonstream"] is True
    assert settings["chunk_ms"] == 200
    assert "test-key" not in repr(config)


def test_config_rejects_extra_fields_and_public_permissions(tmp_path):
    path = tmp_path / "voice.json"
    _write(path, {"api_key": "test", "endpoint": "wss://example.test"})
    with pytest.raises(ConfigError, match="only api_key"):
        load_config(path)

    _write(path, {"api_key": "test"}, mode=0o644)
    with pytest.raises(ConfigError, match="permissions"):
        load_config(path)


def test_config_rejects_symlink(tmp_path):
    target = tmp_path / "target.json"
    link = tmp_path / "voice.json"
    _write(target, {"api_key": "test"})
    link.symlink_to(target)

    with pytest.raises(ConfigError, match="safely"):
        load_config(link)


def test_save_api_key_is_atomic_private_and_loadable(tmp_path):
    path = tmp_path / "private" / "voice.json"

    result = save_api_key("  test-key  ", path)

    assert result == path
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_config(path).api_key == "test-key"
    assert not list(path.parent.glob(".voice.json.*"))


def test_save_api_key_refuses_symlink_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "private"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ConfigError, match="user-owned"):
        save_api_key("test-key", link / "voice.json")
