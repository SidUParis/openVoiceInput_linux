#!/usr/bin/python3 -I
"""Root-owned entry point for the private, hash-locked application import tree."""

from __future__ import annotations

import sys
from pathlib import Path


APPLICATION_ROOT = Path("/usr/lib/open-voice-input-linux/python")
ENTRY_POINTS = {
    "murmur-ime-engine": ("murmur_ime_engine.main", "main"),
    "murmur-voice-daemon": ("murmur_voice.cli", "main"),
    "open-voice-input-settings": ("murmur_voice.settings_app", "main"),
}


def main() -> int:
    sys.dont_write_bytecode = True
    command = Path(sys.argv[0]).name
    try:
        module_name, function_name = ENTRY_POINTS[command]
    except KeyError:
        print(f"unsupported Open Voice Input launcher name: {command}", file=sys.stderr)
        return 2
    # Isolated mode ignores PYTHONPATH and the user site.  The only additional
    # import root is installed by dpkg and is never writable by an ordinary
    # desktop user.  Ubuntu's system dist-packages remains available for GI.
    sys.path.insert(0, str(APPLICATION_ROOT))
    module = __import__(module_name, fromlist=[function_name])
    entry_point = getattr(module, function_name)
    return int(entry_point())


if __name__ == "__main__":
    raise SystemExit(main())
