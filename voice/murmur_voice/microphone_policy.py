# SPDX-License-Identifier: GPL-3.0-only
"""Private, versioned microphone-priority policy configuration."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import (
    ConfigError,
    _load_private_bytes,
    _reject_duplicate_json_fields,
    _write_private_json,
)

MICROPHONE_POLICY_SCHEMA_VERSION = 1
MAX_MICROPHONE_POLICY_BYTES = 32 * 1024
MAX_PULSE_SOURCE_NAME_CHARACTERS = 512

MICROPHONE_CATEGORIES = ("dji", "headset", "external", "built-in")
DEFAULT_MICROPHONE_PRIORITY = MICROPHONE_CATEGORIES
_CATEGORY_SET = frozenset(MICROPHONE_CATEGORIES)


@dataclass(frozen=True, slots=True, repr=False)
class MicrophoneSourcePreference:
    """One optional exact Pulse source used to disambiguate a category."""

    category: str
    source: str = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class MicrophonePolicyConfig:
    """A complete category order plus optional exact source choices."""

    priority: tuple[str, ...] = DEFAULT_MICROPHONE_PRIORITY
    preferred_sources: tuple[MicrophoneSourcePreference, ...] = field(
        default=(), repr=False
    )

    def __post_init__(self) -> None:
        normalized_priority = normalize_microphone_priority(self.priority)
        normalized_preferences = normalize_microphone_source_preferences(
            self.preferred_sources
        )
        object.__setattr__(self, "priority", normalized_priority)
        object.__setattr__(self, "preferred_sources", normalized_preferences)

    def preferred_source_for(self, category: str) -> str | None:
        """Return the configured exact source for ``category``, if any."""

        return next(
            (
                preference.source
                for preference in self.preferred_sources
                if preference.category == category
            ),
            None,
        )


def default_microphone_policy_config_path() -> Path:
    """Return the per-user microphone-priority policy path."""

    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "murmur-ime" / "microphone-priority.json"


def load_microphone_policy_config(
    path: str | os.PathLike[str] | None = None,
) -> MicrophonePolicyConfig:
    """Load a private policy; only an absent file means reviewed defaults.

    An existing malformed, unsafe, or unsupported file raises ``ConfigError``.
    Runtime callers must fail before opening a microphone instead of silently
    replacing an explicit user choice with defaults.
    """

    config_path = (
        Path(path) if path is not None else default_microphone_policy_config_path()
    )
    raw = _load_private_bytes(
        config_path,
        kind="microphone priority configuration",
        limit=MAX_MICROPHONE_POLICY_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return MicrophonePolicyConfig()
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ConfigError(
            "microphone priority configuration is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "version",
        "priority",
        "preferred_sources",
    }:
        raise ConfigError("microphone priority configuration has unsupported fields")
    version = document.get("version")
    if type(version) is not int or version != MICROPHONE_POLICY_SCHEMA_VERSION:
        raise ConfigError(
            "microphone priority configuration uses an unsupported version"
        )
    return MicrophonePolicyConfig(
        priority=normalize_microphone_priority(document.get("priority")),
        preferred_sources=normalize_microphone_source_preferences(
            document.get("preferred_sources")
        ),
    )


def save_microphone_policy_config(
    priority: Sequence[str],
    path: str | os.PathLike[str] | None = None,
    *,
    preferred_sources: (
        Mapping[str, str]
        | Sequence[MicrophoneSourcePreference | Mapping[str, str]]
        | None
    ) = None,
) -> Path:
    """Atomically save a complete policy in a private file.

    Passing ``preferred_sources=None`` preserves existing exact choices. Pass
    an empty mapping or sequence to explicitly clear them. This lets a simple
    category-order UI avoid erasing a more specific device selection.
    """

    config_path = (
        Path(path) if path is not None else default_microphone_policy_config_path()
    )
    normalized_priority = normalize_microphone_priority(priority)
    if preferred_sources is None:
        normalized_preferences = load_microphone_policy_config(
            config_path
        ).preferred_sources
    else:
        normalized_preferences = normalize_microphone_source_preferences(
            preferred_sources
        )
    document = {
        "version": MICROPHONE_POLICY_SCHEMA_VERSION,
        "priority": list(normalized_priority),
        "preferred_sources": {
            preference.category: preference.source
            for preference in normalized_preferences
        },
    }
    return _write_private_json(
        config_path,
        document,
        kind="microphone priority configuration",
        temporary_prefix=".microphone-priority.json.",
    )


def normalize_microphone_priority(values: Any) -> tuple[str, ...]:
    """Require every supported category exactly once."""

    if not isinstance(values, (list, tuple)):
        raise ConfigError("microphone priority must be a list")
    if len(values) != len(MICROPHONE_CATEGORIES):
        raise ConfigError("microphone priority must contain every category once")
    if any(not isinstance(value, str) for value in values):
        raise ConfigError("microphone priority categories must be strings")
    normalized = tuple(values)
    if len(set(normalized)) != len(normalized) or set(normalized) != _CATEGORY_SET:
        raise ConfigError("microphone priority must contain every category once")
    return normalized


def normalize_microphone_source_preferences(
    values: Any,
) -> tuple[MicrophoneSourcePreference, ...]:
    """Validate at most one exact Pulse source for each category."""

    if isinstance(values, Mapping):
        raw_entries: Sequence[Any] = tuple(
            MicrophoneSourcePreference(category=category, source=source)
            for category, source in values.items()
        )
    elif isinstance(values, (list, tuple)):
        raw_entries = values
    else:
        raise ConfigError("preferred microphone sources must be an object")
    if len(raw_entries) > len(MICROPHONE_CATEGORIES):
        raise ConfigError("preferred microphone sources contain too many entries")

    normalized: list[MicrophoneSourcePreference] = []
    seen_categories: set[str] = set()
    for raw_entry in raw_entries:
        if isinstance(raw_entry, MicrophoneSourcePreference):
            category = raw_entry.category
            source = raw_entry.source
        elif isinstance(raw_entry, Mapping) and set(raw_entry) == {
            "category",
            "source",
        }:
            category = raw_entry.get("category")
            source = raw_entry.get("source")
        else:
            raise ConfigError("preferred microphone source entry is invalid")
        if category not in _CATEGORY_SET or category in seen_categories:
            raise ConfigError("preferred microphone source category is invalid")
        if (
            not isinstance(source, str)
            or not source
            or len(source) > MAX_PULSE_SOURCE_NAME_CHARACTERS
            or any(not character.isprintable() for character in source)
        ):
            raise ConfigError("preferred microphone source name is invalid")
        seen_categories.add(category)
        normalized.append(MicrophoneSourcePreference(category=category, source=source))
    # Schema output is deterministic regardless of mapping insertion order.
    normalized.sort(key=lambda item: MICROPHONE_CATEGORIES.index(item.category))
    return tuple(normalized)
