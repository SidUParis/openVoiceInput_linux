#!/usr/bin/env python3
"""Verify the offline preview archive without touching a real desktop session."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
import platform
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Sequence

try:
    from scripts.generate_preview_sbom import (
        SBOM_FILENAME,
        SBOMError,
        WheelComponent,
        build_sbom,
        read_wheelhouse,
        render_sbom,
    )
except ModuleNotFoundError:  # Direct execution as scripts/verify_preview_bundle.py.
    script_import_dir = str(Path(__file__).resolve().parent)
    if script_import_dir not in sys.path:
        sys.path.insert(0, script_import_dir)
    from generate_preview_sbom import (  # type: ignore[no-redef]
        SBOM_FILENAME,
        SBOMError,
        WheelComponent,
        build_sbom,
        read_wheelhouse,
        render_sbom,
    )

MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_SBOM_BYTES = 4 * 1024 * 1024
MAX_LOCK_BYTES = 1024 * 1024
SHA256_LINE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")
LOCK_REQUIREMENT_LINE = re.compile(
    r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==([^\\\s]+) \\$"
)
LOCK_HASH_LINE = re.compile(r"^    --hash=sha256:([0-9a-f]{64})$")
RUNTIME_LOCK = Path("packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt")
RUNTIME_LOCK_TARGET = "ubuntu-24.04-x86_64-py3.12"


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
            canonical_name = path.as_posix()
            if (
                path.is_absolute()
                or not path.parts
                or ".." in path.parts
                or canonical_name != member.name
            ):
                raise VerificationError(f"unsafe archive path: {member.name!r}")
            if canonical_name in names:
                raise VerificationError(f"duplicate archive path: {member.name!r}")
            names.add(canonical_name)
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


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise VerificationError(f"SBOM contains a duplicate JSON key: {name!r}")
        result[name] = value
    return result


def verify_sbom(root: Path) -> None:
    """Recompute and exactly match the target wheelhouse CycloneDX document."""

    path = root / SBOM_FILENAME
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"{SBOM_FILENAME} is missing or unsafe")
    if path.stat().st_size > MAX_SBOM_BYTES:
        raise VerificationError(f"{SBOM_FILENAME} is unexpectedly large")
    payload = path.read_bytes()
    try:
        parsed = json.loads(payload, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError(f"{SBOM_FILENAME} is not valid UTF-8 JSON") from error
    if not isinstance(parsed, dict):
        raise VerificationError(f"{SBOM_FILENAME} must be a JSON object")
    try:
        expected = build_sbom(root)
    except (OSError, UnicodeError, SBOMError) as error:
        raise VerificationError(
            f"wheelhouse SBOM inventory is invalid: {error}"
        ) from error
    if parsed != expected:
        raise VerificationError(
            "SBOM does not exactly match wheel metadata, hashes, target, or dependencies"
        )
    if payload != render_sbom(expected):
        raise VerificationError("SBOM is not in the deterministic canonical JSON form")


def _normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_runtime_lock(root: Path) -> dict[str, tuple[str, str]]:
    """Read the fixed preview runtime lock without invoking pip."""

    path = root / RUNTIME_LOCK
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"runtime lock is missing or unsafe: {RUNTIME_LOCK}")
    if path.stat().st_size > MAX_LOCK_BYTES:
        raise VerificationError("runtime lock is unexpectedly large")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise VerificationError("runtime lock is not valid UTF-8") from error

    locked: dict[str, tuple[str, str]] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line or line.startswith("#"):
            continue
        requirement = LOCK_REQUIREMENT_LINE.fullmatch(line)
        if requirement is None or index >= len(lines):
            raise VerificationError(f"invalid runtime lock entry at line {index}")
        digest = LOCK_HASH_LINE.fullmatch(lines[index])
        index += 1
        if digest is None:
            raise VerificationError(f"missing runtime wheel hash at line {index}")
        name = _normalize_distribution_name(requirement.group(1))
        if name in locked:
            raise VerificationError(f"duplicate runtime lock entry: {name}")
        locked[name] = (requirement.group(2), digest.group(1))
    if not locked:
        raise VerificationError("runtime lock is empty")
    return locked


def verify_runtime_lock(root: Path) -> None:
    """Require the dependency wheels to exactly match the target runtime lock."""

    try:
        target, components, project_name = read_wheelhouse(root)
    except (OSError, UnicodeError, SBOMError) as error:
        raise VerificationError(f"wheelhouse inventory is invalid: {error}") from error
    if target.target != RUNTIME_LOCK_TARGET:
        raise VerificationError(
            f"runtime lock target must be {RUNTIME_LOCK_TARGET}, got {target.target}"
        )
    observed = {
        name: (component.version, component.sha256)
        for name, component in components.items()
        if name != project_name
    }
    locked = read_runtime_lock(root)
    if observed != locked:
        raise VerificationError(
            "runtime wheelhouse does not exactly match the version/hash lock: "
            f"locked={locked!r}, observed={observed!r}"
        )


def verify_project_wheel_source(root: Path, project_wheel: Path | None = None) -> None:
    """Bind every executable project-wheel member to the verified source tree."""

    try:
        _, components, project_name = read_wheelhouse(root)
    except (OSError, UnicodeError, SBOMError) as error:
        raise VerificationError(f"wheelhouse inventory is invalid: {error}") from error
    component = components[project_name]
    expected_wheel = root / "wheelhouse" / component.filename
    if project_wheel is None:
        project_wheel = expected_wheel
    if project_wheel.resolve() != expected_wheel.resolve():
        raise VerificationError("project wheel path disagrees with the wheelhouse")

    voice_root = root / "voice"
    package_root = voice_root / "murmur_voice"
    pyproject_path = voice_root / "pyproject.toml"
    for path, kind in (
        (voice_root, "voice source"),
        (package_root, "project package source"),
    ):
        if not path.is_dir() or path.is_symlink():
            raise VerificationError(f"{kind} is missing or unsafe")
    if not pyproject_path.is_file() or pyproject_path.is_symlink():
        raise VerificationError("voice pyproject is missing or unsafe")

    expected_package: dict[str, bytes] = {}
    for path in sorted(package_root.rglob("*")):
        if path.is_symlink():
            raise VerificationError("project package source contains a symbolic link")
        if path.is_file():
            relative = path.relative_to(voice_root).as_posix()
            expected_package[relative] = path.read_bytes()
    if not expected_package:
        raise VerificationError("project package source is empty")

    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = pyproject["project"]
        expected_scripts = project["scripts"]
    except (
        OSError,
        UnicodeError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
    ) as error:
        raise VerificationError("voice pyproject metadata is invalid") from error
    if not isinstance(project, dict) or not isinstance(expected_scripts, dict):
        raise VerificationError("voice pyproject metadata is invalid")
    if any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in expected_scripts.items()
    ):
        raise VerificationError("voice console scripts are invalid")

    try:
        with zipfile.ZipFile(project_wheel) as archive:
            members = {
                info.filename: info for info in archive.infolist() if not info.is_dir()
            }
            metadata_names = [
                name
                for name in members
                if PurePosixPath(name).name == "METADATA"
                and PurePosixPath(name).parent.name.endswith(".dist-info")
            ]
            if len(metadata_names) != 1:
                raise VerificationError(
                    "project wheel has ambiguous dist-info metadata"
                )
            dist_info = PurePosixPath(metadata_names[0]).parent.as_posix()
            actual_package = {
                name: archive.read(info)
                for name, info in members.items()
                if PurePosixPath(name).parts[0] == "murmur_voice"
            }
            if actual_package != expected_package:
                raise VerificationError(
                    "project wheel package bytes do not match the verified source"
                )
            allowed_dist_info = {
                f"{dist_info}/METADATA",
                f"{dist_info}/WHEEL",
                f"{dist_info}/entry_points.txt",
                f"{dist_info}/top_level.txt",
                f"{dist_info}/RECORD",
                f"{dist_info}/licenses/LICENSE",
                f"{dist_info}/licenses/NOTICE.md",
            }
            if set(members) != set(expected_package) | allowed_dist_info:
                raise VerificationError(
                    "project wheel contains files not derived from the verified project"
                )
            if (
                archive.read(f"{dist_info}/licenses/LICENSE")
                != (voice_root / "LICENSE").read_bytes()
                or archive.read(f"{dist_info}/licenses/NOTICE.md")
                != (voice_root / "NOTICE.md").read_bytes()
            ):
                raise VerificationError("project wheel licences do not match source")
            if archive.read(f"{dist_info}/top_level.txt") != b"murmur_voice\n":
                raise VerificationError("project wheel top-level package is invalid")

            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read_string(
                archive.read(f"{dist_info}/entry_points.txt").decode("utf-8")
            )
            if (
                parser.sections() != ["console_scripts"]
                or dict(parser.items("console_scripts")) != expected_scripts
            ):
                raise VerificationError(
                    "project wheel entry points do not match the verified pyproject"
                )

            wheel_text = archive.read(f"{dist_info}/WHEEL").decode("utf-8")
            wheel_fields = dict(
                line.split(": ", 1) for line in wheel_text.splitlines() if ": " in line
            )
            if (
                wheel_fields.get("Wheel-Version") != "1.0"
                or wheel_fields.get("Root-Is-Purelib") != "true"
                or wheel_fields.get("Tag") != "py3-none-any"
            ):
                raise VerificationError(
                    "project wheel compatibility metadata is invalid"
                )
    except (OSError, UnicodeError, zipfile.BadZipFile, configparser.Error) as error:
        raise VerificationError("project wheel source binding is invalid") from error


def verify_host_target(
    root: Path,
    *,
    implementation: str | None = None,
    python_version: str | None = None,
    machine: str | None = None,
    pointer_bits: int | None = None,
    os_id: str | None = None,
    os_version: str | None = None,
) -> None:
    """Fail closed unless the verifier host matches the fixed preview target."""

    try:
        target, _, _ = read_wheelhouse(root)
    except (OSError, UnicodeError, SBOMError) as error:
        raise VerificationError(f"wheelhouse inventory is invalid: {error}") from error
    if os_id is None or os_version is None:
        try:
            release = platform.freedesktop_os_release()
        except OSError as error:
            raise VerificationError(
                "host operating-system identity is unavailable"
            ) from error
        os_id = release.get("ID", "")
        os_version = release.get("VERSION_ID", "")
    implementation = implementation or sys.implementation.name
    python_version = python_version or (
        f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    machine = machine or platform.machine()
    pointer_bits = pointer_bits or struct.calcsize("P") * 8
    if (
        implementation != "cpython"
        or python_version != target.python_version
        or machine != target.machine
        or pointer_bits != 64
        or os_id.lower() != "ubuntu"
        or os_version != target.ubuntu_version
    ):
        raise VerificationError(
            "host does not match the bundle's Ubuntu, architecture, or CPython target"
        )


def verify_install_wheelhouse(root: Path, wheelhouse: Path) -> list[Path]:
    """Return the exact install set after matching it to the lock and SBOM."""

    verify_host_target(root)
    verify_sbom(root)
    verify_runtime_lock(root)
    verify_project_wheel_source(root)
    try:
        _, components, _ = read_wheelhouse(root)
    except (OSError, UnicodeError, SBOMError) as error:
        raise VerificationError(f"wheelhouse inventory is invalid: {error}") from error
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise VerificationError("install wheelhouse is missing or unsafe")
    expected = {
        component.filename: component.sha256 for component in components.values()
    }
    observed: dict[str, str] = {}
    for path in sorted(wheelhouse.iterdir(), key=lambda candidate: candidate.name):
        if not path.is_file() or path.is_symlink() or path.suffix != ".whl":
            raise VerificationError(f"unexpected install wheelhouse entry: {path.name}")
        observed[path.name] = _sha256_path(path)
    if observed != expected:
        raise VerificationError(
            "install wheelhouse does not exactly match the lock/SBOM file set and hashes: "
            f"expected={expected!r}, observed={observed!r}"
        )
    return [wheelhouse / filename for filename in sorted(expected)]


def verify_bundle_shape(root: Path) -> Path:
    """Reject local state and return the sole project wheel."""

    required = (
        "BUNDLE-INFO",
        "LICENSE",
        "README.md",
        SBOM_FILENAME,
        "packaging/desktop/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop.in",
        "packaging/icons/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg",
        "packaging/open-voice-input-settings",
        str(RUNTIME_LOCK),
        "scripts/generate_preview_sbom.py",
        "scripts/render_desktop_entry.py",
        "scripts/install-user.sh",
        "scripts/uninstall-user.sh",
        "scripts/verify_preview_bundle.py",
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
        "corrections.json",
        "adaptive-corrections.json",
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
        and path.name != SBOM_FILENAME
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


def _offline_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("PIP_") or name in {"PYTHONHOME", "PYTHONPATH"}:
            environment.pop(name)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        }
    )
    return environment


def install_explicit_wheels(
    python: Path,
    wheels: Sequence[Path],
    environment: dict[str, str],
) -> None:
    """Install each selected wheel locally, ignoring visible host distributions."""

    subprocess.run(
        [
            str(python),
            "-I",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-index",
            "--no-cache-dir",
            "--ignore-installed",
            "--no-deps",
            *(str(wheel.resolve()) for wheel in wheels),
        ],
        check=True,
        env=environment,
    )


_INSTALLED_INVENTORY = r"""
import importlib.metadata
import importlib.util
import json
import pathlib
import re
import sys
import sysconfig

normalize = lambda value: re.sub(r"[-_.]+", "-", value).lower()
wanted = set(json.loads(sys.argv[1]))
module_names = json.loads(sys.argv[2])
site_roots = sorted(
    {
        str(pathlib.Path(sysconfig.get_path(kind)).resolve())
        for kind in ("purelib", "platlib")
    }
)
records = {name: [] for name in wanted}
for root in site_roots:
    for distribution in importlib.metadata.distributions(path=[root]):
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        name = normalize(raw_name)
        if name not in wanted:
            continue
        direct_url_text = distribution.read_text("direct_url.json")
        records[name].append(
            {
                "version": distribution.version,
                "location": str(pathlib.Path(distribution.locate_file("")).resolve()),
                "files": sorted(
                    str(pathlib.Path(distribution.locate_file(path)).resolve())
                    for path in (distribution.files or ())
                ),
                "direct_url": (
                    json.loads(direct_url_text) if direct_url_text is not None else None
                ),
            }
        )
for name, module_name in module_names.items():
    spec = importlib.util.find_spec(module_name)
    origin = None if spec is None else spec.origin
    for record in records[name]:
        record["module_origin"] = origin
print(json.dumps({"site_roots": site_roots, "records": records}, sort_keys=True))
"""


def _path_is_within(path_text: str, roots: Sequence[Path]) -> bool:
    path = Path(path_text).resolve()
    return any(path == root or root in path.parents for root in roots)


def _component_wheel_path(
    root: Path,
    component: WheelComponent,
    wheelhouse: Path | None = None,
) -> Path:
    if wheelhouse is None:
        wheelhouse = root / "wheelhouse"
    wheelhouse = wheelhouse.resolve()
    path = (wheelhouse / component.filename).resolve()
    if path.parent != wheelhouse or not path.is_file() or path.is_symlink():
        raise VerificationError(
            f"component wheel is missing or unsafe: {component.filename}"
        )
    return path


def verify_installed_wheel_sources(
    root: Path,
    venv: Path,
    components: dict[str, WheelComponent],
    environment: dict[str, str],
    *,
    module_names: dict[str, str] | None = None,
    wheelhouse: Path | None = None,
) -> dict[str, dict[str, object]]:
    """Prove installed distributions came from the expected wheels in this venv."""

    python = venv / "bin/python"
    if module_names is None:
        module_names = {
            "murmur-ime-voice": "murmur_voice",
            "sounddevice": "sounddevice",
            "websockets": "websockets",
            "cffi": "cffi",
            "pycparser": "pycparser",
        }
    expected_names = sorted(components)
    if set(module_names) != set(expected_names):
        raise VerificationError(
            f"unexpected preview distribution set: {expected_names!r}"
        )
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            _INSTALLED_INVENTORY,
            json.dumps(expected_names),
            json.dumps(module_names, sort_keys=True),
        ],
        check=True,
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        inventory = json.loads(result.stdout)
        roots = [Path(value).resolve() for value in inventory["site_roots"]]
        records = inventory["records"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise VerificationError(
            "installed distribution inventory is invalid"
        ) from error
    if not roots or not isinstance(records, dict):
        raise VerificationError("installed distribution inventory is incomplete")
    venv_root = venv.resolve()
    if any(root == venv_root or venv_root not in root.parents for root in roots):
        raise VerificationError("reported site-packages is outside the staged venv")

    verified: dict[str, dict[str, object]] = {}
    file_owners: dict[Path, set[str]] = {}
    for name, component in components.items():
        matches = records.get(name)
        if not isinstance(matches, list) or len(matches) != 1:
            raise VerificationError(
                f"expected one venv-local installed distribution for {name!r}"
            )
        record = matches[0]
        if not isinstance(record, dict) or record.get("version") != component.version:
            raise VerificationError(
                f"installed version does not match the SBOM for {name!r}"
            )
        location = record.get("location")
        module_origin = record.get("module_origin")
        installed_files = record.get("files")
        if not isinstance(location, str) or not _path_is_within(location, roots):
            raise VerificationError(
                f"installed distribution is outside the venv site-packages: {name!r}"
            )
        if not isinstance(module_origin, str) or not _path_is_within(
            module_origin, roots
        ):
            raise VerificationError(
                f"import does not resolve to the venv-local wheel: {name!r}"
            )
        if not isinstance(installed_files, list) or not all(
            isinstance(path, str) for path in installed_files
        ):
            raise VerificationError(f"installed RECORD is unavailable for {name!r}")
        resolved_files = {Path(path).resolve() for path in installed_files}
        if not resolved_files or any(
            path == venv_root or venv_root not in path.parents
            for path in resolved_files
        ):
            raise VerificationError(
                f"installed RECORD escapes the staged venv for {name!r}"
            )
        if Path(module_origin).resolve() not in resolved_files:
            raise VerificationError(
                f"imported module is not owned by its installed wheel: {name!r}"
            )
        for path in resolved_files:
            file_owners.setdefault(path, set()).add(name)

        direct_url = record.get("direct_url")
        wheel = _component_wheel_path(root, component, wheelhouse)
        if not isinstance(direct_url, dict) or direct_url.get("url") != wheel.as_uri():
            raise VerificationError(
                f"installed distribution source is not its wheelhouse wheel: {name!r}"
            )
        archive_info = direct_url.get("archive_info")
        if not isinstance(archive_info, dict):
            raise VerificationError(f"installed wheel hash is missing for {name!r}")
        hashes = archive_info.get("hashes")
        sha256 = hashes.get("sha256") if isinstance(hashes, dict) else None
        legacy_hash = archive_info.get("hash")
        if sha256 is None and isinstance(legacy_hash, str):
            algorithm, separator, value = legacy_hash.partition("=")
            if separator and algorithm == "sha256":
                sha256 = value
        if sha256 != component.sha256:
            raise VerificationError(
                f"installed wheel hash does not match the SBOM for {name!r}"
            )
        verified[name] = record
    collisions = {
        str(path): sorted(owners)
        for path, owners in file_owners.items()
        if len(owners) > 1
    }
    if collisions:
        raise VerificationError(
            f"installed wheels claim overlapping files: {collisions!r}"
        )
    return verified


def verify_offline_wheel_install(root: Path, project_wheel: Path) -> None:
    """Install and prove every locked wheel with package indexes disabled."""

    try:
        _, components, project_name = read_wheelhouse(root)
    except (OSError, UnicodeError, SBOMError) as error:
        raise VerificationError(f"wheelhouse inventory is invalid: {error}") from error
    expected_project = _component_wheel_path(root, components[project_name])
    if project_wheel.resolve() != expected_project:
        raise VerificationError("project wheel disagrees with the wheelhouse inventory")
    wheels = [
        _component_wheel_path(root, component)
        for component in sorted(components.values(), key=lambda item: item.filename)
    ]
    environment = _offline_environment()
    with tempfile.TemporaryDirectory() as temporary:
        venv = Path(temporary) / "venv"
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "venv",
                "--system-site-packages",
                str(venv),
            ],
            check=True,
            env=environment,
        )
        python = venv / "bin/python"
        install_explicit_wheels(python, wheels, environment)
        subprocess.run(
            [str(python), "-I", "-m", "pip", "check"],
            check=True,
            env=environment,
        )
        verify_installed_wheel_sources(root, venv, components, environment)
        subprocess.run(
            [
                str(python),
                "-I",
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
            cwd=venv,
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
    expected_wheel_names = sorted(
        path.name for path in wheelhouse.iterdir() if path.is_file()
    )
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
                if line.startswith("venv-python -I -m pip")
            ]
            if len(pip_calls) != 1:
                raise VerificationError("mock install did not invoke pip exactly once")
            pip_call = pip_calls[0]
            if (
                "--no-index" not in pip_call
                or "--isolated" not in pip_call
                or "--find-links" not in pip_call
                or "/install-wheelhouse" not in pip_call
                or "--ignore-installed" not in pip_call
                or "--no-deps" not in pip_call
                or any(
                    f"/install-wheelhouse/{name}" not in pip_call
                    for name in expected_wheel_names
                )
            ):
                raise VerificationError(
                    "mock install did not explicitly install every wheelhouse wheel"
                )
            bundle_checks = [
                line for line in harness.calls() if line.startswith("bundle-verify ")
            ]
            if (
                len(bundle_checks) != 3
                or "--check-installed-venv" not in bundle_checks[2]
            ):
                raise VerificationError(
                    "mock install did not verify staged wheels and installed provenance"
                )
        finally:
            harness.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="?", type=Path)
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
    parser.add_argument(
        "--check-install-wheelhouse",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--check-installed-venv", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--bundle-root", type=Path, help=argparse.SUPPRESS)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(arguments)
    if options.check_install_wheelhouse is not None:
        if (
            options.archive is not None
            or options.bundle_root is None
            or options.source_repository is not None
            or options.source_ref != "HEAD"
            or options.skip_wheel_install
        ):
            parser.error(
                "--check-install-wheelhouse requires only --bundle-root and a wheelhouse"
            )
        try:
            root = options.bundle_root.resolve()
            wheelhouse = options.check_install_wheelhouse.resolve()
            verify_install_wheelhouse(root, wheelhouse)
            if options.check_installed_venv is not None:
                try:
                    _, components, _ = read_wheelhouse(root)
                except (OSError, UnicodeError, SBOMError) as error:
                    raise VerificationError(
                        f"wheelhouse inventory is invalid: {error}"
                    ) from error
                verify_installed_wheel_sources(
                    root,
                    options.check_installed_venv.resolve(),
                    components,
                    _offline_environment(),
                    wheelhouse=wheelhouse,
                )
        except (OSError, subprocess.CalledProcessError, VerificationError) as error:
            print(f"install wheelhouse verification failed: {error}", file=sys.stderr)
            return 1
        return 0
    if (
        options.archive is None
        or options.bundle_root is not None
        or options.check_installed_venv is not None
        or (options.source_repository is None and options.source_ref != "HEAD")
    ):
        parser.error("archive is required")
    archive = options.archive.resolve()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = extract_archive(archive, Path(temporary))
            verify_manifest(root)
            project_wheel = verify_bundle_shape(root)
            verify_host_target(root)
            verify_sbom(root)
            verify_runtime_lock(root)
            verify_project_wheel_source(root, project_wheel)
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
