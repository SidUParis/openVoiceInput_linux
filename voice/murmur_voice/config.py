"""Read separate private provider-key, vocabulary, and correction JSON files."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"
MAX_CONFIG_BYTES = 16 * 1024
MAX_API_KEY_BYTES = 4096
MAX_VOCABULARY_BYTES = 128 * 1024
MAX_VOCABULARY_ENTRIES = 200
MAX_VOCABULARY_TERM_CHARACTERS = 64
CORRECTIONS_SCHEMA_VERSION = 1
MAX_CORRECTIONS_BYTES = 64 * 1024
MAX_CORRECTION_PAIRS = 50
MAX_CORRECTION_TEXT_CHARACTERS = 64


class ConfigError(RuntimeError):
    """A safe-to-display configuration error that never contains a secret."""


class _DuplicateJSONField(ValueError):
    """Internal marker for an ambiguous object in a private JSON file."""


@dataclass(frozen=True, slots=True, repr=False)
class CorrectionPair:
    """One explicit provider-side replacement, hidden from debug reprs."""

    wrong: str
    canonical: str


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    """Validated provider configuration.

    The public fallback deliberately stores only the API key. Provider
    behavior stays on reviewed defaults so users don't need to copy a legacy
    Doubao Murmur configuration file.
    """

    api_key: str = field(repr=False)
    hotwords: tuple[str, ...] = field(default=(), repr=False)
    corrections: tuple[CorrectionPair, ...] = field(default=(), repr=False)

    def provider_settings(self) -> dict[str, Any]:
        settings = {
            "api_key": self.api_key,
            "endpoint": DEFAULT_ENDPOINT,
            "resource_id": DEFAULT_RESOURCE_ID,
            "uid": "murmur-ime-voice",
            "enable_nonstream": True,
            "enable_ddc": True,
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
            "result_type": "full",
            "end_window_size": 800,
            "chunk_ms": 200,
            "final_result_timeout": 20.0,
            "max_pending_audio_seconds": 10.0,
        }
        hotwords = normalize_vocabulary_terms(self.hotwords)
        if hotwords:
            settings["hotwords"] = hotwords
        corrections = normalize_correction_pairs(self.corrections)
        if corrections:
            settings["corrections"] = corrections
        return settings


def default_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "murmur-ime" / "voice.json"


def default_vocabulary_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "murmur-ime" / "vocabulary.json"


def default_corrections_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "murmur-ime" / "corrections.json"


def default_adaptive_corrections_path() -> Path:
    """Return the private learned-correction ledger path."""

    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "murmur-ime" / "adaptive-corrections.json"


def load_config(path: str | os.PathLike[str] | None = None) -> VoiceConfig:
    """Load a regular, user-owned, permission-0600 key-only JSON file."""

    config_path = Path(path) if path is not None else default_config_path()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(config_path, flags)
    except FileNotFoundError as error:
        raise ConfigError("voice configuration file not found") from error
    except OSError as error:
        raise ConfigError(
            "voice configuration file could not be opened safely"
        ) from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigError("voice configuration must be a regular file")
        if metadata.st_uid != os.getuid():
            raise ConfigError("voice configuration must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ConfigError("voice configuration permissions must be 0600")
        raw = _read_bounded(descriptor, MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ConfigError("voice configuration is too large")
    finally:
        os.close(descriptor)

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError("voice configuration is not valid UTF-8 JSON") from error
    if not isinstance(document, dict) or set(document) != {"api_key"}:
        raise ConfigError("voice configuration must contain only api_key")

    return VoiceConfig(api_key=_validate_api_key(document.get("api_key")))


def save_api_key(api_key: str, path: str | os.PathLike[str] | None = None) -> Path:
    """Atomically write a key-only config with directory 0700 and file 0600."""

    validated = _validate_api_key(api_key)
    config_path = Path(path) if path is not None else default_config_path()
    return _write_private_json(
        config_path,
        {"api_key": validated},
        kind="voice configuration",
        temporary_prefix=".voice.json.",
    )


def delete_api_key(path: str | os.PathLike[str] | None = None) -> bool:
    """Remove only a private, regular, user-owned key file.

    The settings controller is responsible for proving that the voice service
    is inactive before calling this filesystem-only operation.  Returning
    ``False`` for an absent file makes a repeated confirmed removal harmless.
    """

    config_path = Path(path) if path is not None else default_config_path()
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW

    try:
        directory_descriptor = os.open(config_path.parent, directory_flags)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ConfigError("voice configuration could not be removed safely") from error

    try:
        directory_metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.getuid()
            or stat.S_IMODE(directory_metadata.st_mode) & 0o077
        ):
            raise ConfigError("voice configuration could not be removed safely")

        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0)
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(
                config_path.name,
                file_flags,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ConfigError(
                "voice configuration could not be removed safely"
            ) from error

        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ConfigError("voice configuration could not be removed safely")
            current_metadata = os.stat(
                config_path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                current_metadata.st_dev != metadata.st_dev
                or current_metadata.st_ino != metadata.st_ino
            ):
                raise ConfigError("voice configuration could not be removed safely")
        except OSError as error:
            raise ConfigError(
                "voice configuration could not be removed safely"
            ) from error
        finally:
            os.close(descriptor)

        try:
            os.unlink(config_path.name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ConfigError(
                "voice configuration could not be removed safely"
            ) from error
    finally:
        os.close(directory_descriptor)
    return True


def load_vocabulary(
    path: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Load the optional private vocabulary, returning empty when absent."""

    vocabulary_path = Path(path) if path is not None else default_vocabulary_path()
    raw = _load_private_bytes(
        vocabulary_path,
        kind="personal vocabulary",
        limit=MAX_VOCABULARY_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return ()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError("personal vocabulary is not valid UTF-8 JSON") from error
    if not isinstance(document, dict) or set(document) != {"terms"}:
        raise ConfigError("personal vocabulary must contain only terms")
    return normalize_vocabulary_terms(document.get("terms"))


def save_vocabulary(
    terms: Any,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Validate and atomically store the separate private vocabulary file."""

    normalized = normalize_vocabulary_terms(terms)
    vocabulary_path = Path(path) if path is not None else default_vocabulary_path()
    return _write_private_json(
        vocabulary_path,
        {"terms": list(normalized)},
        kind="personal vocabulary",
        temporary_prefix=".vocabulary.json.",
    )


def load_corrections(
    path: str | os.PathLike[str] | None = None,
) -> tuple[CorrectionPair, ...]:
    """Load optional explicit recognition corrections from a private JSON file."""

    corrections_path = Path(path) if path is not None else default_corrections_path()
    raw = _load_private_bytes(
        corrections_path,
        kind="recognition corrections",
        limit=MAX_CORRECTIONS_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return ()
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONField) as error:
        raise ConfigError("recognition corrections are not valid UTF-8 JSON") from error
    if not isinstance(document, dict) or set(document) != {"version", "pairs"}:
        raise ConfigError("recognition corrections must contain only version and pairs")
    version = document.get("version")
    if type(version) is not int or version != CORRECTIONS_SCHEMA_VERSION:
        raise ConfigError("recognition corrections use an unsupported schema version")
    return normalize_correction_pairs(document.get("pairs"))


def save_corrections(
    pairs: Any,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Validate and atomically store explicit recognition corrections."""

    normalized = normalize_correction_pairs(pairs)
    corrections_path = Path(path) if path is not None else default_corrections_path()
    return _write_private_json(
        corrections_path,
        {
            "version": CORRECTIONS_SCHEMA_VERSION,
            "pairs": [
                {"wrong": pair.wrong, "canonical": pair.canonical}
                for pair in normalized
            ],
        },
        kind="recognition corrections",
        temporary_prefix=".corrections.json.",
    )


def load_vocabulary_import(path: str | os.PathLike[str]) -> tuple[str, ...]:
    """Read a private UTF-8 one-term-per-line import without following links."""

    import_path = Path(path)
    raw = _load_private_bytes(
        import_path,
        kind="vocabulary import",
        limit=MAX_VOCABULARY_BYTES,
        missing_ok=False,
    )
    assert raw is not None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigError("vocabulary import is not valid UTF-8") from error
    if "\x00" in text or "\r" in text:
        raise ConfigError("vocabulary import contains a forbidden control character")
    return normalize_vocabulary_terms(
        [line for line in text.split("\n") if line.strip()]
    )


def normalize_vocabulary_terms(values: Any) -> tuple[str, ...]:
    """Trim and casefold-deduplicate terms while preserving their first spelling."""

    if not isinstance(values, (list, tuple)):
        raise ConfigError("personal vocabulary terms must be a list")
    if len(values) > MAX_VOCABULARY_ENTRIES:
        raise ConfigError(
            f"personal vocabulary exceeds {MAX_VOCABULARY_ENTRIES} entries"
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ConfigError("personal vocabulary entries must be strings")
        if any(character in value for character in "\x00\r\n"):
            raise ConfigError(
                "personal vocabulary entry contains a forbidden control character"
            )
        term = value.strip()
        if not term:
            raise ConfigError("personal vocabulary entry must not be empty")
        if len(term) > MAX_VOCABULARY_TERM_CHARACTERS:
            raise ConfigError(
                "personal vocabulary entry exceeds "
                f"{MAX_VOCABULARY_TERM_CHARACTERS} Unicode characters"
            )
        folded = term.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        normalized.append(term)
    return tuple(normalized)


def normalize_correction_pairs(values: Any) -> tuple[CorrectionPair, ...]:
    """Validate, trim, and deterministically deduplicate explicit corrections."""

    if not isinstance(values, (list, tuple)):
        raise ConfigError("recognition correction pairs must be a list")
    if len(values) > MAX_CORRECTION_PAIRS:
        raise ConfigError(
            f"recognition corrections exceed {MAX_CORRECTION_PAIRS} pairs"
        )

    normalized: list[CorrectionPair] = []
    by_wrong: dict[str, str] = {}
    for value in values:
        if isinstance(value, CorrectionPair):
            wrong_value = value.wrong
            canonical_value = value.canonical
        elif isinstance(value, dict) and set(value) == {"wrong", "canonical"}:
            wrong_value = value.get("wrong")
            canonical_value = value.get("canonical")
        else:
            raise ConfigError(
                "each recognition correction must contain only wrong and canonical"
            )

        wrong = _normalize_correction_text(wrong_value, side="wrong")
        canonical = _normalize_correction_text(
            canonical_value,
            side="canonical",
        )
        if wrong in by_wrong:
            if by_wrong[wrong] != canonical:
                raise ConfigError(
                    "recognition corrections contain a conflicting wrong form"
                )
            continue
        by_wrong[wrong] = canonical
        normalized.append(CorrectionPair(wrong=wrong, canonical=canonical))
    return tuple(normalized)


def _normalize_correction_text(value: Any, *, side: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"recognition correction {side} values must be strings")
    if any(not character.isprintable() for character in value):
        raise ConfigError(
            f"recognition correction {side} contains a forbidden control character"
        )
    text = value.strip()
    if not text:
        raise ConfigError(f"recognition correction {side} must not be empty")
    if len(text) > MAX_CORRECTION_TEXT_CHARACTERS:
        raise ConfigError(
            f"recognition correction {side} exceeds "
            f"{MAX_CORRECTION_TEXT_CHARACTERS} Unicode characters"
        )
    return text


def _reject_duplicate_json_fields(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for name, value in pairs:
        if name in document:
            raise _DuplicateJSONField
        document[name] = value
    return document


def _load_private_bytes(
    path: Path,
    *,
    kind: str,
    limit: int,
    missing_ok: bool,
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ConfigError(f"{kind} file not found") from None
    except OSError as error:
        raise ConfigError(f"{kind} could not be opened safely") from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigError(f"{kind} must be a regular file")
        if metadata.st_uid != os.getuid():
            raise ConfigError(f"{kind} must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ConfigError(f"{kind} permissions must be 0600")
        raw = _read_bounded(descriptor, limit + 1)
        if len(raw) > limit:
            raise ConfigError(f"{kind} is too large")
        return raw
    finally:
        os.close(descriptor)


def _write_private_json(
    path: Path,
    document: dict[str, Any],
    *,
    kind: str,
    temporary_prefix: str,
) -> Path:
    parent = path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_metadata = parent.lstat()
    except OSError as error:
        raise ConfigError(f"{kind} directory is unavailable") from error
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or parent.is_symlink()
    ):
        raise ConfigError(f"{kind} directory must be a user-owned directory")
    try:
        parent.chmod(0o700)
    except OSError as error:
        raise ConfigError(
            f"{kind} directory permissions could not be secured"
        ) from error

    if path.exists() or path.is_symlink():
        try:
            target_metadata = path.lstat()
        except OSError as error:
            raise ConfigError(f"existing {kind} is unsafe") from error
        if (
            not stat.S_ISREG(target_metadata.st_mode)
            or target_metadata.st_uid != os.getuid()
        ):
            raise ConfigError(f"existing {kind} is unsafe")

    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=temporary_prefix, dir=parent
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_name, path)
        temporary_name = ""
        os.chmod(path, 0o600)
    except OSError as error:
        raise ConfigError(f"{kind} could not be written safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return path


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _validate_api_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("api_key must be a string")
    value = value.strip()
    if (
        not value
        or any(character in value for character in "\x00\r\n")
        or len(value.encode("utf-8")) > MAX_API_KEY_BYTES
    ):
        raise ConfigError("api_key is empty or invalid")
    return value
