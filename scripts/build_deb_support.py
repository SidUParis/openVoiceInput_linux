"""Deterministic helpers for the Ubuntu 24.04 Debian package builder.

This module deliberately uses only the Python standard library.  The package
builder calls it before any dependency code is unpacked, and its small public
functions are unit-tested directly.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Sequence
from zipfile import BadZipFile, ZipFile, ZipInfo


class DebBuildError(RuntimeError):
    """Raised when an input cannot produce the declared Debian package."""


_PEP440_RELEASE = re.compile(
    r"^(?P<base>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:(?P<stage>a|b|rc)(?P<number>0|[1-9][0-9]*))?$"
)
_LOCK_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\\\s]+) \\$")
_LOCK_HASH = re.compile(r"^\s+--hash=sha256:([0-9a-f]{64})$")
_SAFE_COMMIT = re.compile(r"^[0-9a-f]{12}$")
_SAFE_EPOCH = re.compile(r"^[1-9][0-9]{8,11}$")


def normalize_name(value: str) -> str:
    """Return the canonical distribution-name spelling used by Python tools."""

    return re.sub(r"[-_.]+", "-", value).lower()


def pep440_to_debian(version: str) -> str:
    """Map the intentionally small project version grammar to Debian syntax."""

    match = _PEP440_RELEASE.fullmatch(version)
    if match is None:
        raise DebBuildError(f"unsupported project version: {version!r}")
    base = ".".join((match.group("base"), match.group("minor"), match.group("patch")))
    stage = match.group("stage")
    if stage is None:
        return f"{base}-1"
    stage_name = {"a": "alpha", "b": "beta", "rc": "rc"}[stage]
    return f"{base}~{stage_name}{match.group('number')}-1"


def package_version(pyproject: Path, source_epoch: str, short_commit: str) -> str:
    """Return the Debian release version after validating provenance inputs."""

    if not _SAFE_EPOCH.fullmatch(source_epoch):
        raise DebBuildError("source epoch is not a bounded positive integer")
    if not _SAFE_COMMIT.fullmatch(short_commit):
        raise DebBuildError("short commit must contain exactly 12 lowercase hex digits")
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        upstream = document["project"]["version"]
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError) as error:
        raise DebBuildError(f"could not read project version: {error}") from error
    if not isinstance(upstream, str):
        raise DebBuildError("project.version must be a string")
    return pep440_to_debian(upstream)


@dataclass(frozen=True)
class LockedWheel:
    name: str
    version: str
    sha256: str


def parse_runtime_lock(path: Path) -> dict[str, LockedWheel]:
    """Read the exact two-line requirement grammar used by the preview lock."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise DebBuildError(f"could not read runtime lock: {error}") from error
    locked: dict[str, LockedWheel] = {}
    line_number = 0
    while line_number < len(lines):
        line = lines[line_number]
        line_number += 1
        if not line or line.startswith("#"):
            continue
        requirement = _LOCK_REQUIREMENT.fullmatch(line)
        if requirement is None or line_number >= len(lines):
            raise DebBuildError(f"invalid locked requirement at line {line_number}")
        digest = _LOCK_HASH.fullmatch(lines[line_number])
        line_number += 1
        if digest is None:
            raise DebBuildError(f"missing wheel hash at line {line_number}")
        name = normalize_name(requirement.group(1))
        if name in locked:
            raise DebBuildError(f"duplicate locked requirement: {name}")
        locked[name] = LockedWheel(
            name=name,
            version=requirement.group(2),
            sha256=digest.group(1),
        )
    if not locked:
        raise DebBuildError("runtime lock is empty")
    return locked


def _regular_unlinked_file(path: Path, *, kind: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise DebBuildError(f"could not inspect {kind}: {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise DebBuildError(f"{kind} must be a regular unlinked file: {path}")
    return metadata


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with ZipFile(path) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise DebBuildError(f"wheel has ambiguous metadata: {path.name}")
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    except (BadZipFile, KeyError, OSError) as error:
        raise DebBuildError(f"invalid wheel archive {path.name}: {error}") from error
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise DebBuildError(f"wheel metadata is incomplete: {path.name}")
    return normalize_name(name), version


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise DebBuildError(f"could not hash wheel {path.name}: {error}") from error
    return digest.hexdigest()


def verified_runtime_wheels(lock: Path, wheelhouse: Path) -> list[Path]:
    """Return the one hash-matched wheel for every runtime lock entry.

    A preview wheelhouse may also contain its project wheel.  That wheel is
    deliberately ignored: application code always comes from the selected Git
    revision, never from an adjacent unpinned archive.
    """

    locked = parse_runtime_lock(lock)
    locked_by_digest = {entry.sha256: entry for entry in locked.values()}
    if len(locked_by_digest) != len(locked):
        raise DebBuildError("runtime lock reuses a wheel hash")
    try:
        wheelhouse_metadata = wheelhouse.lstat()
    except OSError as error:
        raise DebBuildError(f"could not inspect wheelhouse: {error}") from error
    if not stat.S_ISDIR(wheelhouse_metadata.st_mode):
        raise DebBuildError("wheelhouse must be a real directory, not a link")

    observed: dict[str, Path] = {}
    project_wheel_count = 0
    for wheel in sorted(wheelhouse.iterdir(), key=lambda item: item.name):
        _regular_unlinked_file(wheel, kind="wheelhouse entry")
        if wheel.suffix != ".whl":
            raise DebBuildError(f"wheelhouse contains a non-wheel file: {wheel.name}")
        # A complete preview wheelhouse contains the application wheel. It is
        # never opened or trusted by this builder; exact application bytes are
        # exported from Git below. Runtime archives are hash-gated before ZIP
        # metadata is parsed.
        if re.fullmatch(
            r"murmur_ime_voice-[0-9A-Za-z_.+]+-[^-]+-[^-]+-[^-]+\.whl", wheel.name
        ):
            project_wheel_count += 1
            if project_wheel_count > 1:
                raise DebBuildError("wheelhouse contains multiple project wheels")
            continue
        digest = _sha256_file(wheel)
        expected = locked_by_digest.get(digest)
        if expected is None:
            filename_candidates = [
                entry
                for entry in locked.values()
                if wheel.name.startswith(f"{entry.name.replace('-', '_')}-")
            ]
            if len(filename_candidates) == 1:
                raise DebBuildError(f"runtime wheel hash mismatch: {wheel.name}")
            raise DebBuildError(f"wheelhouse contains an unlocked wheel: {wheel.name}")
        name, version = _wheel_identity(wheel)
        if name != expected.name:
            raise DebBuildError(
                f"runtime wheel name mismatch: expected {expected.name}, observed {name}"
            )
        if name in observed:
            raise DebBuildError(f"wheelhouse contains duplicate runtime wheel: {name}")
        if version != expected.version:
            raise DebBuildError(
                f"runtime wheel version mismatch for {name}: {version!r}"
            )
        observed[name] = wheel
    if observed.keys() != locked.keys():
        missing = sorted(locked.keys() - observed.keys())
        raise DebBuildError(f"wheelhouse is missing locked runtime wheels: {missing!r}")
    return [observed[name] for name in sorted(observed)]


def _destination_for_wheel_member(info: ZipInfo) -> PurePosixPath | None:
    raw = info.filename
    if not raw or "\\" in raw or "\x00" in raw:
        raise DebBuildError(f"wheel contains an unsafe member name: {raw!r}")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(
        part in ("", ".", "..") for part in relative.parts
    ):
        raise DebBuildError(f"wheel contains an unsafe member path: {raw!r}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise DebBuildError(f"wheel contains a symbolic link: {raw!r}")
    parts = relative.parts
    data_index = next(
        (index for index, part in enumerate(parts) if part.endswith(".data")), None
    )
    if data_index is None:
        return relative
    if data_index != 0 or len(parts) < 3:
        raise DebBuildError(f"wheel contains malformed data path: {raw!r}")
    scheme = parts[1]
    if scheme not in ("purelib", "platlib"):
        # Console scripts and headers are intentionally not part of this
        # private import tree.  The application provides audited launchers.
        if scheme in ("scripts", "headers", "data"):
            return None
        raise DebBuildError(f"wheel uses an unknown installation scheme: {raw!r}")
    return PurePosixPath(*parts[2:])


def unpack_runtime_wheels(lock: Path, wheelhouse: Path, output: Path) -> None:
    """Safely unpack hash-locked runtime wheels into a new private import tree."""

    if output.exists() or output.is_symlink():
        raise DebBuildError(f"vendor output already exists: {output}")
    output.mkdir(mode=0o755, parents=True)
    destinations: set[PurePosixPath] = set()
    for wheel in verified_runtime_wheels(lock, wheelhouse):
        try:
            with ZipFile(wheel) as archive:
                for info in sorted(archive.infolist(), key=lambda item: item.filename):
                    destination = _destination_for_wheel_member(info)
                    if destination is None or info.is_dir():
                        continue
                    if destination in destinations:
                        raise DebBuildError(
                            f"runtime wheels collide at: {destination.as_posix()}"
                        )
                    destinations.add(destination)
                    target = output.joinpath(*destination.parts)
                    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    data = archive.read(info)
                    descriptor = os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                        0o644,
                    )
                    try:
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(data)
                    except BaseException:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
                        raise
        except (BadZipFile, KeyError, OSError) as error:
            raise DebBuildError(f"could not unpack {wheel.name}: {error}") from error


def render_control(
    template: Path, output: Path, *, version: str, installed_size: str
) -> None:
    """Render the fixed Debian control template without shell interpolation."""

    if not re.fullmatch(r"[0-9A-Za-z.+:~_-]+", version):
        raise DebBuildError("Debian version contains unsupported characters")
    if not re.fullmatch(r"[1-9][0-9]*", installed_size):
        raise DebBuildError("installed size must be a positive integer")
    try:
        content = template.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise DebBuildError(f"could not read control template: {error}") from error
    if content.count("@PACKAGE_VERSION@") != 1:
        raise DebBuildError("control template must contain one version token")
    if content.count("@INSTALLED_SIZE@") != 1:
        raise DebBuildError("control template must contain one size token")
    rendered = content.replace("@PACKAGE_VERSION@", version).replace(
        "@INSTALLED_SIZE@", installed_size
    )
    if re.search(r"@[A-Z_]+@", rendered):
        raise DebBuildError("control template contains an unresolved token")
    try:
        output.write_text(rendered, encoding="utf-8")
    except OSError as error:
        raise DebBuildError(f"could not write package control file: {error}") from error


def build_sbom(
    lock: Path,
    *,
    source_commit: str,
    source_epoch: str,
    package_version_value: str,
) -> dict[str, object]:
    """Build a deterministic CycloneDX inventory for source and bundled wheels."""

    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise DebBuildError("source commit must contain 40 lowercase hex digits")
    if not _SAFE_EPOCH.fullmatch(source_epoch):
        raise DebBuildError("source epoch is not a bounded positive integer")
    timestamp = (
        datetime.fromtimestamp(int(source_epoch), tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    locked = parse_runtime_lock(lock)
    identity = "\n".join(
        [source_commit, source_epoch, package_version_value]
        + [
            f"{entry.name}=={entry.version}:{entry.sha256}"
            for entry in (locked[name] for name in sorted(locked))
        ]
    )
    components: list[dict[str, object]] = []
    for name in sorted(locked):
        entry = locked[name]
        components.append(
            {
                "bom-ref": f"pkg:pypi/{entry.name}@{entry.version}",
                "hashes": [{"alg": "SHA-256", "content": entry.sha256}],
                "name": entry.name,
                "purl": f"pkg:pypi/{entry.name}@{entry.version}",
                "type": "library",
                "version": entry.version,
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "components": components,
        "metadata": {
            "component": {
                "bom-ref": f"pkg:deb/ubuntu/open-voice-input-linux@{package_version_value}?arch=amd64",
                "name": "open-voice-input-linux",
                "properties": [
                    {"name": "openvoiceinput:source-commit", "value": source_commit},
                    {
                        "name": "openvoiceinput:target",
                        "value": "ubuntu-24.04-amd64-cpython-3.12",
                    },
                ],
                "type": "application",
                "version": package_version_value,
            },
            "timestamp": timestamp,
        },
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, identity)}",
        "specVersion": "1.5",
        "version": 1,
    }


def write_sbom(
    lock: Path,
    output: Path,
    *,
    source_commit: str,
    source_epoch: str,
    package_version_value: str,
) -> None:
    import json

    document = build_sbom(
        lock,
        source_commit=source_commit,
        source_epoch=source_epoch,
        package_version_value=package_version_value,
    )
    try:
        output.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise DebBuildError(f"could not write package SBOM: {error}") from error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    version_parser = subparsers.add_parser("version")
    version_parser.add_argument("--pyproject", required=True, type=Path)
    version_parser.add_argument("--source-epoch", required=True)
    version_parser.add_argument("--short-commit", required=True)
    unpack_parser = subparsers.add_parser("unpack-runtime")
    unpack_parser.add_argument("--lock", required=True, type=Path)
    unpack_parser.add_argument("--wheelhouse", required=True, type=Path)
    unpack_parser.add_argument("--output", required=True, type=Path)
    control_parser = subparsers.add_parser("render-control")
    control_parser.add_argument("--template", required=True, type=Path)
    control_parser.add_argument("--output", required=True, type=Path)
    control_parser.add_argument("--version", required=True)
    control_parser.add_argument("--installed-size", required=True)
    sbom_parser = subparsers.add_parser("write-sbom")
    sbom_parser.add_argument("--lock", required=True, type=Path)
    sbom_parser.add_argument("--output", required=True, type=Path)
    sbom_parser.add_argument("--source-commit", required=True)
    sbom_parser.add_argument("--source-epoch", required=True)
    sbom_parser.add_argument("--package-version", required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    options = parser.parse_args(arguments)
    try:
        if options.command == "version":
            print(
                package_version(
                    options.pyproject, options.source_epoch, options.short_commit
                )
            )
        elif options.command == "unpack-runtime":
            unpack_runtime_wheels(options.lock, options.wheelhouse, options.output)
        elif options.command == "render-control":
            render_control(
                options.template,
                options.output,
                version=options.version,
                installed_size=options.installed_size,
            )
        elif options.command == "write-sbom":
            write_sbom(
                options.lock,
                options.output,
                source_commit=options.source_commit,
                source_epoch=options.source_epoch,
                package_version_value=options.package_version,
            )
        else:  # pragma: no cover - argparse prevents this branch.
            parser.error("unsupported command")
    except DebBuildError as error:
        print(f"Debian package build input rejected: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
