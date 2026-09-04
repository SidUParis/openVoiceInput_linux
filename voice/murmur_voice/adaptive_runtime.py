"""Private persistence and per-utterance loading for adaptive corrections."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from .adaptive_correction import (
    CorrectionCandidate,
    canonicalize_with_approved_terms,
    collapsed_term_key,
    extract_corrections,
)
from .adaptive_store import (
    AdaptiveEntry,
    AdaptiveLedger,
    AdaptiveLastResult,
    AdaptiveStoreError,
    activate_correction,
    adaptive_statistics,
    compile_provider_correction_report,
    compile_provider_corrections,
    compile_terminal_corrections,
    normalized_key,
    parse_adaptive_ledger,
    record_evidence,
    serialize_adaptive_ledger,
    with_last_result,
)
from .config import (
    ConfigError,
    VoiceConfig,
    _load_private_bytes,
    _reject_duplicate_json_fields,
    _write_private_json,
    default_adaptive_corrections_path,
    load_config,
    load_corrections,
    load_vocabulary,
)

MAX_ADAPTIVE_CORRECTIONS_BYTES = 384 * 1024
MAX_EXPLICIT_FEEDBACK_TEXT_CHARACTERS = 4096
_SYSTEM_DICTIONARY_GLOBS = (
    "/usr/share/hunspell/en_US*.dic",
    "/usr/share/hunspell/en_GB*.dic",
    "/usr/share/hunspell/fr*.dic",
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, repr=False)
class AdaptiveObservedCandidate:
    """One private bounded pair plus its decision, hidden from debug reprs."""

    wrong: str
    canonical: str
    category: str
    evidence: str
    state: str


@dataclass(frozen=True, slots=True, repr=False)
class AdaptiveObservationResult:
    """One explicit learning outcome suitable for status and feedback."""

    reason_code: str
    captured_count: int = 0
    activated_count: int = 0
    candidate_count: int = 0
    conflicted_count: int = 0
    replacement_hunks: int = 0
    candidates: tuple[AdaptiveObservedCandidate, ...] = field(default=(), repr=False)

    @property
    def learned(self) -> bool:
        return self.activated_count > 0

    def as_feedback_document(self) -> dict[str, Any]:
        """Return only bounded pairs and classifications, never surrounding text."""

        return {
            "reason_code": self.reason_code,
            "captured_count": self.captured_count,
            "activated_count": self.activated_count,
            "candidate_count": self.candidate_count,
            "conflicted_count": self.conflicted_count,
            "replacement_hunks": self.replacement_hunks,
            "corrections": [
                {
                    "wrong": item.wrong,
                    "canonical": item.canonical,
                    "category": item.category,
                    "evidence": item.evidence,
                    "state": item.state,
                }
                for item in self.candidates
            ],
        }


def load_adaptive_ledger(path: str | Path | None = None) -> AdaptiveLedger:
    """Load the optional private ledger without following links."""

    ledger_path = (
        Path(path) if path is not None else default_adaptive_corrections_path()
    )
    raw = _load_private_bytes(
        ledger_path,
        kind="adaptive correction ledger",
        limit=MAX_ADAPTIVE_CORRECTIONS_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return AdaptiveLedger()
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_fields,
        )
        return parse_adaptive_ledger(document)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ConfigError(
            "adaptive correction ledger is not valid UTF-8 JSON"
        ) from error


def save_adaptive_ledger(
    ledger: AdaptiveLedger,
    path: str | Path | None = None,
) -> Path:
    """Atomically store the private bounded ledger as mode 0600."""

    ledger_path = (
        Path(path) if path is not None else default_adaptive_corrections_path()
    )
    try:
        document = serialize_adaptive_ledger(ledger)
    except AdaptiveStoreError as error:
        raise ConfigError("adaptive correction ledger is invalid") from error
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    if len(payload) > MAX_ADAPTIVE_CORRECTIONS_BYTES:
        raise ConfigError("adaptive correction ledger is too large")
    return _write_private_json(
        ledger_path,
        document,
        kind="adaptive correction ledger",
        temporary_prefix=".adaptive-corrections.json.",
    )


class AdaptiveCorrectionRuntime:
    """Join hot reload, conservative edit extraction, and private persistence."""

    def __init__(
        self,
        *,
        config_path: Path,
        vocabulary_path: Path,
        corrections_path: Path,
        adaptive_path: Path,
    ) -> None:
        self._config_path = config_path
        self._vocabulary_path = vocabulary_path
        self._corrections_path = corrections_path
        self._adaptive_path = adaptive_path
        self._lock = threading.RLock()

    def validate(self) -> VoiceConfig:
        """Validate every recognition input before the control socket starts."""

        config, _vocabulary, _manual, _ledger = self._load_snapshot()
        return config

    def create_asr_client(self) -> Any:
        """Create one provider client from a fresh immutable context snapshot."""

        with self._lock:
            config, vocabulary, manual, ledger = self._load_snapshot()
            try:
                terminal_corrections = compile_terminal_corrections(manual, ledger)
                effective = replace(
                    config,
                    hotwords=vocabulary,
                    corrections=compile_provider_corrections(manual, ledger),
                )
            except AdaptiveStoreError as error:
                raise ConfigError("adaptive correction ledger is invalid") from error

        # Keep provider and optional transport imports lazy for configure and
        # status-only commands.
        from .providers import create_asr_client

        client = create_asr_client(effective)
        # This tuple comes from the same locked snapshot as the provider
        # context.  VoiceSession copies it before opening the network or mic,
        # so an edit saved during capture applies only to the next utterance.
        client.terminal_corrections = terminal_corrections
        return client

    def observe(self, snapshot: Any) -> bool:
        """Compatibility wrapper returning whether a provider rule activated."""

        return self.observe_result(snapshot).learned

    def observe_result(self, snapshot: Any) -> AdaptiveObservationResult:
        """Capture classified replacements and persist an explicit outcome."""

        with self._lock:
            cursor = getattr(snapshot, "cursor", None)
            anchor = getattr(snapshot, "anchor", None)
            if type(cursor) is not int or type(anchor) is not int:
                return self._record_result("invalid-snapshot")
            if cursor != anchor:
                return self._record_result("selection-active")
            vocabulary = load_vocabulary(self._vocabulary_path)
            extraction = extract_corrections(
                getattr(snapshot, "baseline_text", None),
                getattr(snapshot, "committed_start", None),
                getattr(snapshot, "committed_end", None),
                getattr(snapshot, "current_text", None),
                approved_term_resolver=lambda text: _canonicalize_approved_term(
                    text,
                    vocabulary,
                ),
            )
            if not extraction.candidates:
                return self._record_result(
                    extraction.reason_code,
                    replacement_hunks=extraction.replacement_hunks,
                )
            with _adaptive_file_lock(self._adaptive_path):
                ledger = load_adaptive_ledger(self._adaptive_path)
                updated = ledger
                try:
                    for candidate in extraction.candidates:
                        updated = record_evidence(
                            updated,
                            candidate.wrong,
                            candidate.canonical,
                            state=(
                                "active"
                                if candidate.evidence == "strong"
                                else "candidate"
                            ),
                            category=candidate.category,
                            evidence=candidate.evidence,
                        )
                    manual = load_corrections(self._corrections_path)
                    provider_report = compile_provider_correction_report(
                        manual, updated
                    )
                except AdaptiveStoreError as error:
                    raise ConfigError(
                        "adaptive correction ledger is invalid"
                    ) from error

                observed = tuple(
                    _observed_candidate(candidate, updated)
                    for candidate in extraction.candidates
                )
                provider_statuses = tuple(
                    provider_report.status_for(item.wrong, item.canonical)
                    if item.state == "active"
                    else None
                    for item in observed
                )
                # ``learned`` remains about adaptive activation.  An identical
                # explicit rule is already effective, but observing it again
                # must not claim that a new adaptive rule was learned.
                activated_count = sum(
                    status == "effective-adaptive" for status in provider_statuses
                )
                candidate_count = sum(item.state == "candidate" for item in observed)
                conflicted_count = sum(item.state == "conflicted" for item in observed)
                reason = _decision_reason(
                    activated_count,
                    candidate_count,
                    conflicted_count,
                    provider_statuses,
                )
                result = AdaptiveObservationResult(
                    reason_code=reason,
                    captured_count=len(observed),
                    activated_count=activated_count,
                    candidate_count=candidate_count,
                    conflicted_count=conflicted_count,
                    replacement_hunks=extraction.replacement_hunks,
                    candidates=observed,
                )
                updated = with_last_result(updated, _last_result(result))
                save_adaptive_ledger(updated, self._adaptive_path)
                return result

    def record_external_result(self, reason_code: str) -> AdaptiveObservationResult:
        """Persist a reason produced outside extraction, such as a timeout."""

        with self._lock:
            return self._record_result(reason_code)

    def confirm(self, wrong: str, canonical: str) -> AdaptiveObservationResult:
        """Explicitly activate one retained choice and archive alternatives."""

        with self._lock:
            return confirm_adaptive_correction(
                self._adaptive_path,
                self._corrections_path,
                wrong,
                canonical,
            )

    def submit_explicit_feedback(
        self,
        provider_text: str,
        spoken_verbatim: str,
    ) -> AdaptiveObservationResult:
        """Apply one daemon-authorized verbatim review through this runtime."""

        with self._lock:
            return submit_explicit_feedback(
                self._adaptive_path,
                self._corrections_path,
                self._vocabulary_path,
                provider_text,
                spoken_verbatim,
            )

    def status_document(self) -> dict[str, Any]:
        """Read content-free statistics and the most recent result."""

        with self._lock:
            return adaptive_status_document(
                self._adaptive_path,
                corrections_path=self._corrections_path,
                vocabulary_path=self._vocabulary_path,
            )

    def _record_result(
        self,
        reason_code: str,
        *,
        replacement_hunks: int = 0,
    ) -> AdaptiveObservationResult:
        result = AdaptiveObservationResult(
            reason_code=reason_code,
            replacement_hunks=replacement_hunks,
        )
        with _adaptive_file_lock(self._adaptive_path):
            ledger = load_adaptive_ledger(self._adaptive_path)
            save_adaptive_ledger(
                with_last_result(ledger, _last_result(result)),
                self._adaptive_path,
            )
        return result

    def _load_snapshot(
        self,
    ) -> tuple[VoiceConfig, tuple[str, ...], tuple[Any, ...], AdaptiveLedger]:
        config = load_config(self._config_path)
        vocabulary = load_vocabulary(self._vocabulary_path)
        manual = load_corrections(self._corrections_path)
        ledger = load_adaptive_ledger(self._adaptive_path)
        return config, vocabulary, manual, ledger


def adaptive_status_document(
    path: str | Path | None = None,
    *,
    corrections_path: str | Path | None = None,
    vocabulary_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return content-free source and effective-provider statistics.

    ``vocabulary.json`` and ``corrections.json`` remain optional explicit
    inputs.  Supplying their paths lets settings/CLI distinguish them from the
    adaptive ledger and from the exact compiled view used by a new dictation.
    """

    ledger = load_adaptive_ledger(path)
    recent = ledger.last_result
    document: dict[str, Any] = {
        "schema_version": ledger.version,
        "statistics": adaptive_statistics(ledger),
        "last_result": (
            {
                "reason_code": recent.reason_code,
                "captured_count": recent.captured_count,
                "activated_count": recent.activated_count,
                "candidate_count": recent.candidate_count,
                "conflicted_count": recent.conflicted_count,
                "replacement_hunks": recent.replacement_hunks,
            }
            if recent is not None
            else None
        ),
    }
    if corrections_path is not None or vocabulary_path is not None:
        manual = (
            load_corrections(corrections_path) if corrections_path is not None else ()
        )
        vocabulary = (
            load_vocabulary(vocabulary_path) if vocabulary_path is not None else ()
        )
        report = compile_provider_correction_report(manual, ledger)
        document["provider_view"] = {
            "explicit_vocabulary_count": len(vocabulary),
            "manual_correction_count": len(manual),
            **report.statistics(),
        }
    return document


def adaptive_review_entries(
    path: str | Path | None = None,
) -> tuple[AdaptiveEntry, ...]:
    """Return only entries that need or permit an explicit local decision."""

    return tuple(
        entry
        for entry in load_adaptive_ledger(path).entries
        if entry.state in {"candidate", "conflicted", "suspended"}
    )


def confirm_adaptive_correction(
    adaptive_path: str | Path,
    corrections_path: str | Path,
    wrong: str,
    canonical: str,
) -> AdaptiveObservationResult:
    """Activate one retained choice under the cross-process ledger lock."""

    path = Path(adaptive_path)
    with _adaptive_file_lock(path):
        ledger = load_adaptive_ledger(path)
        try:
            updated = activate_correction(ledger, wrong, canonical)
            manual = load_corrections(corrections_path)
            provider_report = compile_provider_correction_report(manual, updated)
        except AdaptiveStoreError as error:
            raise ConfigError("adaptive correction ledger is invalid") from error
        identity = (normalized_key(wrong), normalized_key(canonical))
        provider_status = provider_report.status_for(wrong, canonical)
        activated = int(provider_status in {"effective-manual", "effective-adaptive"})
        chosen = next(
            entry
            for entry in updated.entries
            if (normalized_key(entry.wrong), normalized_key(entry.canonical))
            == identity
        )
        observed = AdaptiveObservedCandidate(
            chosen.wrong,
            chosen.canonical,
            chosen.category,
            "explicit",
            chosen.state,
        )
        result = AdaptiveObservationResult(
            reason_code=_confirmation_reason(provider_status),
            captured_count=1,
            activated_count=activated,
            candidates=(observed,),
        )
        save_adaptive_ledger(
            with_last_result(updated, _last_result(result)),
            path,
        )
        # The user-visible success boundary is the on-disk generation that a
        # later daemon process will read, not merely the in-memory mutation.
        # Reload both sources and re-run the same compiler before promising
        # that the next request will contain this rule.
        persisted = load_adaptive_ledger(path)
        persisted_manual = load_corrections(corrections_path)
        try:
            persisted_report = compile_provider_correction_report(
                persisted_manual, persisted
            )
        except AdaptiveStoreError as error:
            raise ConfigError("adaptive correction verification failed") from error
        persisted_status = persisted_report.status_for(wrong, canonical)
        persisted_chosen = next(
            (
                entry
                for entry in persisted.entries
                if (
                    normalized_key(entry.wrong),
                    normalized_key(entry.canonical),
                )
                == identity
            ),
            None,
        )
        if (
            persisted_chosen is None
            or persisted_chosen.state != "active"
            or persisted_status != provider_status
        ):
            raise ConfigError("adaptive correction verification failed")
        return result


def submit_explicit_feedback(
    adaptive_path: str | Path,
    corrections_path: str | Path,
    vocabulary_path: str | Path,
    provider_text: str,
    preferred_text: str,
) -> AdaptiveObservationResult:
    """Reliably learn an explicitly supplied last-result edit.

    This is the cross-application fallback API for clients that cannot expose
    trusted IBus surrounding text.  Only bounded correction pairs are stored;
    neither complete input is persisted in the adaptive ledger.
    """

    if (
        not isinstance(provider_text, str)
        or not isinstance(preferred_text, str)
        or not provider_text
        or not preferred_text
        or len(provider_text) > MAX_EXPLICIT_FEEDBACK_TEXT_CHARACTERS
        or len(preferred_text) > MAX_EXPLICIT_FEEDBACK_TEXT_CHARACTERS
    ):
        raise ConfigError("explicit adaptive feedback is invalid")
    vocabulary = load_vocabulary(vocabulary_path)
    extraction = extract_corrections(
        provider_text,
        0,
        len(provider_text),
        preferred_text,
        approved_term_resolver=lambda text: _canonicalize_approved_term(
            text, vocabulary
        ),
    )
    path = Path(adaptive_path)
    with _adaptive_file_lock(path):
        ledger = load_adaptive_ledger(path)
        if not extraction.candidates:
            result = AdaptiveObservationResult(
                reason_code=f"explicit-feedback-{extraction.reason_code}",
                replacement_hunks=extraction.replacement_hunks,
            )
            save_adaptive_ledger(with_last_result(ledger, _last_result(result)), path)
            return result

        updated = ledger
        try:
            for candidate in extraction.candidates:
                updated = record_evidence(
                    updated,
                    candidate.wrong,
                    candidate.canonical,
                    state="candidate",
                    category=candidate.category,
                    evidence=candidate.evidence,
                )
                updated = activate_correction(
                    updated, candidate.wrong, candidate.canonical
                )
            manual = load_corrections(corrections_path)
            provider_report = compile_provider_correction_report(manual, updated)
        except AdaptiveStoreError as error:
            raise ConfigError("adaptive correction ledger is invalid") from error
        observed = tuple(
            _observed_candidate(candidate, updated)
            for candidate in extraction.candidates
        )
        provider_statuses = tuple(
            provider_report.status_for(item.wrong, item.canonical) for item in observed
        )
        activated = sum(
            status in {"effective-manual", "effective-adaptive"}
            for status in provider_statuses
        )
        result = AdaptiveObservationResult(
            reason_code=_explicit_feedback_reason(activated, provider_statuses),
            captured_count=len(observed),
            activated_count=activated,
            replacement_hunks=extraction.replacement_hunks,
            candidates=tuple(replace(item, evidence="explicit") for item in observed),
        )
        save_adaptive_ledger(
            with_last_result(updated, _last_result(result)),
            path,
        )
        persisted = load_adaptive_ledger(path)
        persisted_manual = load_corrections(corrections_path)
        try:
            persisted_report = compile_provider_correction_report(
                persisted_manual, persisted
            )
        except AdaptiveStoreError as error:
            raise ConfigError("adaptive correction verification failed") from error
        persisted_statuses = tuple(
            persisted_report.status_for(item.wrong, item.canonical) for item in observed
        )
        if persisted_statuses != provider_statuses:
            raise ConfigError("adaptive correction verification failed")
        return result


def _observed_candidate(
    candidate: CorrectionCandidate,
    ledger: AdaptiveLedger,
) -> AdaptiveObservedCandidate:
    identity = (normalized_key(candidate.wrong), normalized_key(candidate.canonical))
    entry = next(
        entry
        for entry in ledger.entries
        if (normalized_key(entry.wrong), normalized_key(entry.canonical)) == identity
    )
    return AdaptiveObservedCandidate(
        wrong=entry.wrong,
        canonical=entry.canonical,
        category=entry.category,
        evidence=entry.evidence,
        state=entry.state,
    )


def _decision_reason(
    activated: int,
    candidates: int,
    conflicted: int,
    provider_statuses: tuple[str | None, ...] = (),
) -> str:
    if conflicted:
        return "conflict-recorded"
    if activated and candidates:
        return "active-and-candidates-saved"
    if candidates:
        return "candidates-saved"
    if activated:
        return "active-learned"
    if provider_statuses and all(
        status == "effective-manual" for status in provider_statuses
    ):
        return "active-already-manual"
    return _suppressed_reason("active", provider_statuses)


def _confirmation_reason(provider_status: str | None) -> str:
    if provider_status == "effective-adaptive":
        return "explicitly-activated"
    if provider_status == "effective-manual":
        return "explicitly-already-manual"
    return _suppressed_reason("explicitly", (provider_status,))


def _explicit_feedback_reason(
    activated: int,
    provider_statuses: tuple[str | None, ...],
) -> str:
    if activated == len(provider_statuses):
        if provider_statuses and all(
            status == "effective-manual" for status in provider_statuses
        ):
            return "explicit-feedback-already-manual"
        return "explicit-feedback-activated"
    if activated:
        return "explicit-feedback-partially-activated"
    return _suppressed_reason("explicit-feedback", provider_statuses)


def _suppressed_reason(prefix: str, statuses: tuple[str | None, ...]) -> str:
    reasons = {
        status.removeprefix("suppressed-")
        for status in statuses
        if isinstance(status, str) and status.startswith("suppressed-")
    }
    if len(reasons) == 1:
        return f"{prefix}-suppressed-{next(iter(reasons))}"
    if len(reasons) > 1:
        return f"{prefix}-suppressed-multiple"
    return f"{prefix}-suppressed"


def _last_result(result: AdaptiveObservationResult) -> AdaptiveLastResult:
    return AdaptiveLastResult(
        reason_code=result.reason_code,
        captured_count=result.captured_count,
        activated_count=result.activated_count,
        candidate_count=result.candidate_count,
        conflicted_count=result.conflicted_count,
        replacement_hunks=result.replacement_hunks,
    )


@contextmanager
def _adaptive_file_lock(path: Path):
    """Serialize daemon/settings ledger mutations without following links."""

    import fcntl

    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = parent.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or parent.is_symlink()
    ):
        raise ConfigError("adaptive correction directory is unsafe")
    parent.chmod(0o700)
    lock_path = path.with_name(".adaptive-corrections.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ConfigError("adaptive correction lock is unavailable") from error
    try:
        lock_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.getuid()
        ):
            raise ConfigError("adaptive correction lock is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _canonicalize_approved_term(text: str, vocabulary: tuple[str, ...]) -> str:
    """Resolve a spelling in O(vocabulary) plus one cached dictionary lookup."""

    key = collapsed_term_key(text)
    personal_matches = tuple(
        term for term in vocabulary if collapsed_term_key(term) == key
    )
    if personal_matches:
        return canonicalize_with_approved_terms(text, personal_matches)

    normalized = unicodedata.normalize("NFKC", text).casefold()
    has_whitespace = any(character.isspace() for character in normalized)
    has_semantic_punctuation = any(
        not character.isspace()
        and not character.isalnum()
        and not unicodedata.category(character).startswith("M")
        for character in normalized
    )
    if not has_whitespace or has_semantic_punctuation:
        # The system dictionary is used only to join whitespace in terms such
        # as "bench mark" -> "benchmark". Symbols in R&D, C++, .NET, paths,
        # handles, and similar technical names carry meaning and are preserved.
        return text
    resolved = _lookup_system_dictionary(key)
    return resolved if resolved is not None else text


@lru_cache(maxsize=128)
def _lookup_system_dictionary(key: str) -> str | None:
    """Resolve one spelling without retaining a full Hunspell index in RAM."""

    paths: list[Path] = []
    for pattern in _SYSTEM_DICTIONARY_GLOBS:
        root = Path(pattern).parent
        paths.extend(sorted(root.glob(Path(pattern).name)))

    preferred: str | None = None
    preferred_normalized: str | None = None
    ambiguous = False
    seen_files: set[tuple[int, int]] = set()
    total_bytes = 0
    for path in paths:
        try:
            stat_result = path.stat()
            identity = (stat_result.st_dev, stat_result.st_ino)
            if identity in seen_files:
                continue
            seen_files.add(identity)
            total_bytes += stat_result.st_size
            if total_bytes > 16 * 1024 * 1024:
                break
            dictionary = path.open("r", encoding="utf-8")
        except OSError:
            continue
        try:
            next(dictionary, None)
            for line in dictionary:
                word = line.split("/", 1)[0].strip()
                if (
                    not word
                    or len(word) > 64
                    or any(not character.isprintable() for character in word)
                ):
                    continue
                if collapsed_term_key(word) != key:
                    continue
                normalized_word = unicodedata.normalize("NFKC", word).casefold()
                if preferred is None:
                    preferred = word
                    preferred_normalized = normalized_word
                    continue
                if normalized_word != preferred_normalized:
                    ambiguous = True
        except (OSError, UnicodeDecodeError):
            continue
        finally:
            dictionary.close()
    logger.info(
        "Checked optional local spelling lexicons (match=%s ambiguous=%s)",
        preferred is not None,
        ambiguous,
    )
    return None if ambiguous else preferred
