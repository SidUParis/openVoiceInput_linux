from __future__ import annotations

import hashlib
import json
import shutil
import stat
import threading
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
from murmur_voice.microphone_metadata import (
    MicrophoneCaptureMetadata,
    MicrophoneRouteObservation,
    MicrophoneSelectionMetadata,
    privacy_preserving_microphone_identity,
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
    usage_root = selected / "openvoiceinput-dataset-v1" / "usage"
    assert usage_root.is_dir()
    assert stat.S_IMODE(usage_root.stat().st_mode) == 0o700
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
    usage_path = selected / "openvoiceinput-dataset-v1" / "usage" / "utterance-1.json"
    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    assert usage == {
        "schema_version": 1,
        "kind": "openvoiceinput-private-usage-summary",
        "utterance_id": "utterance-1",
        "recorded_at_utc": "2026-08-30T12:00:00Z",
        "audio_duration_ms": 75,
        "non_whitespace_character_count": 12,
    }
    assert "teacher final" not in usage_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(usage_path.stat().st_mode) == 0o600
    assert runtime.close()


def test_pcm_quality_is_posthoc_numeric_writer_metadata_without_filtering(
    tmp_path,
    monkeypatch,
):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-quality")
    assert recorder is not None
    first_second = b"\xff\x7f" * SAMPLE_RATE
    second_second = b"\x00\x00" * SAMPLE_RATE
    pcm = first_second + second_second
    analysis_threads = []
    real_summarizer = data_collection._summarize_pcm_quality

    def observe_writer_thread(chunks):
        analysis_threads.append(threading.current_thread().name)
        return real_summarizer(chunks)

    monkeypatch.setattr(
        data_collection,
        "_summarize_pcm_quality",
        observe_writer_thread,
    )
    recorder.add_audio(first_second)
    recorder.add_audio(second_second)
    assert analysis_threads == []
    recorder.commit("teacher final")
    assert runtime.wait_until_idle()

    final = selected / "openvoiceinput-dataset-v1" / "utterances" / "utterance-quality"
    quality = json.loads((final / "record.json").read_text(encoding="utf-8"))["audio"][
        "quality"
    ]
    assert analysis_threads == ["openvoice-data-writer"]
    assert quality == {
        "analysis_version": 1,
        "clipping_threshold_abs": 32760,
        "overall": {
            "sample_count": SAMPLE_RATE * 2,
            "clipped_fraction": 0.5,
            "rms_dbfs": -3.011,
            "peak_dbfs": 0.0,
            "dc_offset_fraction": 0.49998474,
            "zero_fraction": 0.5,
        },
        "first_second": {
            "sample_count": SAMPLE_RATE,
            "clipped_fraction": 1.0,
            "rms_dbfs": 0.0,
            "peak_dbfs": 0.0,
            "dc_offset_fraction": 0.99996948,
            "zero_fraction": 0.0,
        },
    }
    with wave.open(str(final / "audio.wav"), "rb") as audio:
        assert audio.readframes(audio.getnframes()) == pcm
    assert runtime.close()


def test_quality_analysis_failure_never_discards_valid_audio(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-quality-fallback")
    assert recorder is not None
    pcm = b"\x01\x02" * 100
    recorder.add_audio(pcm)

    def fail_quality(_chunks):
        raise RuntimeError("simulated optional analysis failure")

    monkeypatch.setattr(data_collection, "_summarize_pcm_quality", fail_quality)
    recorder.commit("teacher final")
    assert runtime.wait_until_idle()

    final = (
        selected
        / "openvoiceinput-dataset-v1"
        / "utterances"
        / "utterance-quality-fallback"
    )
    document = json.loads((final / "record.json").read_text(encoding="utf-8"))
    assert "quality" not in document["audio"]
    with wave.open(str(final / "audio.wav"), "rb") as audio:
        assert audio.readframes(audio.getnframes()) == pcm
    assert runtime.status_code() == "none"
    assert runtime.close()


def test_record_binds_the_actual_provider_without_changing_label_semantics(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-qwen")
    assert recorder is not None
    recorder.set_provider_identity("qwen", "qwen-audio-3.0-asr-flash-streaming")
    recorder.add_audio(b"\x00\x00" * 100)
    recorder.commit("teacher final")
    assert runtime.wait_until_idle()

    record = (
        selected
        / "openvoiceinput-dataset-v1"
        / "utterances"
        / "utterance-qwen"
        / "record.json"
    )
    document = json.loads(record.read_text(encoding="utf-8"))
    assert document["provider"] == {
        "name": "qwen",
        "model": "qwen-audio-3.0-asr-flash-streaming",
    }
    assert document["labels"]["provider_final"]["review_status"] == (
        "teacher-unreviewed"
    )
    assert runtime.close()


def test_schema_v2_records_selected_and_actual_microphone_without_private_name(
    tmp_path,
):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-microphone")
    assert recorder is not None
    dji = privacy_preserving_microphone_identity(
        "dji",
        bus="usb",
        vendor_id="2ca3",
        product_id="4011",
    )
    built_in = privacy_preserving_microphone_identity(
        "built-in",
        bus="pci",
        form_factor="internal",
    )
    recorder.add_audio(b"\x00\x00" * 100)
    # Actual source-output observations are asynchronous and may arrive after
    # audio has already started; attaching them never filters or delays audio.
    recorder.set_microphone_metadata(
        MicrophoneCaptureMetadata(
            MicrophoneSelectionMetadata(
                dji,
                "pulse",
                "policy-preferred",
                "online",
            ),
            "pulse-source-output",
            (
                MicrophoneRouteObservation(dji, 8),
                MicrophoneRouteObservation(built_in, 740),
            ),
        )
    )
    recorder.commit("teacher final")
    assert runtime.wait_until_idle()

    dataset_root = selected / "openvoiceinput-dataset-v1"
    marker = json.loads((dataset_root / "dataset.json").read_text(encoding="utf-8"))
    record_path = dataset_root / "utterances" / "utterance-microphone" / "record.json"
    document = json.loads(record_path.read_text(encoding="utf-8"))
    assert marker["schema_version"] == 1
    assert document["schema_version"] == 2
    assert document["microphone"]["selection"] == {
        "backend": "pulse",
        "category": "dji",
        "fingerprint": dji.fingerprint,
        "fingerprint_scope": "device-model",
        "provenance": "policy-preferred",
        "dji_link_state_at_selection": "online",
    }
    assert [
        route["category"] for route in document["microphone"]["actual"]["routes"]
    ] == ["dji", "built-in"]
    assert document["microphone"]["actual"]["status"] == "observed"
    assert document["microphone"]["actual"]["route_changed"] is True
    serialized = json.dumps(document["microphone"])
    assert "alsa_input" not in serialized
    assert "serial" not in serialized
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


def _feedback_document():
    return {
        "reason_code": "candidates-saved",
        "captured_count": 1,
        "activated_count": 0,
        "candidate_count": 1,
        "conflicted_count": 0,
        "replacement_hunks": 2,
        "corrections": [
            {
                "wrong": "Ostro",
                "canonical": "Austral",
                "category": "recognition",
                "evidence": "medium",
                "state": "candidate",
            }
        ],
    }


def test_enabled_collection_writes_feedback_sidecar_without_mutating_record(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-1")
    assert recorder is not None
    recorder.add_audio(b"\x00\x00" * 100)
    recorder.commit("teacher final")
    assert runtime.wait_until_idle()

    final = selected / "openvoiceinput-dataset-v1" / "utterances" / "utterance-1"
    record_before = (final / "record.json").read_bytes()
    assert runtime.record_feedback("utterance-1", _feedback_document())
    assert runtime.wait_until_idle()
    event_root = selected / "openvoiceinput-dataset-v1" / "feedback" / "utterance-1"
    events = list(event_root.glob("*.json"))
    assert len(events) == 1
    sidecar = json.loads(events[0].read_text(encoding="utf-8"))

    assert (final / "record.json").read_bytes() == record_before
    assert sorted(path.name for path in final.iterdir()) == ["audio.wav", "record.json"]
    assert sidecar["kind"] == "openvoiceinput-correction-feedback"
    assert sidecar["dataset_id"] == load_data_collection_config(config_path).dataset_id
    assert sidecar["utterance_id"] == "utterance-1"
    assert sidecar["result"] == _feedback_document()
    assert "surrounding" not in json.dumps(sidecar)
    assert stat.S_IMODE(event_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(events[0].stat().st_mode) == 0o600
    assert runtime.close()


def test_feedback_accepts_owner_private_fuse_style_regular_files(tmp_path):
    """SSHFS may surface remote 0600 regular files as local 0700 inodes."""

    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-1")
    assert recorder is not None
    recorder.add_audio(b"\x00\x00" * 100)
    recorder.commit("teacher final")
    assert runtime.wait_until_idle()

    utterance = selected / "openvoiceinput-dataset-v1" / "utterances" / "utterance-1"
    (utterance / "audio.wav").chmod(0o700)
    (utterance / "record.json").chmod(0o700)

    assert runtime.record_feedback("utterance-1", _feedback_document())
    assert runtime.wait_until_idle()
    assert runtime.status_code() == "none"
    assert (
        len(
            list(
                (
                    selected / "openvoiceinput-dataset-v1" / "feedback" / "utterance-1"
                ).glob("*.json")
            )
        )
        == 1
    )
    assert runtime.close()


@pytest.mark.parametrize("unsafe_mode", (0o400, 0o640, 0o644, 0o755))
def test_private_regular_validation_rejects_unsafe_or_unwritable_modes(
    tmp_path, unsafe_mode
):
    target = tmp_path / "record.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(unsafe_mode)

    with pytest.raises(DataCollectionError, match="incomplete"):
        data_collection._validate_private_regular_file(target)


def test_private_regular_validation_rejects_symlink(tmp_path):
    target = tmp_path / "record.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "linked.json"
    link.symlink_to(target)

    with pytest.raises(DataCollectionError, match="incomplete"):
        data_collection._validate_private_regular_file(link)


def test_feedback_queued_immediately_after_record_preserves_fifo_publication(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    recorder = runtime.begin("utterance-1")
    assert recorder is not None
    recorder.add_audio(b"\x00\x00" * 100)

    recorder.commit("teacher final")
    assert runtime.record_feedback("utterance-1", _feedback_document())
    assert runtime.wait_until_idle()

    final = selected / "openvoiceinput-dataset-v1" / "utterances" / "utterance-1"
    assert (final / "record.json").is_file()
    assert sorted(path.name for path in final.iterdir()) == ["audio.wav", "record.json"]
    event_root = selected / "openvoiceinput-dataset-v1" / "feedback" / "utterance-1"
    assert len(list(event_root.glob("*.json"))) == 1
    assert runtime.status_code() == "none"
    assert runtime.close()


def test_disabled_collection_writes_no_feedback_sidecar(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(False, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")

    assert runtime.record_feedback("utterance-1", _feedback_document()) is False
    assert not (selected / "openvoiceinput-dataset-v1").exists()
    assert runtime.close()


def test_feedback_rejects_surrounding_or_unknown_fields(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    config_path = tmp_path / "private" / "data-collection.json"
    save_data_collection_config(True, selected, config_path)
    runtime = DataCollectionRuntime(config_path, session_id="session-1")
    document = _feedback_document()
    document["surrounding_text"] = "must not persist"

    with pytest.raises(DataCollectionError, match="feedback is invalid"):
        runtime.record_feedback("utterance-1", document)

    assert runtime.close()
