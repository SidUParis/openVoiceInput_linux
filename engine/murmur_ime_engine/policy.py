"""Small, dependency-free validation helpers for the IBus boundary."""

from __future__ import annotations

import re

# These are the stable numeric values of IBus.InputPurpose.PASSWORD/PIN and
# IBus.InputHints.PRIVATE.  Keeping this policy module free of GI makes the
# security rules easy to unit test without a graphical session.
PASSWORD_PURPOSE = 8
PIN_PURPOSE = 9
PRIVATE_HINT = 1 << 11

MAX_UTTERANCE_ID_LENGTH = 128
MAX_TEXT_CODEPOINTS = 4096
MAX_TEXT_UTF8_BYTES = 16 * 1024

_UTTERANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def is_private_input(purpose: int, hints: int) -> bool:
    """Return True for password, PIN, or explicitly private fields."""

    return purpose in (PASSWORD_PURPOSE, PIN_PURPOSE) or bool(hints & PRIVATE_HINT)


def is_real_input_client(client: str) -> bool:
    """Reject IBus's non-editable global placeholder input context.

    IBus uses the literal client name ``fake`` for its global/fallback input
    context. An empty name remains valid for compatibility with clients and
    IBus versions that cannot identify themselves.
    """

    return client != "fake"


def valid_utterance_id(value: str) -> bool:
    """Accept compact opaque IDs while rejecting control characters."""

    return (
        bool(value)
        and len(value) <= MAX_UTTERANCE_ID_LENGTH
        and _UTTERANCE_ID_RE.fullmatch(value) is not None
    )


def valid_preedit_text(value: str) -> bool:
    """Bound untrusted D-Bus text before it reaches an application."""

    if len(value) > MAX_TEXT_CODEPOINTS or "\x00" in value:
        return False
    return len(value.encode("utf-8")) <= MAX_TEXT_UTF8_BYTES


def valid_surrounding_text(value: object, cursor: int, anchor: int) -> bool:
    """Bound one IBus surrounding snapshot and its character offsets."""

    return (
        isinstance(value, str)
        and isinstance(cursor, int)
        and not isinstance(cursor, bool)
        and isinstance(anchor, int)
        and not isinstance(anchor, bool)
        and 0 <= cursor <= len(value)
        and 0 <= anchor <= len(value)
        and valid_preedit_text(value)
    )
