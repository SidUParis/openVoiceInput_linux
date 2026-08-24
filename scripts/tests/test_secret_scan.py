from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCANNER = Path(__file__).resolve().parents[1] / "scan_repository_secrets.py"


class RepositorySecretScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = Path(self.temporary_directory.name)
        self._git("init", "--initial-branch=main")

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_EMAIL": "tests@example.invalid",
                "GIT_AUTHOR_NAME": "Secret Scan Tests",
                "GIT_COMMITTER_EMAIL": "tests@example.invalid",
                "GIT_COMMITTER_NAME": "Secret Scan Tests",
            }
        )
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def _commit_all(self, message: str) -> None:
        self._git("add", "--all")
        self._git("commit", "-m", message)

    def _run_scan(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                str(SCANNER),
                "--repository",
                str(self.repository),
                *arguments,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    @staticmethod
    def _issues(result: subprocess.CompletedProcess[str]) -> list[dict[str, str]]:
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_detects_supported_secret_shapes_without_echoing_values(self) -> None:
        values = {
            "volcengine_uuid_api_key": "9f1c2a3b-4d5e-4a6b-" + "8c7d-0e1f2a3b4c5d",
            "aws_access_key_id": "AKIA" + "7R3Q9W2E5T8Y4U6I",
            "aws_secret_access_key": "aws_secret_access_key="
            + "aB3dE5gH7jK9mN2pQ4sT6vW8yZ0/1+2=3A4B5C6D",
            "aws_session_token": "aws_session_token="
            + "Ab3/De5+Fg7Hi9Jk2Lm4No6Pq8Rs0Tu1Vw3Xy5Za7Bc9De2Fg4Hi6Jk8Lm0No2Pq4Rs6Tu8Vw0Xy9Za7Bc",
            "github_token": "ghp_" + "Ab3dE5gH7jK9mN2pQ4sT6vW8yZ0aB2cD4eF6",
            "openai_api_key": "sk-proj-" + "Ab3dE5gH7jK9mN2pQ4sT6vW8yZ0aB2cD4eF6",
        }
        pem_begin = "-----BEGIN " + "PRIVATE KEY-----"
        pem_end = "-----END " + "PRIVATE KEY-----"
        pem = "\n".join(
            [
                pem_begin,
                "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCAbcwggGzAgEAAkEA",
                "vJ3D4o5FmB7N8pQ2rS6tU9wX1yZ3aC5dE7fG9hJ2kL4mN6pQ",
                pem_end,
            ]
        )
        path = self.repository / "credentials.txt"
        path.write_text("\n".join([*values.values(), pem]), encoding="utf-8")

        result = self._run_scan("--no-history")

        self.assertEqual(result.returncode, 1, result.stderr)
        rules = {issue["rule"] for issue in self._issues(result)}
        self.assertEqual(rules, {*values, "private_key_pem"})
        combined_output = result.stdout + result.stderr
        for value in values.values():
            self.assertNotIn(value, combined_output)
        self.assertNotIn(pem, combined_output)

    def test_finds_a_removed_secret_in_reachable_history(self) -> None:
        secret = "2a4c6e8f-1b3d-4f5a-" + "9c7e-0d2f4a6b8c1e"
        path = self.repository / "old-config.json"
        path.write_text('{"api_key": "' + secret + '"}\n', encoding="utf-8")
        self._commit_all("add old config")
        path.write_text('{"api_key": "redacted"}\n', encoding="utf-8")
        self._commit_all("remove credential")

        result = self._run_scan("--no-current")

        self.assertEqual(result.returncode, 1, result.stderr)
        issues = self._issues(result)
        self.assertTrue(
            any(
                issue["path"] == "old-config.json"
                and issue["rule"] == "volcengine_uuid_api_key"
                and issue["source"] == "history"
                for issue in issues
            )
        )
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_scans_index_even_when_worktree_was_overwritten(self) -> None:
        path = self.repository / "staged.txt"
        path.write_text("safe\n", encoding="utf-8")
        self._commit_all("initial")
        secret = "github_pat_" + "A1b2C3d4E5f6G7h8I9j0K1m2N3p4Q5r6S7t8U9"
        path.write_text(secret + "\n", encoding="utf-8")
        self._git("add", str(path.name))
        path.write_text("safe again\n", encoding="utf-8")

        result = self._run_scan("--no-history")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(
            any(
                issue["source"] == "index" and issue["rule"] == "github_token"
                for issue in self._issues(result)
            )
        )
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_placeholders_ignored_files_and_binary_files_do_not_report(self) -> None:
        (self.repository / ".gitignore").write_text(".env\n", encoding="utf-8")
        placeholders = [
            "00000000-0000-4000-8000-000000000000",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_exampleexampleexampleexampleexampleexample",
            "sk-proj-placeholderplaceholderplaceholder",
        ]
        (self.repository / "config.example").write_text(
            "\n".join(placeholders), encoding="utf-8"
        )
        ignored_secret = "8a7b6c5d-4e3f-4a2b-" + "9c8d-7e6f5a4b3c2d"
        (self.repository / ".env").write_text(ignored_secret, encoding="utf-8")
        binary_secret = "gho_" + "A1b2C3d4E5f6G7h8I9j0K1m2N3p4Q5r6"
        (self.repository / "image.bin").write_bytes(b"\0" + binary_secret.encode())

        result = self._run_scan("--no-history")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._issues(result), [])
        for value in [*placeholders, ignored_secret, binary_secret]:
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_oversized_text_fails_closed_without_reading_it_all(self) -> None:
        (self.repository / "large.txt").write_text("A" * 1024, encoding="utf-8")

        result = self._run_scan(
            "--no-history", "--max-blob-bytes", "64", "--max-total-bytes", "256"
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(
            {issue["rule"] for issue in self._issues(result)},
            {"unscanned_large_text"},
        )
        self.assertNotIn("A" * 64, result.stdout + result.stderr)

    def test_a_secret_shaped_filename_is_redacted_in_output(self) -> None:
        filename_secret = "gho_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8"
        content_secret = "7d6c5b4a-3f2e-4d1c-" + "8b9a-0f1e2d3c4b5a"
        path = self.repository / f"unsafe-{filename_secret}.txt"
        path.write_text(content_secret, encoding="utf-8")

        result = self._run_scan("--no-history")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("<redacted>", result.stdout)
        self.assertNotIn(filename_secret, result.stdout + result.stderr)
        self.assertNotIn(content_secret, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
