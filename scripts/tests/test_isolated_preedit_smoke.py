from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPOSITORY / "scripts" / "run_isolated_preedit_smoke.py"
SPEC = importlib.util.spec_from_file_location("run_isolated_preedit_smoke", SMOKE_PATH)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class IsolatedPreeditSmokeTests(unittest.TestCase):
    def test_window_search_waits_for_a_visible_probe(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="42\n", stderr="")
        with (
            mock.patch.object(smoke, "system_command", return_value="/usr/bin/xdotool"),
            mock.patch.object(smoke.subprocess, "run", return_value=completed) as run,
        ):
            window = smoke.find_probe_window({"DISPLAY": ":99"})

        self.assertEqual(window, "42")
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/xdotool",
                "search",
                "--onlyvisible",
                "--name",
                "Isolated Preedit Probe",
            ],
        )


if __name__ == "__main__":
    unittest.main()
