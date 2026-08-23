from __future__ import annotations

import json
import os
import stat

import pytest

from murmur_voice.config import (
    MAX_VOCABULARY_ENTRIES,
    ConfigError,
    VoiceConfig,
    load_config,
    load_vocabulary,
    load_vocabulary_import,
    save_api_key,
    save_vocabulary,
)


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
    assert "hotwords" not in settings
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


def test_missing_vocabulary_is_an_empty_optional_default(tmp_path):
    assert load_vocabulary(tmp_path / "vocabulary.json") == ()
    assert "hotwords" not in VoiceConfig("test-key").provider_settings()


def test_vocabulary_is_private_trimmed_and_stably_casefold_deduplicated(tmp_path):
    path = tmp_path / "private" / "vocabulary.json"

    result = save_vocabulary(["  Alpha  ", "alpha", "Straße", "STRASSE", "中文"], path)

    assert result == path
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_vocabulary(path) == ("Alpha", "Straße", "中文")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "terms": ["Alpha", "Straße", "中文"]
    }
    assert "Alpha" not in repr(VoiceConfig("test-key", ("Alpha",)))
    assert not list(path.parent.glob(".vocabulary.json.*"))


@pytest.mark.parametrize(
    "terms",
    (
        ["x" * 65],
        ["bad\x00term"],
        ["bad\rterm"],
        ["bad\nterm"],
        [""],
        ["   "],
        [123],
        ["term"] * (MAX_VOCABULARY_ENTRIES + 1),
    ),
)
def test_vocabulary_rejects_invalid_entries(tmp_path, terms):
    with pytest.raises(ConfigError):
        save_vocabulary(terms, tmp_path / "vocabulary.json")


def test_vocabulary_accepts_exactly_64_unicode_characters(tmp_path):
    term = "界" * 64
    path = tmp_path / "vocabulary.json"

    save_vocabulary([term], path)

    assert load_vocabulary(path) == (term,)


def test_vocabulary_accepts_exactly_200_unique_entries(tmp_path):
    terms = [f"term-{index}" for index in range(MAX_VOCABULARY_ENTRIES)]
    path = tmp_path / "vocabulary.json"

    save_vocabulary(terms, path)

    assert load_vocabulary(path) == tuple(terms)


def test_vocabulary_rejects_extra_fields_public_permissions_and_symlink(tmp_path):
    path = tmp_path / "vocabulary.json"
    _write(path, {"terms": ["private"], "enabled": True})
    with pytest.raises(ConfigError, match="only terms"):
        load_vocabulary(path)

    _write(path, {"terms": ["private"]}, mode=0o644)
    with pytest.raises(ConfigError, match="permissions"):
        load_vocabulary(path)

    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    _write(target, {"terms": ["private"]})
    link.symlink_to(target)
    with pytest.raises(ConfigError, match="safely"):
        load_vocabulary(link)

    with pytest.raises(ConfigError, match="unsafe"):
        save_vocabulary(["replacement"], link)


def test_private_text_import_is_deduplicated_without_exposing_terms(tmp_path):
    source = tmp_path / "terms.txt"
    source.write_text("  Alpha  \nalpha\n中文\n", encoding="utf-8")
    source.chmod(0o600)

    assert load_vocabulary_import(source) == ("Alpha", "中文")

    source.write_text("bad\r\n", encoding="utf-8")
    source.chmod(0o600)
    with pytest.raises(ConfigError, match="control"):
        load_vocabulary_import(source)


def test_text_import_rejects_public_file_and_symlink(tmp_path):
    source = tmp_path / "terms.txt"
    source.write_text("private\n", encoding="utf-8")
    source.chmod(0o644)
    with pytest.raises(ConfigError, match="permissions"):
        load_vocabulary_import(source)

    source.chmod(0o600)
    link = tmp_path / "terms-link.txt"
    link.symlink_to(source)
    with pytest.raises(ConfigError, match="safely"):
        load_vocabulary_import(link)


def test_text_import_rejects_nonregular_file_without_blocking(tmp_path):
    fifo = tmp_path / "terms.fifo"
    os.mkfifo(fifo, mode=0o600)

    with pytest.raises(ConfigError, match="regular file"):
        load_vocabulary_import(fifo)
