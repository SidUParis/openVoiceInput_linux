from __future__ import annotations

import json
import os
import stat

import pytest

from murmur_voice.config import (
    CORRECTIONS_SCHEMA_VERSION,
    MAX_CORRECTIONS_BYTES,
    MAX_CORRECTION_PAIRS,
    CorrectionPair,
    MAX_VOCABULARY_ENTRIES,
    ConfigError,
    VoiceConfig,
    delete_api_key,
    load_config,
    load_corrections,
    load_vocabulary,
    load_vocabulary_import,
    save_api_key,
    save_corrections,
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


def test_delete_api_key_removes_only_private_file_and_fsyncs_parent(
    tmp_path, monkeypatch
):
    path = tmp_path / "private" / "voice.json"
    save_api_key("test-key", path)
    real_fsync = os.fsync
    fsynced_modes = []

    def recording_fsync(descriptor):
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    assert delete_api_key(path) is True
    assert not path.exists()
    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)
    assert delete_api_key(path) is False


@pytest.mark.parametrize("unsafe_kind", ("symlink", "public", "directory", "foreign"))
def test_delete_api_key_rejects_unsafe_targets_without_removing_them(
    tmp_path, monkeypatch, unsafe_kind
):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "voice.json"
    target = directory / "target.json"

    if unsafe_kind == "symlink":
        _write(target, {"api_key": "private-key-sentinel"})
        path.symlink_to(target)
    elif unsafe_kind == "public":
        _write(path, {"api_key": "private-key-sentinel"}, mode=0o644)
    elif unsafe_kind == "directory":
        path.mkdir(mode=0o700)
    else:
        _write(path, {"api_key": "private-key-sentinel"})
        current_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: current_uid + 1)

    with pytest.raises(ConfigError) as captured:
        delete_api_key(path)

    assert "private-key-sentinel" not in str(captured.value)
    assert path.exists() or path.is_symlink()


def test_delete_api_key_rejects_public_or_symlink_directory(tmp_path):
    public_directory = tmp_path / "public"
    public_directory.mkdir(mode=0o755)
    public_path = public_directory / "voice.json"
    _write(public_path, {"api_key": "private-key-sentinel"})

    with pytest.raises(ConfigError, match="safely"):
        delete_api_key(public_path)
    assert public_path.exists()

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    link = tmp_path / "directory-link"
    link.symlink_to(private_directory, target_is_directory=True)
    linked_path = link / "voice.json"
    _write(private_directory / "voice.json", {"api_key": "private-key-sentinel"})

    with pytest.raises(ConfigError, match="safely"):
        delete_api_key(linked_path)
    assert (private_directory / "voice.json").exists()


def test_missing_vocabulary_is_an_empty_optional_default(tmp_path):
    assert load_vocabulary(tmp_path / "vocabulary.json") == ()
    assert "hotwords" not in VoiceConfig("test-key").provider_settings()


def test_missing_corrections_are_an_empty_optional_default(tmp_path):
    assert load_corrections(tmp_path / "corrections.json") == ()
    assert "corrections" not in VoiceConfig("test-key").provider_settings()


def test_corrections_are_private_trimmed_deduplicated_and_repr_hidden(tmp_path):
    path = tmp_path / "private" / "corrections.json"
    private_wrong = "deep seek"
    private_canonical = "DeepSeek"

    result = save_corrections(
        [
            {"wrong": f"  {private_wrong}  ", "canonical": private_canonical},
            CorrectionPair(private_wrong, private_canonical),
            {"wrong": "欧盆爱", "canonical": "OpenAI"},
        ],
        path,
    )

    expected = (
        CorrectionPair(private_wrong, private_canonical),
        CorrectionPair("欧盆爱", "OpenAI"),
    )
    assert result == path
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_corrections(path) == expected
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": CORRECTIONS_SCHEMA_VERSION,
        "pairs": [
            {"wrong": private_wrong, "canonical": private_canonical},
            {"wrong": "欧盆爱", "canonical": "OpenAI"},
        ],
    }
    config = VoiceConfig("test-key", corrections=expected)
    assert config.provider_settings()["corrections"] == expected
    assert private_wrong not in repr(expected[0])
    assert private_canonical not in repr(expected[0])
    assert private_wrong not in repr(config)
    assert private_canonical not in repr(config)
    assert not list(path.parent.glob(".corrections.json.*"))


@pytest.mark.parametrize(
    "pairs",
    (
        [{"wrong": "", "canonical": "valid"}],
        [{"wrong": "   ", "canonical": "valid"}],
        [{"wrong": "valid", "canonical": ""}],
        [{"wrong": "bad\x00form", "canonical": "valid"}],
        [{"wrong": "bad\tform", "canonical": "valid"}],
        [{"wrong": "valid", "canonical": "bad\nform"}],
        [{"wrong": "界" * 65, "canonical": "valid"}],
        [{"wrong": "valid", "canonical": "界" * 65}],
        [{"wrong": 123, "canonical": "valid"}],
        [{"wrong": "valid", "canonical": None}],
        [{"wrong": "valid"}],
        [{"wrong": "valid", "canonical": "target", "extra": True}],
        [
            {"wrong": f"wrong-{index}", "canonical": f"right-{index}"}
            for index in range(MAX_CORRECTION_PAIRS + 1)
        ],
    ),
)
def test_corrections_reject_invalid_pairs(tmp_path, pairs):
    with pytest.raises(ConfigError):
        save_corrections(pairs, tmp_path / "corrections.json")


def test_corrections_reject_conflicting_duplicate_wrong_form(tmp_path):
    path = tmp_path / "corrections.json"

    with pytest.raises(ConfigError, match="conflicting") as captured:
        save_corrections(
            [
                {"wrong": "same wrong form", "canonical": "first target"},
                {"wrong": "same wrong form", "canonical": "second target"},
            ],
            path,
        )

    assert "same wrong form" not in str(captured.value)
    assert "first target" not in str(captured.value)
    assert "second target" not in str(captured.value)
    assert not path.exists()


def test_corrections_accept_exact_pair_and_unicode_character_limits(tmp_path):
    pairs = [
        {"wrong": f"wrong-{index}", "canonical": "界" * 64}
        for index in range(MAX_CORRECTION_PAIRS)
    ]
    path = tmp_path / "corrections.json"

    save_corrections(pairs, path)

    loaded = load_corrections(path)
    assert len(loaded) == MAX_CORRECTION_PAIRS
    assert loaded[-1].canonical == "界" * 64


@pytest.mark.parametrize(
    "document",
    (
        {"version": 1},
        {"pairs": []},
        {"version": 1, "pairs": [], "extra": True},
        {"version": True, "pairs": []},
        {"version": 2, "pairs": []},
        {"version": 1, "pairs": [{"wrong": "x", "canonical": "y", "x": 1}]},
    ),
)
def test_corrections_reject_unknown_schema_and_fields(tmp_path, document):
    path = tmp_path / "corrections.json"
    _write(path, document)

    with pytest.raises(ConfigError):
        load_corrections(path)


def test_corrections_reject_duplicate_json_fields_without_echoing_values(tmp_path):
    path = tmp_path / "corrections.json"
    private_first = "private-first-wrong"
    private_second = "private-second-wrong"
    path.write_text(
        '{"version":1,"pairs":[{"wrong":"'
        + private_first
        + '","wrong":"'
        + private_second
        + '","canonical":"target"}]}',
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(ConfigError) as captured:
        load_corrections(path)

    assert private_first not in str(captured.value)
    assert private_second not in str(captured.value)


def test_corrections_reject_public_oversized_and_symlink_files(tmp_path):
    path = tmp_path / "corrections.json"
    _write(path, {"version": 1, "pairs": []}, mode=0o644)
    with pytest.raises(ConfigError, match="permissions"):
        load_corrections(path)

    path.write_bytes(b"x" * (MAX_CORRECTIONS_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(ConfigError, match="too large"):
        load_corrections(path)

    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    _write(target, {"version": 1, "pairs": []})
    link.symlink_to(target)
    with pytest.raises(ConfigError, match="safely"):
        load_corrections(link)
    with pytest.raises(ConfigError, match="unsafe"):
        save_corrections([], link)


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
