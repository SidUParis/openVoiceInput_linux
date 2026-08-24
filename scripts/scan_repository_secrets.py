#!/usr/bin/env python3
"""Scan the current repository and reachable Git history for credential shapes.

The scanner deliberately reports only an object/location and a rule name. It
never prints the matched bytes. Ignored worktree files (for example a private
``.env``) are outside the repository boundary, while tracked/indexed content
and every reachable historical blob are inside it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_BLOB_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 128 * 1024 * 1024
BINARY_SAMPLE_BYTES = 8192


@dataclass(frozen=True)
class SecretRule:
    name: str
    pattern: re.Pattern[bytes]


SECRET_RULES = (
    SecretRule(
        "volcengine_uuid_api_key",
        re.compile(
            rb"(?<![0-9A-Fa-f])"
            rb"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-4[0-9A-Fa-f]{3}-"
            rb"[89AaBb][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}"
            rb"(?![0-9A-Fa-f])"
        ),
    ),
    SecretRule(
        "aws_access_key_id",
        re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ),
    SecretRule(
        "aws_secret_access_key",
        re.compile(
            rb"(?i:aws_secret_access_key)[ \t]*[:=][ \t]*[\"']?"
            rb"[A-Za-z0-9/+=]{40}"
        ),
    ),
    SecretRule(
        "aws_session_token",
        re.compile(
            rb"(?i:aws_session_token)[ \t]*[:=][ \t]*[\"']?"
            rb"[A-Za-z0-9/+=]{80,}"
        ),
    ),
    SecretRule(
        "github_token",
        re.compile(
            rb"(?<![A-Za-z0-9_])"
            rb"(?:gh[pousr]_[A-Za-z0-9_]{20,255}|github_pat_[A-Za-z0-9_]{20,255})"
            rb"(?![A-Za-z0-9_])"
        ),
    ),
    SecretRule(
        "openai_api_key",
        re.compile(
            rb"(?<![A-Za-z0-9_-])sk-(?:(?:proj|svcacct)-)?"
            rb"[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
        ),
    ),
    SecretRule(
        "private_key_pem",
        re.compile(
            rb"-----BEGIN (?P<pem_kind>(?:(?:RSA|DSA|EC|OPENSSH) )?PRIVATE KEY)-----"
            rb"[\s\S]{1,131072}?"
            rb"-----END (?P=pem_kind)-----"
        ),
    ),
)

PLACEHOLDER_MARKERS = (
    b"changeme",
    b"example",
    b"notareal",
    b"placeholder",
    b"redacted",
    b"replacewith",
    b"yourapikey",
    b"yourtoken",
)

KNOWN_EXAMPLE_UUIDS = (
    b"123e4567-e89b-42d3-a456-426614174000",
    b"f47ac10b-58cc-4372-a567-0e02b2c3d479",
)


@dataclass(frozen=True)
class ScanIssue:
    blob: str
    path: str
    rule: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {
            "blob": self.blob,
            "path": _safe_path(self.path),
            "rule": self.rule,
            "source": self.source,
        }


@dataclass
class ScanReport:
    findings: list[ScanIssue]
    problems: list[ScanIssue]


class ScanFailure(RuntimeError):
    """Raised when Git metadata cannot be inspected safely."""


def _run_git(
    repository: Path, arguments: list[str], *, input_data: bytes = b""
) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise ScanFailure(f"git command failed: {arguments[0]}")
    return result.stdout


def _safe_path(path: str) -> str:
    """Keep a malicious filename from echoing a credential in scanner output."""
    encoded = os.fsencode(path)
    for rule in SECRET_RULES:
        encoded = rule.pattern.sub(b"<redacted>", encoded)
    return os.fsdecode(encoded)


def _is_placeholder(rule: SecretRule, candidate: bytes) -> bool:
    if rule.name == "private_key_pem":
        # A complete private-key block does not belong in repository content,
        # even if a surrounding fixture describes it as fake or sample data.
        return False

    lowered = candidate.lower()
    if rule.name == "volcengine_uuid_api_key" and lowered in KNOWN_EXAMPLE_UUIDS:
        return True

    compact = re.sub(rb"[^a-z0-9]", b"", lowered)
    if any(marker in compact for marker in PLACEHOLDER_MARKERS):
        return True

    # Repeated zero/x/a-style values are common documentation placeholders and
    # vanishingly unlikely credentials. Prefix labels are removed first.
    payload = re.sub(
        rb"^(?:akia|asia|gh[pousr]|githubpat|skproj|sksvcacct|sk)",
        b"",
        compact,
    )
    return len(payload) >= 16 and len(set(payload)) <= 3


def _text_bytes(data: bytes) -> bytes | None:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16").encode()
        except UnicodeError:
            return None
    if b"\0" in data[:BINARY_SAMPLE_BYTES]:
        return None
    return data


def _matching_rules(data: bytes) -> tuple[str, ...]:
    text = _text_bytes(data)
    if text is None:
        return ()

    matches = set()
    for rule in SECRET_RULES:
        if any(
            not _is_placeholder(rule, match.group())
            for match in rule.pattern.finditer(text)
        ):
            matches.add(rule.name)
    return tuple(sorted(matches))


class RepositoryScanner:
    def __init__(
        self,
        repository: Path,
        *,
        max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        self.repository = repository.resolve()
        self.max_blob_bytes = max_blob_bytes
        self.max_total_bytes = max_total_bytes
        self.total_bytes = 0
        self.rule_cache: dict[str, tuple[str, ...]] = {}
        self.findings: list[ScanIssue] = []
        self.problems: list[ScanIssue] = []
        self._finding_keys: set[tuple[str, str, str]] = set()
        self._problem_keys: set[tuple[str, str, str]] = set()
        self.budget_exhausted = False

    def scan(self, *, current: bool = True, history: bool = True) -> ScanReport:
        _run_git(self.repository, ["rev-parse", "--git-dir"])
        if current:
            self._scan_index()
            self._scan_worktree()
        if history and not self.budget_exhausted:
            self._scan_history()
        return ScanReport(self.findings, self.problems)

    def _add_finding(self, issue: ScanIssue) -> None:
        key = (issue.blob, issue.path, issue.rule)
        if key not in self._finding_keys:
            self._finding_keys.add(key)
            self.findings.append(issue)

    def _add_problem(self, issue: ScanIssue) -> None:
        key = (issue.blob, issue.path, issue.rule)
        if key not in self._problem_keys:
            self._problem_keys.add(key)
            self.problems.append(issue)

    def _record_rules(
        self,
        *,
        data: bytes,
        blob: str,
        path: str,
        source: str,
    ) -> None:
        if self.budget_exhausted:
            return
        rules = self.rule_cache.get(blob)
        if rules is None:
            if self.total_bytes + len(data) > self.max_total_bytes:
                self.budget_exhausted = True
                self._add_problem(
                    ScanIssue(blob, path, "scan_total_budget_exceeded", source)
                )
                return
            self.total_bytes += len(data)
            rules = _matching_rules(data)
            self.rule_cache[blob] = rules
        for rule in rules:
            self._add_finding(ScanIssue(blob, path, rule, source))

    def _record_oversized(
        self,
        *,
        prefix: bytes,
        blob: str,
        path: str,
        source: str,
    ) -> None:
        if _text_bytes(prefix) is not None:
            self._add_problem(ScanIssue(blob, path, "unscanned_large_text", source))

    def _scan_index(self) -> None:
        entries = _run_git(self.repository, ["ls-files", "--stage", "-z"])
        for entry in entries.split(b"\0"):
            if not entry or self.budget_exhausted:
                continue
            metadata, separator, raw_path = entry.partition(b"\t")
            fields = metadata.split()
            if not separator or len(fields) != 3 or fields[0] == b"160000":
                continue
            blob = fields[1].decode("ascii")
            path = os.fsdecode(raw_path)
            self._scan_git_blob(blob=blob, path=path, source="index")

    def _scan_worktree(self) -> None:
        entries = _run_git(
            self.repository,
            ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        )
        for raw_path in entries.split(b"\0"):
            if not raw_path or self.budget_exhausted:
                continue
            path = os.fsdecode(raw_path)
            absolute_path = self.repository / path
            try:
                file_stat = absolute_path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            try:
                with absolute_path.open("rb") as handle:
                    data = handle.read(self.max_blob_bytes + 1)
            except OSError:
                self._add_problem(
                    ScanIssue("WORKTREE", path, "worktree_file_unreadable", "worktree")
                )
                continue
            if len(data) > self.max_blob_bytes:
                self._record_oversized(
                    prefix=data[:BINARY_SAMPLE_BYTES],
                    blob="WORKTREE",
                    path=path,
                    source="worktree",
                )
                continue
            blob = _run_git(
                self.repository, ["hash-object", "--stdin"], input_data=data
            )
            self._record_rules(
                data=data,
                blob=blob.decode("ascii").strip(),
                path=path,
                source="worktree",
            )

    def _scan_history(self) -> None:
        raw_objects = _run_git(self.repository, ["rev-list", "--objects", "--all"])
        paths: dict[str, str] = {}
        object_ids: list[str] = []
        for line in raw_objects.splitlines():
            raw_object, separator, raw_path = line.partition(b" ")
            object_id = raw_object.decode("ascii")
            object_ids.append(object_id)
            if separator:
                paths.setdefault(object_id, os.fsdecode(raw_path))

        metadata = _run_git(
            self.repository,
            ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            input_data=("\n".join(object_ids) + "\n").encode(),
        )
        for line in metadata.splitlines():
            if self.budget_exhausted:
                break
            fields = line.split()
            if len(fields) != 3 or fields[1] != b"blob":
                continue
            blob = fields[0].decode("ascii")
            size = int(fields[2])
            self._scan_git_blob(
                blob=blob,
                path=paths.get(blob, "<unknown>"),
                source="history",
                known_size=size,
            )

    def _scan_git_blob(
        self,
        *,
        blob: str,
        path: str,
        source: str,
        known_size: int | None = None,
    ) -> None:
        if blob in self.rule_cache:
            for rule in self.rule_cache[blob]:
                self._add_finding(ScanIssue(blob, path, rule, source))
            return

        size = known_size
        if size is None:
            raw_size = _run_git(self.repository, ["cat-file", "-s", blob])
            size = int(raw_size)
        if size > self.max_blob_bytes:
            prefix = self._git_blob_prefix(blob)
            self._record_oversized(prefix=prefix, blob=blob, path=path, source=source)
            return

        data = _run_git(self.repository, ["cat-file", "blob", blob])
        self._record_rules(data=data, blob=blob, path=path, source=source)

    def _git_blob_prefix(self, blob: str) -> bytes:
        process = subprocess.Popen(
            ["git", "cat-file", "blob", blob],
            cwd=self.repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdout is not None
        try:
            return process.stdout.read(BINARY_SAMPLE_BYTES)
        finally:
            process.stdout.close()
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan repository content without printing matched secret values."
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--no-current", action="store_true")
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument(
        "--max-blob-bytes",
        type=_positive_integer,
        default=DEFAULT_MAX_BLOB_BYTES,
    )
    parser.add_argument(
        "--max-total-bytes",
        type=_positive_integer,
        default=DEFAULT_MAX_TOTAL_BYTES,
    )
    return parser


def main(arguments: list[str] | None = None) -> int:
    options = _build_parser().parse_args(arguments)
    try:
        report = RepositoryScanner(
            options.repository,
            max_blob_bytes=options.max_blob_bytes,
            max_total_bytes=options.max_total_bytes,
        ).scan(current=not options.no_current, history=not options.no_history)
    except (OSError, ScanFailure, ValueError):
        print(
            "Repository secret scan could not inspect Git metadata safely.",
            file=sys.stderr,
        )
        return 2

    for issue in [*report.findings, *report.problems]:
        print(json.dumps(issue.as_dict(), sort_keys=True))

    if report.problems:
        print(
            "Repository secret scan was incomplete and failed closed.", file=sys.stderr
        )
        return 2
    if report.findings:
        print(
            f"Repository secret scan found {len(report.findings)} location(s).",
            file=sys.stderr,
        )
        return 1
    print("Repository secret scan passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
