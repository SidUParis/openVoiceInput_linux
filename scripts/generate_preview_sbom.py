#!/usr/bin/env python3
"""Generate a deterministic, offline CycloneDX SBOM for a preview wheelhouse."""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import csv
import hashlib
import json
import re
import stat
import sys
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, replace
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Sequence
from urllib.parse import quote

SBOM_FILENAME = "SBOM.cdx.json"
MAX_WHEEL_FILES = 10_000
MAX_WHEEL_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
NAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
TARGET_PATTERN = re.compile(
    r"^ubuntu-(?P<ubuntu>[0-9]+(?:\.[0-9]+)*)-"
    r"(?P<machine>[A-Za-z0-9_]+)-py(?P<python>[0-9]+\.[0-9]+)$"
)
PYTHON_PATTERN = re.compile(
    r"^Python (?P<version>[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[A-Za-z0-9.+-]*))$"
)
REQUIREMENT_PATTERN = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"\s*(?:\[(?P<extras>[^]]*)\])?\s*(?P<constraints>.*)$"
)
SPECIFIER_PATTERN = re.compile(r"^(===|~=|==|!=|<=|>=|<|>)\s*(\S+)$")
VERSION_PATTERN = re.compile(
    r"^(?:(?P<epoch>[0-9]+)!)?"
    r"(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?:(?:[._-]?)(?P<pre>a|b|rc|alpha|beta|c|pre|preview)(?P<pre_n>[0-9]+)?)?"
    r"(?:(?:[._-]?post|[-_])(?P<post>[0-9]+))?"
    r"(?:(?:[._-]?dev)(?P<dev>[0-9]+))?"
    r"(?:\+(?P<local>[A-Za-z0-9._-]+))?$",
    re.IGNORECASE,
)
MARKER_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<string>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")|"
    r"(?P<operator>===|~=|==|!=|<=|>=|<|>)|"
    r"(?P<lparen>\()|(?P<rparen>\))|"
    r"(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
    r")"
)
VERSION_MARKER_NAMES = {
    "implementation_version",
    "python_full_version",
    "python_version",
}


class SBOMError(RuntimeError):
    """The bundle cannot be represented by the supported offline model."""


@dataclass(frozen=True)
class TargetEnvironment:
    source_commit: str
    target: str
    python_description: str
    python_version: str
    python_full_version: str
    ubuntu_version: str
    machine: str

    def marker_values(self, extra: str) -> dict[str, str]:
        return {
            "implementation_name": "cpython",
            "implementation_version": self.python_full_version,
            "os_name": "posix",
            "platform_machine": self.machine,
            "platform_python_implementation": "CPython",
            "platform_system": "Linux",
            "python_full_version": self.python_full_version,
            "python_version": self.python_version,
            "sys_platform": "linux",
            "extra": extra,
        }


@dataclass(frozen=True)
class Requirement:
    raw: str
    normalized_name: str
    extras: tuple[str, ...]
    specifiers: tuple[tuple[str, str], ...]
    marker: str | None


@dataclass(frozen=True)
class WheelComponent:
    filename: str
    name: str
    normalized_name: str
    version: str
    purl: str
    sha256: str
    license_expression: str | None
    license_name: str | None
    requirements: tuple[Requirement, ...]
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Operand:
    value: str
    variable: str | None = None


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _purl(name: str, version: str) -> str:
    normalized = _normalize_name(name)
    return f"pkg:pypi/{quote(normalized, safe='._~-')}@{quote(version, safe='._~-')}"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bundle_info(root: Path) -> TargetEnvironment:
    path = root / "BUNDLE-INFO"
    if not path.is_file() or path.is_symlink():
        raise SBOMError("BUNDLE-INFO is missing or unsafe")
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            raise SBOMError(f"invalid BUNDLE-INFO line: {line!r}")
        name, value = line.split("=", 1)
        if not name or name in fields or not value:
            raise SBOMError(f"invalid or duplicate BUNDLE-INFO field: {name!r}")
        fields[name] = value
    missing = {"source_commit", "target", "python"} - fields.keys()
    if missing:
        raise SBOMError(f"BUNDLE-INFO fields are missing: {sorted(missing)}")
    commit = fields["source_commit"]
    if re.fullmatch(r"[0-9a-f]{40,64}", commit) is None:
        raise SBOMError("BUNDLE-INFO source_commit is not a full hexadecimal commit")
    target_match = TARGET_PATTERN.fullmatch(fields["target"])
    if target_match is None:
        raise SBOMError("BUNDLE-INFO target is not a supported Ubuntu preview target")
    python_match = PYTHON_PATTERN.fullmatch(fields["python"])
    if python_match is None:
        raise SBOMError("BUNDLE-INFO python field is not a supported Python version")
    full_version = python_match.group("version")
    python_version = ".".join(full_version.split(".")[:2])
    if python_version != target_match.group("python"):
        raise SBOMError("BUNDLE-INFO Python version disagrees with its target tag")
    return TargetEnvironment(
        source_commit=commit,
        target=fields["target"],
        python_description=fields["python"],
        python_version=python_version,
        python_full_version=full_version,
        ubuntu_version=target_match.group("ubuntu"),
        machine=target_match.group("machine"),
    )


def _safe_wheel_member(name: str, *, is_directory: bool = False) -> PurePosixPath:
    path = PurePosixPath(name)
    canonical_name = path.as_posix()
    expected_name = f"{canonical_name}/" if is_directory else canonical_name
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "\\" in name
        or expected_name != name
    ):
        raise SBOMError(f"unsafe wheel member path: {name!r}")
    return path


def _record_digest(content: bytes, encoded: str, path: str) -> None:
    if "=" not in encoded:
        raise SBOMError(f"invalid RECORD digest for {path!r}")
    algorithm, encoded_digest = encoded.split("=", 1)
    if algorithm not in {"sha256", "sha384", "sha512"}:
        raise SBOMError(f"unsupported RECORD digest algorithm for {path!r}")
    try:
        expected = base64.urlsafe_b64decode(
            encoded_digest + "=" * (-len(encoded_digest) % 4)
        )
    except (binascii.Error, ValueError, TypeError) as error:
        raise SBOMError(f"invalid RECORD digest for {path!r}") from error
    actual = hashlib.new(algorithm, content).digest()
    if actual != expected:
        raise SBOMError(f"wheel RECORD digest mismatch: {path}")


def _validate_record(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    record_name: str,
) -> None:
    try:
        record_text = archive.read(record_name).decode("utf-8")
    except UnicodeDecodeError as error:
        raise SBOMError("wheel RECORD is not UTF-8") from error
    declared: dict[str, tuple[str, str]] = {}
    try:
        rows = csv.reader(record_text.splitlines())
        for row in rows:
            if len(row) != 3:
                raise SBOMError(f"wheel RECORD row must have three fields: {row!r}")
            filename, digest, size = row
            _safe_wheel_member(filename)
            if filename in declared:
                raise SBOMError(f"duplicate wheel RECORD path: {filename!r}")
            declared[filename] = (digest, size)
    except csv.Error as error:
        raise SBOMError("wheel RECORD is malformed") from error
    if set(declared) != set(members):
        missing = sorted(set(members) - set(declared))
        unexpected = sorted(set(declared) - set(members))
        raise SBOMError(
            f"wheel RECORD file set differs: missing={missing}, unexpected={unexpected}"
        )
    signature_names = {
        record_name,
        f"{record_name}.jws",
        f"{record_name}.p7s",
    }
    for filename, info in members.items():
        digest, size = declared[filename]
        content = archive.read(info)
        if filename in signature_names and not digest and not size:
            continue
        if not digest or not size or not size.isdecimal():
            raise SBOMError(f"wheel RECORD omits integrity data: {filename}")
        if int(size) != len(content):
            raise SBOMError(f"wheel RECORD size mismatch: {filename}")
        _record_digest(content, digest, filename)


def _split_marker(value: str) -> tuple[str, str | None]:
    quote_character: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote_character is not None:
            escaped = True
            continue
        if character in {'"', "'"}:
            if quote_character is None:
                quote_character = character
            elif quote_character == character:
                quote_character = None
            continue
        if character == ";" and quote_character is None:
            requirement, marker = value[:index].strip(), value[index + 1 :].strip()
            if not marker:
                raise SBOMError(f"empty environment marker in Requires-Dist: {value!r}")
            return requirement, marker
    if quote_character is not None:
        raise SBOMError(f"unterminated quote in Requires-Dist: {value!r}")
    return value.strip(), None


def _parse_requirement(value: str) -> Requirement:
    requirement_text, marker = _split_marker(value)
    match = REQUIREMENT_PATTERN.fullmatch(requirement_text)
    if match is None:
        raise SBOMError(f"unsupported Requires-Dist: {value!r}")
    name = match.group("name")
    if not NAME_PATTERN.fullmatch(name):
        raise SBOMError(f"invalid dependency name in Requires-Dist: {value!r}")
    extras_text = match.group("extras")
    extras: list[str] = []
    if extras_text is not None:
        for extra in extras_text.split(","):
            normalized = _normalize_name(extra.strip())
            if not normalized or not NAME_PATTERN.fullmatch(extra.strip()):
                raise SBOMError(f"invalid dependency extra in Requires-Dist: {value!r}")
            extras.append(normalized)
    constraints = match.group("constraints").strip()
    if constraints.startswith("@"):
        raise SBOMError("direct-URL Requires-Dist entries are not allowed offline")
    if constraints.startswith("(") and constraints.endswith(")"):
        constraints = constraints[1:-1].strip()
    parsed_specifiers: list[tuple[str, str]] = []
    if constraints:
        for specifier in constraints.split(","):
            specifier_match = SPECIFIER_PATTERN.fullmatch(specifier.strip())
            if specifier_match is None:
                raise SBOMError(
                    f"unsupported version specifier in Requires-Dist: {value!r}"
                )
            parsed_specifiers.append(specifier_match.groups())
    return Requirement(
        raw=value,
        normalized_name=_normalize_name(name),
        extras=tuple(sorted(set(extras))),
        specifiers=tuple(parsed_specifiers),
        marker=marker,
    )


def _metadata_value(message: object, name: str) -> str:
    values = message.get_all(name, [])  # type: ignore[attr-defined]
    if len(values) != 1 or not values[0].strip():
        raise SBOMError(f"wheel METADATA must contain exactly one {name} field")
    return values[0].strip()


def _read_wheel(path: Path) -> WheelComponent:
    if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
        raise SBOMError(f"unsafe or non-wheel wheelhouse entry: {path.name}")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_WHEEL_FILES:
                raise SBOMError(f"wheel has an invalid entry count: {path.name}")
            total_size = sum(info.file_size for info in infos if not info.is_dir())
            if total_size > MAX_WHEEL_BYTES:
                raise SBOMError(f"wheel expands beyond the safety limit: {path.name}")
            members: dict[str, zipfile.ZipInfo] = {}
            seen_members: set[str] = set()
            for info in infos:
                member_path = _safe_wheel_member(
                    info.filename,
                    is_directory=info.is_dir(),
                )
                canonical_name = member_path.as_posix()
                if canonical_name in seen_members:
                    raise SBOMError(f"duplicate wheel member: {info.filename!r}")
                seen_members.add(canonical_name)
                mode = info.external_attr >> 16
                if stat.S_IFMT(mode) == stat.S_IFLNK:
                    raise SBOMError(
                        f"wheel contains a symbolic link: {info.filename!r}"
                    )
                if info.flag_bits & 0x1:
                    raise SBOMError(
                        f"wheel contains an encrypted member: {info.filename!r}"
                    )
                if not info.is_dir():
                    members[canonical_name] = info
            metadata_names = [
                name
                for name in members
                if PurePosixPath(name).name == "METADATA"
                and PurePosixPath(name).parent.name.endswith(".dist-info")
            ]
            if len(metadata_names) != 1:
                raise SBOMError("wheel must contain exactly one dist-info/METADATA")
            metadata_name = metadata_names[0]
            record_name = str(PurePosixPath(metadata_name).with_name("RECORD"))
            if record_name not in members:
                raise SBOMError("wheel dist-info/RECORD is missing")
            _validate_record(archive, members, record_name)
            metadata_bytes = archive.read(metadata_name)
            if len(metadata_bytes) > MAX_METADATA_BYTES:
                raise SBOMError("wheel METADATA exceeds the safety limit")
            message = BytesParser(policy=policy.compat32).parsebytes(metadata_bytes)
    except (OSError, zipfile.BadZipFile) as error:
        raise SBOMError(f"cannot read wheel {path.name}: {error}") from error

    name = _metadata_value(message, "Name")
    version = _metadata_value(message, "Version")
    if (
        not NAME_PATTERN.fullmatch(name)
        or not version
        or any(character.isspace() for character in version)
    ):
        raise SBOMError(f"invalid package identity in wheel: {path.name}")
    expressions = [value.strip() for value in message.get_all("License-Expression", [])]
    legacy_licenses = [value.strip() for value in message.get_all("License", [])]
    if len(expressions) > 1 or len(legacy_licenses) > 1:
        raise SBOMError(f"ambiguous licence metadata in wheel: {path.name}")
    license_expression = expressions[0] if expressions and expressions[0] else None
    license_name = (
        legacy_licenses[0] if legacy_licenses and legacy_licenses[0] else None
    )
    if license_expression is None and license_name is None:
        raise SBOMError(f"wheel has no machine-readable licence metadata: {path.name}")
    requirements = tuple(
        _parse_requirement(value) for value in message.get_all("Requires-Dist", [])
    )
    return WheelComponent(
        filename=path.name,
        name=name,
        normalized_name=_normalize_name(name),
        version=version,
        purl=_purl(name, version),
        sha256=_sha256_path(path),
        license_expression=license_expression,
        license_name=license_name,
        requirements=requirements,
    )


def _version_key(value: str) -> tuple[object, ...]:
    match = VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise SBOMError(
            f"unsupported PEP 440 version in dependency metadata: {value!r}"
        )
    release = [int(part) for part in match.group("release").split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre_name = (match.group("pre") or "").lower()
    pre_aliases = {
        "alpha": "a",
        "beta": "b",
        "c": "rc",
        "pre": "rc",
        "preview": "rc",
    }
    pre_name = pre_aliases.get(pre_name, pre_name)
    pre_rank = {"a": -3, "b": -2, "rc": -1}.get(pre_name, 0)
    pre_number = int(match.group("pre_n") or 0)
    dev = match.group("dev")
    post = match.group("post")
    if dev is not None and not pre_name:
        stage = (-4, int(dev))
    elif pre_name:
        stage = (pre_rank, pre_number, -1 if dev is not None else 0, int(dev or 0))
    elif post is not None:
        stage = (1, int(post))
    else:
        stage = (0, 0)
    return (int(match.group("epoch") or 0), tuple(release), stage)


def _compare_versions(left: str, right: str) -> int:
    left_key = _version_key(left)
    right_key = _version_key(right)
    return (left_key > right_key) - (left_key < right_key)


def _specifier_matches(version: str, operator: str, expected: str) -> bool:
    if operator == "===":
        return version == expected
    if operator in {"==", "!="} and expected.endswith(".*"):
        prefix = expected[:-2]
        actual_release = version.split("+", 1)[0].split("!", 1)[-1]
        matches = actual_release == prefix or actual_release.startswith(f"{prefix}.")
        return matches if operator == "==" else not matches
    comparison = _compare_versions(version, expected)
    if operator == "==":
        return comparison == 0
    if operator == "!=":
        return comparison != 0
    if operator == "<":
        return comparison < 0
    if operator == "<=":
        return comparison <= 0
    if operator == ">":
        return comparison > 0
    if operator == ">=":
        return comparison >= 0
    if operator == "~=":
        release = expected.split("+", 1)[0].split("!", 1)[-1].split(".")
        if len(release) < 2 or not all(part.isdecimal() for part in release):
            raise SBOMError(f"unsupported compatible-release specifier: {expected!r}")
        prefix = ".".join(release[:-1] if len(release) > 2 else release[:1])
        return comparison >= 0 and _specifier_matches(version, "==", f"{prefix}.*")
    raise SBOMError(f"unsupported version operator: {operator!r}")


class _MarkerParser:
    def __init__(self, marker: str, environment: dict[str, str]) -> None:
        self.marker = marker
        self.environment = environment
        self.tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(marker):
            match = MARKER_TOKEN.match(marker, position)
            if match is None:
                raise SBOMError(f"unsupported environment marker: {marker!r}")
            token_type = match.lastgroup
            if token_type is None:
                raise SBOMError(f"unsupported environment marker: {marker!r}")
            self.tokens.append((token_type, match.group(token_type)))
            position = match.end()
        self.position = 0

    def parse(self) -> bool:
        result = self._parse_or()
        if self.position != len(self.tokens):
            raise SBOMError(f"unexpected token in environment marker: {self.marker!r}")
        return result

    def _peek_word(self, value: str) -> bool:
        return self.position < len(self.tokens) and self.tokens[self.position] == (
            "word",
            value,
        )

    def _consume(self, token_type: str, value: str | None = None) -> str:
        if self.position >= len(self.tokens):
            raise SBOMError(f"incomplete environment marker: {self.marker!r}")
        actual_type, actual_value = self.tokens[self.position]
        if actual_type != token_type or (value is not None and actual_value != value):
            raise SBOMError(f"unexpected token in environment marker: {self.marker!r}")
        self.position += 1
        return actual_value

    def _parse_or(self) -> bool:
        result = self._parse_and()
        while self._peek_word("or"):
            self._consume("word", "or")
            next_result = self._parse_and()
            result = result or next_result
        return result

    def _parse_and(self) -> bool:
        result = self._parse_factor()
        while self._peek_word("and"):
            self._consume("word", "and")
            next_result = self._parse_factor()
            result = result and next_result
        return result

    def _parse_factor(self) -> bool:
        if (
            self.position < len(self.tokens)
            and self.tokens[self.position][0] == "lparen"
        ):
            self._consume("lparen")
            result = self._parse_or()
            self._consume("rparen")
            return result
        left = self._parse_operand()
        operator = self._parse_operator()
        right = self._parse_operand()
        return self._compare(left, operator, right)

    def _parse_operand(self) -> _Operand:
        if self.position >= len(self.tokens):
            raise SBOMError(f"incomplete environment marker: {self.marker!r}")
        token_type, value = self.tokens[self.position]
        self.position += 1
        if token_type == "string":
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError) as error:
                raise SBOMError(
                    f"invalid string in environment marker: {self.marker!r}"
                ) from error
            if not isinstance(parsed, str):
                raise SBOMError(
                    f"invalid string in environment marker: {self.marker!r}"
                )
            return _Operand(parsed)
        if token_type != "word" or value not in self.environment:
            raise SBOMError(
                f"unknown target variable {value!r} in environment marker: {self.marker!r}"
            )
        return _Operand(self.environment[value], value)

    def _parse_operator(self) -> str:
        if self.position >= len(self.tokens):
            raise SBOMError(f"missing operator in environment marker: {self.marker!r}")
        token_type, value = self.tokens[self.position]
        if token_type == "operator":
            self.position += 1
            return value
        if (token_type, value) == ("word", "in"):
            self.position += 1
            return "in"
        if (token_type, value) == ("word", "not"):
            self.position += 1
            self._consume("word", "in")
            return "not in"
        raise SBOMError(f"unsupported operator in environment marker: {self.marker!r}")

    def _compare(self, left: _Operand, operator: str, right: _Operand) -> bool:
        if operator in {"in", "not in"}:
            result = left.value in right.value
            return result if operator == "in" else not result
        version_comparison = (
            left.variable in VERSION_MARKER_NAMES
            or right.variable in VERSION_MARKER_NAMES
        )
        if version_comparison:
            if left.variable in VERSION_MARKER_NAMES:
                return _specifier_matches(left.value, operator, right.value)
            reversed_operators = {
                "==": "==",
                "!=": "!=",
                "<": ">",
                "<=": ">=",
                ">": "<",
                ">=": "<=",
            }
            if operator not in reversed_operators:
                raise SBOMError(
                    f"unsupported reversed version comparison: {self.marker!r}"
                )
            return _specifier_matches(
                right.value,
                reversed_operators[operator],
                left.value,
            )
        comparisons = {
            "==": left.value == right.value,
            "!=": left.value != right.value,
            "<": left.value < right.value,
            "<=": left.value <= right.value,
            ">": left.value > right.value,
            ">=": left.value >= right.value,
        }
        if operator not in comparisons:
            raise SBOMError(f"unsupported string comparison: {self.marker!r}")
        return comparisons[operator]


def _requirement_is_active(
    requirement: Requirement,
    target: TargetEnvironment,
    selected_extras: set[str],
) -> bool:
    if requirement.marker is None:
        return True
    environments = [""] + sorted(selected_extras)
    return any(
        _MarkerParser(requirement.marker, target.marker_values(extra)).parse()
        for extra in environments
    )


def _resolve_dependencies(
    components: dict[str, WheelComponent],
    project_name: str,
    target: TargetEnvironment,
) -> dict[str, WheelComponent]:
    selected_extras: dict[str, set[str]] = {project_name: set()}
    dependencies: dict[str, set[str]] = {name: set() for name in components}
    queue: deque[str] = deque([project_name])
    while queue:
        component_name = queue.popleft()
        component = components[component_name]
        for requirement in component.requirements:
            if not _requirement_is_active(
                requirement,
                target,
                selected_extras[component_name],
            ):
                continue
            dependency = components.get(requirement.normalized_name)
            if dependency is None:
                raise SBOMError(
                    f"wheelhouse is missing runtime dependency "
                    f"{requirement.normalized_name!r} required by {component.name!r}"
                )
            if not all(
                _specifier_matches(dependency.version, operator, expected)
                for operator, expected in requirement.specifiers
            ):
                raise SBOMError(
                    f"wheelhouse version {dependency.version!r} does not satisfy "
                    f"{requirement.raw!r} required by {component.name!r}"
                )
            dependencies[component_name].add(requirement.normalized_name)
            first_visit = requirement.normalized_name not in selected_extras
            existing_extras = selected_extras.setdefault(
                requirement.normalized_name, set()
            )
            extra_change = not set(requirement.extras).issubset(existing_extras)
            existing_extras.update(requirement.extras)
            if first_visit or extra_change:
                queue.append(requirement.normalized_name)
    unreachable = sorted(set(components) - set(selected_extras))
    if unreachable:
        raise SBOMError(f"wheelhouse contains unrelated runtime wheels: {unreachable}")
    return {
        name: replace(
            component,
            dependencies=tuple(sorted(dependencies[name])),
        )
        for name, component in components.items()
    }


def read_wheelhouse(
    root: Path,
) -> tuple[TargetEnvironment, dict[str, WheelComponent], str]:
    """Read and validate the exact target-specific runtime dependency closure."""

    target = _read_bundle_info(root)
    wheelhouse = root / "wheelhouse"
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise SBOMError("wheelhouse is missing or unsafe")
    entries = sorted(wheelhouse.iterdir(), key=lambda path: path.name)
    if not entries:
        raise SBOMError("wheelhouse is empty")
    components: dict[str, WheelComponent] = {}
    for path in entries:
        component = _read_wheel(path)
        if component.normalized_name in components:
            raise SBOMError(
                f"wheelhouse contains multiple versions of {component.normalized_name!r}"
            )
        components[component.normalized_name] = component
    project_matches = [
        name for name in components if name == _normalize_name("murmur-ime-voice")
    ]
    if len(project_matches) != 1:
        raise SBOMError(
            "wheelhouse must contain exactly one murmur-ime-voice project wheel"
        )
    project_name = project_matches[0]
    return target, _resolve_dependencies(components, project_name, target), project_name


def _cyclonedx_licenses(component: WheelComponent) -> list[dict[str, object]]:
    if component.license_expression is not None:
        return [{"expression": component.license_expression}]
    if component.license_name is None:
        raise AssertionError("validated component has no licence")
    return [{"license": {"name": component.license_name}}]


def _cyclonedx_component(
    component: WheelComponent,
    *,
    project: bool,
) -> dict[str, object]:
    return {
        "bom-ref": component.purl,
        "type": "application" if project else "library",
        "name": component.name,
        "version": component.version,
        "purl": component.purl,
        "hashes": [{"alg": "SHA-256", "content": component.sha256}],
        "licenses": _cyclonedx_licenses(component),
        "properties": [
            {"name": "openvoice:wheel-filename", "value": component.filename}
        ],
    }


def build_sbom(root: Path) -> dict[str, object]:
    """Build the complete deterministic CycloneDX 1.5 JSON object in memory."""

    target, components, project_name = read_wheelhouse(root)
    project = components[project_name]
    serial_seed = json.dumps(
        {
            "source_commit": target.source_commit,
            "target": target.target,
            "python": target.python_description,
            "wheels": [
                [component.filename, component.sha256]
                for component in sorted(
                    components.values(), key=lambda item: item.normalized_name
                )
            ],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_seed)}",
        "version": 1,
        "metadata": {
            "component": _cyclonedx_component(project, project=True),
            "properties": [
                {"name": "openvoice:source-commit", "value": target.source_commit},
                {"name": "openvoice:target", "value": target.target},
                {"name": "openvoice:python", "value": target.python_description},
            ],
        },
        "components": [
            _cyclonedx_component(component, project=False)
            for component in sorted(
                components.values(), key=lambda item: item.normalized_name
            )
            if component.normalized_name != project_name
        ],
        "dependencies": [
            {
                "ref": component.purl,
                "dependsOn": [components[name].purl for name in component.dependencies],
            }
            for component in sorted(
                components.values(), key=lambda item: item.normalized_name
            )
        ],
    }


def render_sbom(sbom: dict[str, object]) -> bytes:
    """Serialize with one canonical, reproducible JSON representation."""

    return (
        json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(arguments)
    root = options.bundle_root.resolve()
    output = options.output.resolve()
    expected_output = root / SBOM_FILENAME
    if output != expected_output:
        print(
            f"SBOM generation failed: output must be {expected_output}",
            file=sys.stderr,
        )
        return 2
    if output.exists() or output.is_symlink():
        print(
            f"SBOM generation failed: refusing to overwrite {output}",
            file=sys.stderr,
        )
        return 2
    try:
        payload = render_sbom(build_sbom(root))
        output.write_bytes(payload)
    except (OSError, UnicodeError, SBOMError) as error:
        print(f"SBOM generation failed: {error}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
