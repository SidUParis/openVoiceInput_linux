from __future__ import annotations

import hashlib
import json
import shutil
import stat
import wave

import pytest

import murmur_voice.data_collection as data_collection
from murmur_voice.config import ConfigError
from murmur_voice.data_collection import (
    CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    DataCollectionError,
    DataCollectionRuntime,
    load_data_collection_config,
    save_data_collection_config,
)


def test_missing_config_is_disabled_and_begin_writes_nothing(tmp_path):
    path = tmp_path / "private" / "data-collection.json"
    runtime = DataCollectionRuntime(path, session_id="session-1")

    assert runtime.validate().enabled is False
    assert runtime.begin("utterance-1") is None
    assert not path.exists()
    assert runtime.close()


def test_config_round_trip_is_private_and_requires_absolute_path(tmp_path):
    path = tmp_path / "private" / "data-collection.json"
    selected = tmp_path / "selected"
    selected.mkdir()

    save_data_collection_config(True, selected, path)
    loaded = load_data_collection_config(path)

    assert loaded.enabled is True
    assert loaded.directory == selected
    assert loaded.dataset_id
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert str(selected) not in repr(loaded)
    marker = json.loads(
        (selected / "openvoiceinput-dataset-v1" / "dataset.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["dataset_id"] == loaded.dataset_id
    with pytest.raises(ConfigError, match="absolute"):
        save_data_collection_config(True, "relative", path)


def test_existing_dataset_children_cannot_be_replaced_by_symlinks(tmp_path):
    path = tmp_path / "private" / "data-collection.json"
    selected = tmp_path / "selected"
    outside = tmp_path / "outside"
    selected.mkdir()
    outside.mkdir(mode=0o755)
    save_data_collection_config(True, selected, path)
    pending = selected / "openvoiceinput-dataset-v1" / ".pending"
    pending.rmdir()
    pending.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError, match="unavailable"):
        save_data_collection_config(True, selected, path)

    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    "document",
    (
        {"version": 1, "enabled": True, "directory": None},
        {"version": 1, "enabled": 1, "directory": "/tmp"},
        {"version": 2, "enabled": False, "directory": None},
        {"version": 1, "enabled": False, "directory": None, "extra": True},
    ),
)
def test_config_rejects_invalid_documents_without_echoing_paths(tmp_path, document):
    path = tmp_path / "data-collection.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ConfigError):
        load_data_collection_config(path)


def test_runtime_reloads_enable_and_location_for_each_utterance(tmp_path):
    path = tmp_path / "private" / "data-collection.json"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    runtime = DataCollectionRuntime(path, session_id="session-1")

    save_data_collection_config(False, first, path)
    assert runtime.begin("utterance-1") is None
    save_data_collection_config(True, second, path)
    recorder = runtime.begin("utterance-2")

    assert recorder is not None
    recorder.discard()
    runtime.close()
    assert not list((second / "openvoiceinput-dataset-v1" / "utterances").iterdir())
    assert not (first / "openvoiceinput-dataset-v1").exists()


def test_completed_record_is_atomic_wav_plus_unreviewed_teacher_label(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    pcm = (b"\x01\x02" * 800) + (b"\x03\x04" * 400)
    recorder = runtime.begin("utterance-1")
    assert recorder is not None
    recorder._recorded_at_utc = "2026-08-30T12:00:00Z"

    recorder.add_audio(pcm[:1600])
    recorder.add_audio(pcm[1600:])
    recorder.commit("teacher final")
    assert runtime.wait_until_idle()

    final = selected / "openvoiceinput-dataset-v1" / "utterances" / "utterance-1"
    assert sorted(path.name for path in final.iterdir()) == ["audio.wav", "record.json"]
    assert not list((selected / "openvoiceinput-dataset-v1" / ".pending").iterdir())
    with wave.open(str(final / "audio.wav"), "rb") as audio:
        assert audio.getframerate() == SAMPLE_RATE
        assert audio.getnchannels() == CHANNELS
        assert audio.getsampwidth() == SAMPLE_WIDTH_BYTES
        assert audio.getnframes() == len(pcm) // SAMPLE_WIDTH_BYTES
        assert audio.readframes(audio.getnframes()) == pcm
    document = json.loads((final / "record.json").read_text(encoding="utf-8"))
    assert document["consent"] == "explicit-opt-in"
    assert document["audio"]["pcm_sha256"] == hashlib.sha256(pcm).hexdigest()
    assert (
        document["audio"]["file_sha256"]
        == hashlib.sha256((final / "audio.wav").read_bytes()).hexdigest()
    )
    assert document["labels"] == {
        "provider_final": {
            "text": "teacher final",
            "review_status": "teacher-unreviewed",
        },
        "spoken_verbatim": {"text": None, "review_status": "unreviewed"},
        "preferred_output": {"text": None, "review_status": "unreviewed"},
    }
    assert document["recorded_at_utc"] == "2026-08-30T12:00:00Z"
    assert runtime.close()


def test_cancel_or_empty_final_publishes_no_training_record(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-1")
    assert recorder is not None
    recorder.add_audio(b"\x00\x00" * 100)

    with pytest.raises(DataCollectionError, match="final"):
        recorder.commit("")

    assert not list((selected / "openvoiceinput-dataset-v1" / "utterances").iterdir())
    assert runtime.close()


def test_queue_overflow_discards_record_without_blocking_audio_callback(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-1")
    assert recorder is not None
    # Freeze the writer's ability to drain by marking the collector failed via
    # one malformed chunk; later valid calls remain no-ops and never block.
    recorder.add_audio(b"odd")
    for _ in range(1000):
        recorder.add_audio(b"\x00\x00")

    with pytest.raises(DataCollectionError):
        recorder.commit("teacher final")
    assert runtime.close()


def test_audio_limit_accepts_exact_bound_and_rejects_one_more_frame(
    tmp_path, monkeypatch
):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    monkeypatch.setattr(data_collection, "MAX_AUDIO_BYTES", 8)

    exact = runtime.begin("utterance-exact")
    assert exact is not None
    exact.add_audio(b"\x00\x00" * 4)
    exact.commit("teacher final")

    overflow = runtime.begin("utterance-overflow")
    assert overflow is not None
    overflow.add_audio(b"\x00\x00" * 4)
    overflow.add_audio(b"\x00\x00")
    with pytest.raises(DataCollectionError, match="audio"):
        overflow.commit("teacher final")

    assert runtime.wait_until_idle()
    assert (
        selected / "openvoiceinput-dataset-v1" / "utterances" / "utterance-exact"
    ).is_dir()
    assert not (
        selected / "openvoiceinput-dataset-v1" / "utterances" / "utterance-overflow"
    ).exists()
    assert runtime.close()


def test_provider_final_limit_is_measured_in_utf8_bytes(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    monkeypatch.setattr(data_collection, "MAX_PROVIDER_FINAL_BYTES", 4)

    exact = runtime.begin("utterance-exact")
    assert exact is not None
    exact.add_audio(b"\x00\x00")
    exact.commit("éé")

    overflow = runtime.begin("utterance-overflow")
    assert overflow is not None
    overflow.add_audio(b"\x00\x00")
    with pytest.raises(DataCollectionError, match="too large"):
        overflow.commit("ééx")

    assert runtime.wait_until_idle()
    record = (
        selected
        / "openvoiceinput-dataset-v1"
        / "utterances"
        / "utterance-exact"
        / "record.json"
    )
    assert (
        json.loads(record.read_text(encoding="utf-8"))["labels"]["provider_final"][
            "text"
        ]
        == "éé"
    )
    assert runtime.close()


def test_background_storage_error_sets_fixed_status_and_leaves_no_record(
    tmp_path, monkeypatch
):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-1")
    assert recorder is not None
    recorder.add_audio(b"\x00\x00" * 100)

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated ENOSPC")

    monkeypatch.setattr(data_collection, "_write_wav", fail_write)
    recorder.commit("teacher final")

    assert runtime.wait_until_idle()
    assert runtime.status_code() == "data-collection-failed"
    assert not (
        selected / "openvoiceinput-dataset-v1" / "utterances" / "utterance-1"
    ).exists()
    assert runtime.close()


def test_post_rename_sync_failure_reports_uncertain_durability(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-1")
    assert recorder is not None
    recorder.add_audio(b"\x00\x00" * 100)
    real_fsync = data_collection._fsync_directory

    def fail_final_directory(path):
        if path.name == "utterances":
            raise OSError("simulated EROFS")
        return real_fsync(path)

    monkeypatch.setattr(data_collection, "_fsync_directory", fail_final_directory)
    recorder.commit("teacher final")

    assert runtime.wait_until_idle()
    assert runtime.status_code() == "data-collection-failed"
    assert (
        selected / "openvoiceinput-dataset-v1" / "utterances" / "utterance-1"
    ).is_dir()
    assert runtime.close()


def test_unavailable_selected_directory_fails_only_in_background(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-1")
    assert recorder is not None
    recorder.add_audio(b"\x00\x00" * 100)
    shutil.rmtree(selected)

    recorder.commit("teacher final")

    assert runtime.wait_until_idle()
    assert not selected.exists()
    assert runtime.status_code() == "data-collection-failed"
    assert runtime.close()


def test_disabling_collection_during_recording_discards_pending_record(tmp_path):
    config_path = tmp_path / "private" / "data-collection.json"
    selected = tmp_path / "selected"
    selected.mkdir()
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-1")
    assert recorder is not None
    recorder.add_audio(b"\x00\x00" * 100)

    save_data_collection_config(False, selected, config_path)

    recorder.commit("teacher final")
    assert runtime.wait_until_idle()
    assert not list((selected / "openvoiceinput-dataset-v1" / "utterances").iterdir())
    assert runtime.status_code() == "none"
    assert runtime.close()
