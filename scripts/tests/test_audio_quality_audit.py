from __future__ import annotations

import array
import errno
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
import unittest
from unittest import mock
import wave


SCRIPT = Path(__file__).parents[1] / "audit_audio_quality.py"
SPEC = importlib.util.spec_from_file_location("audit_audio_quality", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def _private_file(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def _record(
    root: Path, utterance_id: str, samples: array.array[int]
) -> tuple[Path, Path]:
    directory = _private_dir(root / "utterances" / utterance_id)
    audio_path = directory / "audio.wav"
    with wave.open(str(audio_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(samples.tobytes())
    audio_path.chmod(0o600)
    audio_bytes = audio_path.read_bytes()
    document = {
        "schema_version": 4,
        "dataset_id": "a" * 32,
        "utterance_id": utterance_id,
        "collection_session_id": "b" * 32,
        "recorded_at_utc": "2026-09-04T08:00:00Z",
        "consent": "explicit-opt-in",
        "audio": {
            "file": "audio.wav",
            "format": "wav-pcm-s16le",
            "sample_rate_hz": 16_000,
            "channels": 1,
            "frames": len(samples),
            "file_sha256": hashlib.sha256(audio_bytes).hexdigest(),
            "pcm_sha256": hashlib.sha256(samples.tobytes()).hexdigest(),
        },
        "microphone": {
            "selection": {"category": "built-in"},
            "actual": {
                "route_changed": False,
                "routes": [{"category": "built-in", "first_observed_ms": 0}],
            },
        },
        "provider": {"name": "test", "model": "test"},
        "labels": {
            "provider_final": {
                "text": "private transcript must not enter the sidecar",
                "review_status": "teacher-unreviewed",
            },
            "spoken_verbatim": {"text": None, "review_status": "unreviewed"},
            "preferred_output": {"text": None, "review_status": "unreviewed"},
        },
        "delivery": {
            "target": "caret",
            "mode": "faithful",
            "outcome": "unchanged",
            "review_status": "machine-derived-unreviewed",
            "edits": [],
            "processor": {"name": "openvoice-clean-expression", "version": 1},
        },
    }
    record_path = directory / "record.json"
    _private_file(record_path, json.dumps(document).encode())
    return audio_path, record_path


def _dataset(tmp_path: Path) -> Path:
    root = _private_dir(tmp_path / "dataset")
    _private_dir(root / "utterances")
    _private_file(
        root / "dataset.json",
        json.dumps(
            {
                "schema_version": 1,
                "kind": "openvoiceinput-personal-asr-dataset",
                "dataset_id": "a" * 32,
            }
        ).encode(),
    )
    return root


def _rewrite_record(
    path: Path,
    update: Callable[[dict[str, Any]], None],
) -> None:
    document = json.loads(path.read_text())
    update(document)
    _private_file(path, json.dumps(document).encode())


class AudioQualityAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    @staticmethod
    def _clean_samples() -> array.array[int]:
        return array.array(
            "h",
            (
                round(10_000 * math.sin(2 * math.pi * 440 * index / 16_000))
                for index in range(16_000)
            ),
        )

    def test_emits_signal_only_sidecars_without_changing_sources(self) -> None:
        root = _dataset(self.root)
        audio_path, record_path = _record(root, "1" * 32, self._clean_samples())
        audio_before = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        record_before = hashlib.sha256(record_path.read_bytes()).hexdigest()
        sidecars = self.root / "quality-v1"

        first = audit.audit_dataset(root, sidecars, audit.ZoneInfo("Europe/Paris"))
        second = audit.audit_dataset(root, sidecars, audit.ZoneInfo("Europe/Paris"))

        self.assertEqual(first["snapshot"]["tiers"]["high"]["count"], 1)
        self.assertEqual(first["sidecars"]["records"], {"created": 1})
        self.assertEqual(first["sidecars"]["manifest"]["status"], "created")
        self.assertEqual(second["sidecars"]["records"], {"unchanged": 1})
        self.assertEqual(second["sidecars"]["manifest"]["status"], "unchanged")
        self.assertEqual(
            hashlib.sha256(audio_path.read_bytes()).hexdigest(), audio_before
        )
        self.assertEqual(
            hashlib.sha256(record_path.read_bytes()).hexdigest(), record_before
        )
        sidecar_path = sidecars / ("1" * 32) / "quality.json"
        self.assertEqual(sidecar_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sidecar_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (sidecar_path.parent / "complete").read_bytes(),
            audit._completion_marker(sidecar_path.read_bytes()),
        )
        sidecar = json.loads(sidecar_path.read_text())
        self.assertEqual(sidecar["assessment"]["scope"], "signal-only")
        self.assertEqual(
            sidecar["assessment"]["classification_basis"],
            "heuristic-threshold-policy-v1",
        )
        self.assertEqual(
            sidecar["assessment"]["transcript_review_status"], "not-evaluated"
        )
        self.assertEqual(
            sidecar["assessment"]["training_label_status"],
            "non-gold-signal-audit-only",
        )
        self.assertNotIn("private transcript", sidecar_path.read_text())
        manifests = list((sidecars / "manifests").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text())
        self.assertEqual(
            (manifests[0].parent / "complete").read_bytes(),
            audit._completion_marker(manifests[0].read_bytes()),
        )
        self.assertEqual(manifest["snapshot_record_count"], 1)
        self.assertEqual(manifest["records"][0]["utterance_id"], "1" * 32)
        self.assertEqual(manifest["timezone"], "Europe/Paris")
        self.assertEqual(manifest["as_of_recorded_at_utc"], "2026-09-04T08:00:00Z")
        self.assertEqual(manifest["transcript_review_status"], "not-evaluated")
        self.assertEqual(
            manifest["training_label_status"], "non-gold-signal-audit-only"
        )
        self.assertEqual(
            manifest["classification_basis"], "heuristic-threshold-policy-v1"
        )

    def test_accepts_current_schema_v5_record_and_publishes_manifest(self) -> None:
        root = _dataset(self.root)
        utterance_id = "20" * 16
        _, record_path = _record(root, utterance_id, self._clean_samples())

        def upgrade_to_current_v5(document: dict[str, Any]) -> None:
            document["schema_version"] = 5
            document["delivery"] = {
                "target": "caret",
                "mode": "faithful",
                "text": "private transcript must not enter the sidecar",
                "review_status": "machine-derived-unreviewed",
                "pipeline": [
                    {
                        "input_basis": "provider-final",
                        "processor": {
                            "name": "openvoice-confirmed-correction",
                            "version": 1,
                        },
                        "outcome": "unchanged",
                        "edits": [],
                    },
                    {
                        "input_basis": "previous-stage",
                        "processor": {"name": "identity", "version": 1},
                        "outcome": "faithful",
                        "edits": [],
                    },
                ],
            }

        _rewrite_record(record_path, upgrade_to_current_v5)
        sidecars = self.root / "quality-v1"

        result = audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))

        self.assertEqual(result["snapshot"]["schema_versions"], {"5": 1})
        self.assertEqual(result["snapshot"]["structural_failures"], {})
        self.assertEqual(result["sidecars"]["records"], {"created": 1})
        self.assertEqual(result["sidecars"]["manifest"]["status"], "created")
        sidecar = (sidecars / utterance_id / "quality.json").read_text()
        self.assertNotIn("private transcript", sidecar)
        manifests = list((sidecars / "manifests").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text())
        self.assertEqual(manifest["snapshot_record_count"], 1)
        self.assertEqual(manifest["records"][0]["utterance_id"], utterance_id)
        self.assertEqual(manifest["timezone"], "UTC")

    def test_rejects_severely_clipped_and_dc_offset_audio(self) -> None:
        root = _dataset(self.root)
        _record(root, "2" * 32, array.array("h", [32_767] * 16_000))

        result = audit.audit_dataset(root, None, audit.ZoneInfo("UTC"))

        self.assertEqual(result["snapshot"]["tiers"]["reject"]["count"], 1)
        reasons = result["snapshot"]["reason_counts"]
        self.assertEqual(reasons["severe-clipping"], 1)
        self.assertEqual(reasons["severe-dc-offset"], 1)

    def test_refuses_symlinked_source_files(self) -> None:
        root = _dataset(self.root)
        samples = array.array("h", [1_000] * 16_000)
        audio_path, _ = _record(root, "3" * 32, samples)
        target = self.root / "outside.wav"
        audio_path.rename(target)
        audio_path.symlink_to(target)

        result = audit.audit_dataset(root, None, audit.ZoneInfo("UTC"))

        self.assertEqual(result["snapshot"]["tiers"]["reject"]["count"], 1)
        self.assertEqual(result["snapshot"]["reason_counts"], {"structural-invalid": 1})

    def test_refuses_to_overwrite_a_conflicting_sidecar(self) -> None:
        root = _dataset(self.root)
        _record(root, "4" * 32, array.array("h", [1_000] * 16_000))
        sidecars = _private_dir(self.root / "quality-v1")
        conflict_root = _private_dir(sidecars / ("4" * 32))
        conflict = conflict_root / "quality.json"
        _private_file(conflict, b"{}\n")
        _private_file(
            conflict_root / "complete",
            audit._completion_marker(b"{}\n"),
        )

        with self.assertRaisesRegex(audit.AuditError, "conflicts"):
            audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))

    def test_new_record_creates_new_manifest_without_changing_old_files(self) -> None:
        root = _dataset(self.root)
        samples = self._clean_samples()
        _record(root, "5" * 32, samples)
        sidecars = self.root / "quality-v1"
        first = audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))
        first_sidecar = sidecars / ("5" * 32) / "quality.json"
        first_manifest = (
            sidecars
            / "manifests"
            / first["sidecars"]["manifest"]["snapshot_sha256"]
            / "manifest.json"
        )
        sidecar_hash = hashlib.sha256(first_sidecar.read_bytes()).hexdigest()
        manifest_hash = hashlib.sha256(first_manifest.read_bytes()).hexdigest()

        _record(root, "6" * 32, samples)
        second = audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))

        self.assertEqual(second["sidecars"]["records"], {"created": 1, "unchanged": 1})
        self.assertEqual(second["sidecars"]["manifest"]["status"], "created")
        self.assertNotEqual(
            first["sidecars"]["manifest"]["snapshot_sha256"],
            second["sidecars"]["manifest"]["snapshot_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(first_sidecar.read_bytes()).hexdigest(), sidecar_hash
        )
        self.assertEqual(
            hashlib.sha256(first_manifest.read_bytes()).hexdigest(), manifest_hash
        )
        self.assertEqual(len(list((sidecars / "manifests").glob("*/manifest.json"))), 2)

    def test_record_metadata_change_cannot_reuse_an_old_sidecar_or_manifest(
        self,
    ) -> None:
        root = _dataset(self.root)
        _, record_path = _record(root, "0" * 32, self._clean_samples())
        sidecars = self.root / "quality-v1"
        first = audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))
        first_digest = first["sidecars"]["manifest"]["snapshot_sha256"]
        _rewrite_record(
            record_path,
            lambda document: document.update(
                {"recorded_at_utc": "2026-09-04T09:00:00Z"}
            ),
        )

        with self.assertRaisesRegex(audit.PublishError, "conflicts"):
            audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))

        manifests = list((sidecars / "manifests").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(manifests[0].parent.name, first_digest)

    def test_route_change_is_provenance_only_and_does_not_change_tier(self) -> None:
        root = _dataset(self.root)
        _, stable_record = _record(root, "7" * 32, self._clean_samples())
        _, changed_record = _record(root, "8" * 32, self._clean_samples())
        _rewrite_record(
            changed_record,
            lambda document: document["microphone"]["actual"].update(
                {
                    "route_changed": True,
                    "routes": [
                        {"category": "built-in", "first_observed_ms": 0},
                        {"category": "dji", "first_observed_ms": 250},
                    ],
                }
            ),
        )

        result = audit.audit_dataset(root, None, audit.ZoneInfo("UTC"))

        self.assertEqual(result["snapshot"]["tiers"]["high"]["count"], 2)
        self.assertEqual(result["snapshot"]["reason_counts"], {})
        self.assertEqual(
            result["snapshot"]["provenance_warning_counts"],
            {"source-changed-during-capture": 1},
        )
        self.assertEqual(stable_record.parent.name, "7" * 32)

    def test_late_timestamp_failure_counts_only_one_reject(self) -> None:
        root = _dataset(self.root)
        _, record_path = _record(root, "9" * 32, self._clean_samples())
        _rewrite_record(
            record_path,
            lambda document: document.update(
                {"recorded_at_utc": "2026-09-04T08:00:00"}
            ),
        )

        result = audit.audit_dataset(root, None, audit.ZoneInfo("UTC"))

        tiers = result["snapshot"]["tiers"]
        self.assertEqual(sum(value["count"] for value in tiers.values()), 1)
        self.assertEqual(tiers["reject"]["count"], 1)
        self.assertEqual(result["snapshot"]["duration_seconds"], 0.0)
        self.assertEqual(result["snapshot"]["schema_versions"], {})

    def test_microphone_values_are_allowlisted_before_summary_or_sidecar(self) -> None:
        root = _dataset(self.root)
        _, record_path = _record(root, "a" * 32, self._clean_samples())
        private_value = "private custom microphone label"

        def poison(document: dict[str, object]) -> None:
            microphone = document["microphone"]
            assert isinstance(microphone, dict)
            selection = microphone["selection"]
            actual = microphone["actual"]
            assert isinstance(selection, dict) and isinstance(actual, dict)
            selection["category"] = private_value
            actual["routes"] = [{"category": private_value, "first_observed_ms": 0}]

        _rewrite_record(record_path, poison)
        sidecars = self.root / "quality-v1"

        result = audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))
        rendered = json.dumps(result, sort_keys=True)
        sidecar = (sidecars / ("a" * 32) / "quality.json").read_text()

        self.assertNotIn(private_value, rendered)
        self.assertNotIn(private_value, sidecar)
        self.assertEqual(
            result["snapshot"]["provenance_warning_counts"],
            {"microphone-metadata-invalid": 1},
        )
        self.assertIn("unknown", result["snapshot"]["microphone_selection"])

    def test_non_string_microphone_category_rejects_one_record_without_crashing(
        self,
    ) -> None:
        root = _dataset(self.root)
        _, record_path = _record(root, "1a" * 16, self._clean_samples())

        def invalidate(document: dict[str, object]) -> None:
            microphone = document["microphone"]
            assert isinstance(microphone, dict)
            selection = microphone["selection"]
            assert isinstance(selection, dict)
            selection["category"] = []

        _rewrite_record(record_path, invalidate)

        result = audit.audit_dataset(root, None, audit.ZoneInfo("UTC"))

        self.assertEqual(result["snapshot"]["tiers"]["reject"]["count"], 1)
        self.assertEqual(
            sum(value["count"] for value in result["snapshot"]["tiers"].values()),
            1,
        )
        self.assertIn(
            "microphone metadata is invalid",
            result["snapshot"]["structural_failures"],
        )

    def test_accepts_owner_only_0700_source_files(self) -> None:
        root = _dataset(self.root)
        audio_path, record_path = _record(root, "b" * 32, self._clean_samples())
        audio_path.chmod(0o700)
        record_path.chmod(0o700)

        result = audit.audit_dataset(root, None, audit.ZoneInfo("UTC"))

        self.assertEqual(result["snapshot"]["tiers"]["high"]["count"], 1)

    def test_rejects_boolean_schema_naive_time_and_duplicate_keys(self) -> None:
        cases = ("boolean", "naive", "offset", "duplicate")
        for index, case in enumerate(cases, start=12):
            with self.subTest(case=case):
                case_root = _dataset(self.root / case)
                _, record_path = _record(
                    case_root,
                    f"{index:032x}",
                    self._clean_samples(),
                )
                if case == "boolean":
                    _rewrite_record(
                        record_path,
                        lambda document: document.update({"schema_version": True}),
                    )
                elif case in {"naive", "offset"}:
                    _rewrite_record(
                        record_path,
                        lambda document: document.update(
                            {
                                "recorded_at_utc": (
                                    "2026-09-04T08:00:00"
                                    if case == "naive"
                                    else "2026-09-04T08:00:00+00:00"
                                )
                            }
                        ),
                    )
                else:
                    payload = record_path.read_text()
                    _private_file(
                        record_path,
                        payload.replace(
                            '"schema_version": 4',
                            '"schema_version": 4, "schema_version": 4',
                            1,
                        ).encode(),
                    )
                result = audit.audit_dataset(case_root, None, audit.ZoneInfo("UTC"))
                self.assertEqual(result["snapshot"]["tiers"]["reject"]["count"], 1)
                self.assertEqual(
                    sum(
                        value["count"] for value in result["snapshot"]["tiers"].values()
                    ),
                    1,
                )

    def test_accepts_supported_integer_schema_versions(self) -> None:
        root = _dataset(self.root)
        for version in (1, 2, 3, 4):
            _, record_path = _record(
                root,
                f"{version:032x}",
                self._clean_samples(),
            )
            _rewrite_record(
                record_path,
                lambda document, version=version: document.update(
                    {"schema_version": version}
                ),
            )

        result = audit.audit_dataset(root, None, audit.ZoneInfo("UTC"))

        self.assertEqual(
            result["snapshot"]["schema_versions"],
            {"1": 1, "2": 1, "3": 1, "4": 1},
        )

    def test_does_not_scan_feedback_or_treat_it_as_transcript_review(self) -> None:
        root = _dataset(self.root)
        _record(root, "c" * 32, self._clean_samples())
        feedback = _private_dir(root / "feedback")
        trap = feedback / "must-not-be-opened.json"
        _private_file(trap, b"not json and transcript private")
        trap.chmod(0o000)

        sidecars = self.root / "quality-v1"
        result = audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))
        sidecar = json.loads((sidecars / ("c" * 32) / "quality.json").read_text())

        self.assertEqual(result["audit"]["transcript_review_status"], "not-evaluated")
        self.assertEqual(
            sidecar["assessment"]["transcript_review_status"], "not-evaluated"
        )

    def test_threshold_boundaries_are_explicit(self) -> None:
        def metrics() -> dict[str, object]:
            clean = {
                "clipped_fraction": 0.0,
                "dc_offset_fraction": 0.0,
                "rms_dbfs": -18.0,
                "peak_dbfs": -6.0,
            }
            return {
                "duration_seconds": 1.0,
                "overall": dict(clean),
                "first_second": dict(clean),
                "frame_analysis": {
                    "active_frame_fraction": 1.0,
                    "active_rms_dbfs": -18.0,
                    "longest_zero_run_ms": 0.0,
                },
            }

        cases = (
            ("overall", "clipped_fraction", 0.0, "high"),
            ("overall", "clipped_fraction", 0.00000001, "usable"),
            ("overall", "clipped_fraction", 0.00999999, "usable"),
            ("overall", "clipped_fraction", 0.01, "low"),
            ("overall", "clipped_fraction", 0.09999999, "low"),
            ("overall", "clipped_fraction", 0.10, "reject"),
            ("overall", "dc_offset_fraction", 0.00999999, "high"),
            ("overall", "dc_offset_fraction", 0.01, "usable"),
            ("overall", "dc_offset_fraction", 0.05, "low"),
            ("overall", "dc_offset_fraction", 0.20, "reject"),
            ("first_second", "clipped_fraction", 0.01, "usable"),
            ("first_second", "clipped_fraction", 0.10, "low"),
            ("first_second", "dc_offset_fraction", 0.05, "usable"),
            ("first_second", "dc_offset_fraction", 0.20, "low"),
        )
        for section, field, value, expected in cases:
            with self.subTest(section=section, field=field, value=value):
                document = metrics()
                values = document[section]
                assert isinstance(values, dict)
                values[field] = value
                self.assertEqual(audit._classify(document)[0], expected)

        below = metrics()
        below["duration_seconds"] = 0.299999
        at = metrics()
        at["duration_seconds"] = 0.3
        self.assertEqual(audit._classify(below)[0], "reject")
        self.assertEqual(audit._classify(at)[0], "high")

    def test_rejects_wav_declaring_more_than_600_seconds(self) -> None:
        root = _dataset(self.root)
        audio_path, record_path = _record(root, "d" * 32, self._clean_samples())
        declared_frames = audit.MAX_AUDIO_FRAMES + 1
        header = (
            b"RIFF"
            + struct.pack("<I", 36 + declared_frames * 2)
            + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 16_000, 32_000, 2, 16)
            + b"data"
            + struct.pack("<I", declared_frames * 2)
        )
        _private_file(audio_path, header)
        _rewrite_record(
            record_path,
            lambda document: document["audio"].update(
                {
                    "frames": declared_frames,
                    "file_sha256": hashlib.sha256(header).hexdigest(),
                    "pcm_sha256": hashlib.sha256(b"").hexdigest(),
                }
            ),
        )

        result = audit.audit_dataset(root, None, audit.ZoneInfo("UTC"))

        self.assertEqual(result["snapshot"]["tiers"]["reject"]["count"], 1)
        self.assertIn(
            "WAV duration is outside the audit boundary",
            result["snapshot"]["structural_failures"],
        )

    def test_rejects_ancestor_symlink_and_detects_directory_replacement(self) -> None:
        real_parent = _private_dir(self.root / "real")
        root = _dataset(real_parent)
        _record(root, "e" * 32, self._clean_samples())
        alias = self.root / "alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(audit.AuditError, "unsafe"):
            audit.audit_dataset(alias / "dataset", None, audit.ZoneInfo("UTC"))

        original = audit._assert_child_directory_stable
        replaced = False

        def replace_after_first_stable_check(
            parent_fd: int,
            name: str,
            child_fd: int,
        ) -> None:
            nonlocal replaced
            original(parent_fd, name, child_fd)
            if not replaced and name == "e" * 32:
                replaced = True
                utterance = root / "utterances" / name
                moved = root / "utterances" / "moved"
                utterance.rename(moved)
                utterance.symlink_to(moved, target_is_directory=True)

        with mock.patch.object(
            audit,
            "_assert_child_directory_stable",
            replace_after_first_stable_check,
        ):
            sidecars = self.root / "quality-v1"
            with self.assertRaisesRegex(audit.AuditError, "changed after validation"):
                audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))
        self.assertFalse(sidecars.exists())

    def test_same_size_restored_mtime_mutation_aborts_before_any_publication(
        self,
    ) -> None:
        root = _dataset(self.root)
        audio_path, _ = _record(root, "1c" * 16, self._clean_samples())
        original = audit._assert_child_directory_stable
        changed = False

        def mutate_after_first_stable_check(
            parent_fd: int,
            name: str,
            child_fd: int,
        ) -> None:
            nonlocal changed
            original(parent_fd, name, child_fd)
            if not changed and name == "1c" * 16:
                changed = True
                before = audio_path.stat()
                payload = bytearray(audio_path.read_bytes())
                payload[-1] ^= 1
                audio_path.write_bytes(payload)
                audio_path.chmod(0o600)
                os.utime(
                    audio_path,
                    ns=(before.st_atime_ns, before.st_mtime_ns),
                )
                after = audio_path.stat()
                self.assertEqual(after.st_size, before.st_size)
                self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

        sidecars = self.root / "quality-v1"
        with mock.patch.object(
            audit,
            "_assert_child_directory_stable",
            mutate_after_first_stable_check,
        ):
            with self.assertRaisesRegex(audit.AuditError, "content changed"):
                audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))

        self.assertFalse(sidecars.exists())

    def test_corrupt_wav_at_rehash_is_a_clean_audit_error_with_zero_publication(
        self,
    ) -> None:
        root = _dataset(self.root)
        audio_path, _ = _record(root, "1f" * 16, self._clean_samples())
        original = audit._assert_child_directory_stable
        changed = False

        def corrupt_after_first_stable_check(
            parent_fd: int,
            name: str,
            child_fd: int,
        ) -> None:
            nonlocal changed
            original(parent_fd, name, child_fd)
            if not changed and name == "1f" * 16:
                changed = True
                before = audio_path.stat()
                payload = bytearray(audio_path.read_bytes())
                payload[:4] = b"NOPE"
                audio_path.write_bytes(payload)
                audio_path.chmod(0o600)
                os.utime(
                    audio_path,
                    ns=(before.st_atime_ns, before.st_mtime_ns),
                )

        sidecars = self.root / "quality-v1"
        with mock.patch.object(
            audit,
            "_assert_child_directory_stable",
            corrupt_after_first_stable_check,
        ):
            with self.assertRaisesRegex(audit.AuditError, "rehashed"):
                audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))

        self.assertFalse(sidecars.exists())

    def test_raced_empty_destination_is_never_replaced(self) -> None:
        root = _dataset(self.root)
        utterance_id = "1d" * 16
        _record(root, utterance_id, self._clean_samples())
        sidecars = self.root / "quality-v1"
        original = os.mkdir
        raced = False

        def occupy_empty_destination(
            path: str | bytes,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            nonlocal raced
            if not raced and path == utterance_id:
                raced = True
                original(path, mode, dir_fd=dir_fd)
            original(path, mode, dir_fd=dir_fd)

        with (
            mock.patch.object(os, "mkdir", occupy_empty_destination),
            mock.patch.object(audit, "CONCURRENT_PUBLICATION_WAIT_SECONDS", 0.01),
        ):
            with self.assertRaisesRegex(audit.PublishError, "incomplete"):
                audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))

        raced_target = sidecars / utterance_id
        self.assertTrue(raced_target.is_dir())
        self.assertEqual(list(raced_target.iterdir()), [])
        self.assertEqual(
            list((sidecars / "manifests").glob("*/manifest.json")),
            [],
        )

    def test_sshfs_flagged_rename_einval_shape_does_not_block_publication(
        self,
    ) -> None:
        root = _dataset(self.root)
        _record(root, "1e" * 16, self._clean_samples())
        sidecars = self.root / "quality-v1"
        original = os.rename

        def sshfs_shaped_rename(
            source: str | bytes,
            destination: str | bytes,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            if src_dir_fd != dst_dir_fd:
                raise OSError(errno.EINVAL, "flagged directory rename unsupported")
            original(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        with mock.patch.object(os, "rename", sshfs_shaped_rename):
            result = audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))

        self.assertEqual(result["sidecars"]["records"], {"created": 1})
        self.assertTrue((sidecars / ("1e" * 16) / "complete").is_file())

    def test_interrupted_second_sidecar_leaves_cache_without_snapshot_commit(
        self,
    ) -> None:
        root = _dataset(self.root)
        first_id = "2a" * 16
        second_id = "2b" * 16
        _record(root, first_id, self._clean_samples())
        _record(root, second_id, self._clean_samples())
        sidecars = self.root / "quality-v1"
        original = audit._publish_directory_document

        def interrupt_second(
            destination_fd: int,
            directory_name: str,
            filename: str,
            document: dict[str, object],
            *,
            maximum_existing_bytes: int = audit.MAX_RECORD_BYTES,
        ) -> str:
            if directory_name == second_id:
                raise audit.PublishError("simulated interruption")
            return original(
                destination_fd,
                directory_name,
                filename,
                document,
                maximum_existing_bytes=maximum_existing_bytes,
            )

        with mock.patch.object(
            audit,
            "_publish_directory_document",
            interrupt_second,
        ):
            with self.assertRaisesRegex(audit.PublishError, "interruption"):
                audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))

        self.assertTrue((sidecars / first_id / "complete").is_file())
        self.assertFalse((sidecars / second_id).exists())
        self.assertEqual(
            list((sidecars / "manifests").glob("*/complete")),
            [],
        )

        result = audit.audit_dataset(root, sidecars, audit.ZoneInfo("UTC"))

        self.assertEqual(
            result["sidecars"]["records"],
            {"created": 1, "unchanged": 1},
        )
        snapshot_digest = result["sidecars"]["manifest"]["snapshot_sha256"]
        self.assertTrue(
            (sidecars / "manifests" / snapshot_digest / "complete").is_file()
        )

    def test_refuses_sidecar_directories_overlapping_dataset_content(self) -> None:
        root = _dataset(self.root)
        _, record_path = _record(root, "1b" * 16, self._clean_samples())
        feedback = _private_dir(root / "feedback")
        usage = _private_dir(root / "usage")
        candidates = (
            root / "utterances" / "quality-v1",
            record_path.parent / "quality-v1",
            feedback / "quality-v1",
            usage / "quality-v1",
        )

        for sidecar_dir in candidates:
            with self.subTest(sidecar_dir=sidecar_dir.parent.name):
                with self.assertRaisesRegex(audit.AuditError, "overlaps"):
                    audit.audit_dataset(root, sidecar_dir, audit.ZoneInfo("UTC"))
                self.assertFalse(sidecar_dir.exists())

    def test_concurrent_identical_publication_is_idempotent(self) -> None:
        root = _dataset(self.root)
        _record(root, "f" * 32, self._clean_samples())
        sidecars = self.root / "quality-v1"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: audit.audit_dataset(
                        root,
                        sidecars,
                        audit.ZoneInfo("UTC"),
                    ),
                    range(2),
                )
            )

        statuses = [result["sidecars"]["records"] for result in results]
        self.assertEqual(
            sorted(next(iter(status)) for status in statuses),
            ["created", "unchanged"],
        )
        self.assertTrue((sidecars / ("f" * 32) / "complete").is_file())


if __name__ == "__main__":
    unittest.main()
