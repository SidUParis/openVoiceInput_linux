"""Private persistence and per-utterance loading for adaptive corrections."""

from __future__ import annotations

import json
import logging
import threading
import unicodedata
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from .adaptive_correction import (
    canonicalize_with_approved_terms,
    collapsed_term_key,
    extract_correction,
)
from .adaptive_store import (
    AdaptiveLedger,
    AdaptiveStoreError,
    compile_provider_corrections,
    normalized_key,
    parse_adaptive_ledger,
    record_correction,
    serialize_adaptive_ledger,
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
_SYSTEM_DICTIONARY_GLOBS = (
    "/usr/share/hunspell/en_US*.dic",
    "/usr/share/hunspell/en_GB*.dic",
    "/usr/share/hunspell/fr*.dic",
)

logger = logging.getLogger(__name__)


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
                effective = replace(
                    config,
                    hotwords=vocabulary,
                    corrections=compile_provider_corrections(manual, ledger),
                )
            except AdaptiveStoreError as error:
                raise ConfigError("adaptive correction ledger is invalid") from error

        # Keep provider/GI imports lazy for configure and status-only commands.
        from .volcengine import VolcengineASRClient

        return VolcengineASRClient(effective.provider_settings())

    def observe(self, snapshot: Any) -> bool:
        """Record one strict replacement without retaining surrounding text."""

        with self._lock:
            if (
                type(snapshot.cursor) is not int
                or type(snapshot.anchor) is not int
                or snapshot.cursor != snapshot.anchor
            ):
                return False
            vocabulary = load_vocabulary(self._vocabulary_path)
            candidate = extract_correction(
                snapshot.baseline_text,
                snapshot.committed_start,
                snapshot.committed_end,
                snapshot.current_text,
                approved_term_resolver=lambda text: _canonicalize_approved_term(
                    text,
                    vocabulary,
                ),
            )
            if candidate is None:
                return False
            ledger = load_adaptive_ledger(self._adaptive_path)
            try:
                updated = record_correction(
                    ledger,
                    candidate.wrong,
                    candidate.canonical,
                )
            except AdaptiveStoreError as error:
                raise ConfigError("adaptive correction ledger is invalid") from error
            save_adaptive_ledger(updated, self._adaptive_path)
            manual = load_corrections(self._corrections_path)
            try:
                provider_view = compile_provider_corrections(manual, updated)
            except AdaptiveStoreError as error:
                raise ConfigError("adaptive correction ledger is invalid") from error
            source_key = normalized_key(candidate.wrong)
            canonical_key = normalized_key(candidate.canonical)
            if any(normalized_key(pair.wrong) == source_key for pair in manual):
                return False
            active_matches = tuple(
                entry
                for entry in updated.entries
                if entry.state == "active"
                and normalized_key(entry.wrong) == source_key
                and normalized_key(entry.canonical) == canonical_key
            )
            return any(
                pair.wrong == entry.wrong and pair.canonical == entry.canonical
                for entry in active_matches
                for pair in provider_view
            )

    def _load_snapshot(
        self,
    ) -> tuple[VoiceConfig, tuple[str, ...], tuple[Any, ...], AdaptiveLedger]:
        config = load_config(self._config_path)
        vocabulary = load_vocabulary(self._vocabulary_path)
        manual = load_corrections(self._corrections_path)
        ledger = load_adaptive_ledger(self._adaptive_path)
        return config, vocabulary, manual, ledger


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
