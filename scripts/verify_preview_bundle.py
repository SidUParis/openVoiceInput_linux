#!/usr/bin/env python3
"""Verify the offline preview archive without touching a real desktop session."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Sequence

MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
SHA256_LINE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")


class VerificationError(RuntimeError):
    """The preview artifact violated a release invariant."""


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_path(path: Path) -> str:
    with path.open("rb") as stream:
        return _sha256_stream(stream)


def extract_archive(archive: Path, destination: Path) -> Path:
    """Safely extract one regular-file/directory-only bundle root."""

    with tarfile.open(archive, mode="r:gz") as source:
        members = source.getmembers()
        if not members:
            raise VerificationError("preview archive is empty")
        if len(members) > MAX_ARCHIVE_FILES:
            raise VerificationError("preview archive contains too many entries")
        total_size = sum(member.size for member in members if member.isfile())
        if total_size > MAX_ARCHIVE_BYTES:
            raise VerificationError("preview archive expands beyond the safety limit")

        roots: set[str] = set()
        names: set[str] = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise VerificationError(f"unsafe archive path: {member.name!r}")
            if member.name in names:
                raise VerificationError(f"duplicate archive path: {member.name!r}")
            names.add(member.name)
            roots.add(path.parts[0])
            if not (member.isfile() or member.isdir()):
                raise VerificationError(
                    f"unsupported link or special entry: {member.name!r}"
                )
        if len(roots) != 1:
            raise VerificationError("preview archive must have one top-level directory")

        # Every member was checked above; no link can redirect extraction.
        source.extractall(destination)  # noqa: S202

    root = destination / next(iter(roots))
    if not root.is_dir() or root.is_symlink():
        raise VerificationError("preview root is not a regular directory")
    return root


def verify_manifest(root: Path) -> None:
    """Require a complete, exact SHA256 manifest for every payload file."""

    manifest = root / "SHA256SUMS"
    if not manifest.is_file() or manifest.is_symlink():
        raise VerificationError("SHA256SUMS is missing or unsafe")
    if manifest.stat().st_size > 4 * 1024 * 1024:
        raise VerificationError("SHA256SUMS is unexpectedly large")

    declared: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = SHA256_LINE.fullmatch(line)
        if match is None:
            raise VerificationError(f"invalid SHA256SUMS line: {line!r}")
        expected, relative_text = match.groups()
        relative = PurePosixPath(relative_text)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or relative_text == "SHA256SUMS"
        ):
            raise VerificationError(f"unsafe manifest path: {relative_text!r}")
        if relative_text in declared:
            raise VerificationError(f"duplicate manifest path: {relative_text!r}")
        declared[relative_text] = expected

    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    if set(declared) != actual:
        missing = sorted(actual - set(declared))
        unexpected = sorted(set(declared) - actual)
        raise VerificationError(
            f"manifest file set differs: missing={missing}, unexpected={unexpected}"
        )
    for relative, expected in declared.items():
        path = root / relative
        if path.is_symlink() or _sha256_path(path) != expected:
            raise VerificationError(f"SHA256 mismatch: {relative}")


def verify_bundle_shape(root: Path) -> Path:
    """Reject local state and return the sole project wheel."""

    required = (
        "BUNDLE-INFO",
        "LICENSE",
        "README.md",
        "packaging/open-voice-input-settings",
        "scripts/install-user.sh",
        "scripts/uninstall-user.sh",
        "voice/pyproject.toml",
    )
    for relative in required:
        if not (root / relative).is_file():
            raise VerificationError(f"required bundle file is missing: {relative}")

    wheelhouse = root / "wheelhouse"
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise VerificationError("wheelhouse is missing or unsafe")
    wheels = sorted(path for path in wheelhouse.iterdir() if path.is_file())
    if not wheels or any(path.suffix != ".whl" for path in wheels):
        raise VerificationError("wheelhouse must contain only wheel files")
    project_wheels = [
        path for path in wheels if path.name.startswith("murmur_ime_voice-")
    ]
    if len(project_wheels) != 1:
        raise VerificationError("wheelhouse must contain exactly one project wheel")

    forbidden_names = {
        ".env",
        "credentials.json",
        "voice.json",
        "vocabulary.json",
        "volcengine.json",
    }
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts or "__pycache__" in relative.parts:
            raise VerificationError(f"repository/cache state leaked: {relative}")
        if not path.is_file():
            continue
        if path.name in forbidden_names or path.suffix in {
            ".key",
            ".pyc",
            ".pyo",
            ".token",
        }:
            raise VerificationError(f"local configuration leaked: {relative}")
    return project_wheels[0]


def _git_archive_files(repository: Path, source_ref: str) -> dict[str, str]:
    command = ["git", "-C", str(repository), "archive", "--format=tar", source_ref]
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise VerificationError(
            f"cannot read source revision: {result.stderr.decode(errors='replace')}"
        )
    expected: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise VerificationError(
                    f"source revision contains a non-file entry: {member.name}"
                )
            stream = archive.extractfile(member)
            if stream is None:
                raise VerificationError(f"cannot read source entry: {member.name}")
            expected[member.name] = _sha256_stream(stream)
    return expected


def verify_clean_git_source(root: Path, repository: Path, source_ref: str) -> None:
    """Prove that the source portion exactly equals ``git archive REF``."""

    expected = _git_archive_files(repository, source_ref)
    actual = {
        path.relative_to(root).as_posix(): _sha256_path(path)
        for path in root.rglob("*")
        if path.is_file()
        and path.name != "SHA256SUMS"
        and path.name != "BUNDLE-INFO"
        and "wheelhouse" not in path.relative_to(root).parts
    }
    if expected != actual:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        changed = sorted(
            path
            for path in set(expected) & set(actual)
            if expected[path] != actual[path]
        )
        raise VerificationError(
            "bundle source is not the clean Git archive: "
            f"missing={missing}, unexpected={unexpected}, changed={changed}"
        )

    commit = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            f"{source_ref}^{{commit}}",
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    info = dict(
        line.split("=", 1)
        for line in (root / "BUNDLE-INFO").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    if info.get("source_commit") != commit:
        raise VerificationError("BUNDLE-INFO source commit does not match Git")


def verify_offline_wheel_install(root: Path, project_wheel: Path) -> None:
    """Install every Python runtime dependency with package indexes disabled."""

    wheelhouse = root / "wheelhouse"
    with tempfile.TemporaryDirectory() as temporary:
        environment = os.environ.copy()
        environment.update(
            {
                "PIP_CONFIG_FILE": os.devnull,
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INDEX": "1",
                "PYTHONNOUSERSITE": "1",
            }
        )
        venv = Path(temporary) / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
            check=True,
        )
        python = venv / "bin/python"
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-index",
                "--no-cache-dir",
                "--find-links",
                str(wheelhouse),
                str(project_wheel),
            ],
            check=True,
            env=environment,
        )
        subprocess.run([str(python), "-m", "pip", "check"], check=True, env=environment)
        subprocess.run(
            [
                str(python),
                "-c",
                "import gi, murmur_voice, sounddevice, websockets; "
                "gi.require_version('Gtk', '4.0'); "
                "from murmur_voice import settings_app",
            ],
            check=True,
            env=environment,
        )
        subprocess.run(
            [str(venv / "bin/murmur-voice-daemon"), "--help"],
            check=True,
            env=environment,
            stdout=subprocess.DEVNULL,
        )


def verify_mock_installer_lifecycle(root: Path) -> None:
    """Exercise default and explicit wheelhouse paths with desktop mocks."""

    repository = Path(__file__).resolve().parents[1]
    if str(repository) not in sys.path:
        sys.path.insert(0, str(repository))
    from scripts.tests.test_user_install import InstallerHarness

    installer = root / "scripts/install-user.sh"
    wheelhouse = (root / "wheelhouse").resolve()
    for arguments in ((), ("--wheelhouse", str(wheelhouse))):
        harness = InstallerHarness(repository=root)
        try:
            result = harness.run(installer, *arguments)
            if result.returncode != 0:
                raise VerificationError(
                    "mock preview install failed: "
                    f"stdout={result.stdout!r}, stderr={result.stderr!r}"
                )
            pip_calls = [
                line
                for line in harness.calls()
                if line.startswith("venv-python -m pip")
            ]
            if len(pip_calls) != 1:
                raise VerificationError("mock install did not invoke pip exactly once")
            pip_call = pip_calls[0]
            if (
                "--no-index" not in pip_call
                or f"--find-links {wheelhouse}" not in pip_call
            ):
                raise VerificationError("mock install was not locked to the wheelhouse")
        finally:
            harness.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--source-repository",
        type=Path,
        help="also prove that source files exactly match git archive",
    )
    parser.add_argument("--source-ref", default="HEAD")
    parser.add_argument(
        "--skip-wheel-install",
        action="store_true",
        help="skip only the real offline pip smoke test (unit tests only)",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(arguments)
    archive = options.archive.resolve()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = extract_archive(archive, Path(temporary))
            verify_manifest(root)
            project_wheel = verify_bundle_shape(root)
            if options.source_repository is not None:
                verify_clean_git_source(
                    root,
                    options.source_repository.resolve(),
                    options.source_ref,
                )
            if not options.skip_wheel_install:
                verify_offline_wheel_install(root, project_wheel)
            verify_mock_installer_lifecycle(root)
    except (
        OSError,
        subprocess.CalledProcessError,
        tarfile.TarError,
        VerificationError,
    ) as error:
        print(f"preview verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified offline preview bundle: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
