#!/usr/bin/env python3
"""Render user-service templates without invoking a shell at runtime."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"@([A-Z][A-Z0-9_]*)@")


def systemd_quote(value: str) -> str:
    """Quote one absolute path for a systemd directive or ExecStart word."""

    if not value or not Path(value).is_absolute():
        raise ValueError("systemd path replacements must be absolute")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("systemd path replacements cannot contain controls")
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def render_unit(template: str, replacements: dict[str, str]) -> str:
    """Replace every declared @NAME@ exactly once with a quoted path."""

    placeholders = set(_PLACEHOLDER_RE.findall(template))
    supplied = set(replacements)
    if placeholders != supplied:
        missing = ", ".join(sorted(placeholders - supplied)) or "none"
        extra = ", ".join(sorted(supplied - placeholders)) or "none"
        raise ValueError(f"placeholder mismatch (missing: {missing}; extra: {extra})")
    rendered = template
    for key, value in replacements.items():
        if _KEY_RE.fullmatch(key) is None:
            raise ValueError("invalid placeholder name")
        rendered = rendered.replace(f"@{key}@", systemd_quote(value))
    if _PLACEHOLDER_RE.search(rendered):
        raise ValueError("unresolved systemd placeholder")
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
        rendered = render_unit(template, replacements)
        options.output.write_text(rendered, encoding="utf-8")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
