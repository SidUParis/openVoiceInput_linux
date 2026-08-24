#!/usr/bin/env python3
"""Render a desktop-entry template without shell interpretation."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"@([A-Z][A-Z0-9_]*)@")


def desktop_exec_quote(value: str) -> str:
    """Quote one absolute executable path for a desktop Exec field."""

    if not value or not Path(value).is_absolute():
        raise ValueError("desktop Exec replacements must be absolute paths")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("desktop Exec replacements cannot contain controls")

    # Desktop Entry strings consume one escaping layer before Exec tokenization.
    # Preserve the second layer required for quoted backslash, quote, backtick,
    # and dollar characters. A doubled percent is a literal, not a field code.
    escaped_parts: list[str] = []
    for character in value:
        if character == "\\":
            escaped_parts.append(r"\\\\")
        elif character == '"':
            escaped_parts.append(r"\\\"")
        elif character in {"`", "$"}:
            escaped_parts.append("\\\\" + character)
        elif character == "%":
            escaped_parts.append("%%")
        else:
            escaped_parts.append(character)
    return f'"{"".join(escaped_parts)}"'


def render_entry(template: str, replacements: dict[str, str]) -> str:
    """Replace every declared placeholder exactly once with a quoted path."""

    placeholders = _PLACEHOLDER_RE.findall(template)
    if len(placeholders) != len(set(placeholders)):
        raise ValueError("desktop template placeholders must be unique")
    supplied = set(replacements)
    expected = set(placeholders)
    if expected != supplied:
        missing = ", ".join(sorted(expected - supplied)) or "none"
        extra = ", ".join(sorted(supplied - expected)) or "none"
        raise ValueError(f"placeholder mismatch (missing: {missing}; extra: {extra})")
    rendered = template
    for key, value in replacements.items():
        if _KEY_RE.fullmatch(key) is None:
            raise ValueError("invalid placeholder name")
        rendered = rendered.replace(f"@{key}@", desktop_exec_quote(value), 1)
    if _PLACEHOLDER_RE.search(rendered):
        raise ValueError("unresolved desktop placeholder")
    return rendered


def _parse_replacements(assignments: list[str]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for assignment in assignments:
        key, separator, value = assignment.partition("=")
        if not separator or _KEY_RE.fullmatch(key) is None or key in replacements:
            raise ValueError("each --set must be a unique NAME=ABSOLUTE_PATH")
        replacements[key] = value
    return replacements


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--set", action="append", default=[], dest="assignments")
    options = parser.parse_args(arguments)
    try:
        replacements = _parse_replacements(options.assignments)
        template = options.template.read_text(encoding="utf-8")
        rendered = render_entry(template, replacements)
        options.output.write_text(rendered, encoding="utf-8")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
