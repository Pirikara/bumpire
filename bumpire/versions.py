"""Version-policy evaluation with package-manager-specific ordering rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import zip_longest
from typing import Literal

from bumpire.models import VersionRequirement

VersionScheme = Literal["semver", "rubygems"]

_SEMVER = re.compile(
    r"\A"
    r"(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>"
    r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"\Z"
)
_RUBYGEMS = re.compile(r"\A[0-9]+(?:\.[0-9A-Za-z]+)*\Z")
_RUBYGEMS_SEGMENT = re.compile(r"[0-9]+|[A-Za-z]+")


class VersionComparisonError(ValueError):
    """A version cannot be evaluated under its package-manager scheme."""


@dataclass(frozen=True, slots=True)
class _SemVer:
    release: tuple[int, int, int]
    prerelease: tuple[int | str, ...] | None


def satisfies(
    actual: str,
    requirement: VersionRequirement,
    scheme: VersionScheme,
) -> bool:
    """Return whether one locked version satisfies an explicit update policy."""
    if requirement.kind == "exact":
        return actual == requirement.version
    if scheme == "semver":
        return _compare_semver(actual, requirement.version) >= 0
    if scheme == "rubygems":
        return _compare_rubygems(actual, requirement.version) >= 0
    raise VersionComparisonError(f"unsupported version scheme: {scheme}")


def _compare_semver(left: str, right: str) -> int:
    lhs = _parse_semver(left)
    rhs = _parse_semver(right)
    if lhs.release != rhs.release:
        return _sign((lhs.release > rhs.release) - (lhs.release < rhs.release))
    if lhs.prerelease is None:
        return 0 if rhs.prerelease is None else 1
    if rhs.prerelease is None:
        return -1
    for left_part, right_part in zip_longest(lhs.prerelease, rhs.prerelease):
        if left_part is None:
            return -1
        if right_part is None:
            return 1
        if left_part == right_part:
            continue
        if isinstance(left_part, int) and isinstance(right_part, str):
            return -1
        if isinstance(left_part, str) and isinstance(right_part, int):
            return 1
        return _sign((left_part > right_part) - (left_part < right_part))
    return 0


def _parse_semver(value: str) -> _SemVer:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise VersionComparisonError(f"invalid semantic version: {value!r}")
    prerelease = match.group("prerelease")
    parts = None
    if prerelease is not None:
        parts = tuple(int(part) if part.isdigit() else part for part in prerelease.split("."))
    return _SemVer(
        release=tuple(int(match.group(name)) for name in ("major", "minor", "patch")),
        prerelease=parts,
    )


def _compare_rubygems(left: str, right: str) -> int:
    lhs = _rubygems_segments(left)
    rhs = _rubygems_segments(right)
    for left_part, right_part in zip_longest(lhs, rhs, fillvalue=0):
        if left_part == right_part:
            continue
        if isinstance(left_part, str) and isinstance(right_part, int):
            return -1
        if isinstance(left_part, int) and isinstance(right_part, str):
            return 1
        return _sign((left_part > right_part) - (left_part < right_part))
    return 0


def _rubygems_segments(value: str) -> tuple[int | str, ...]:
    normalized = value.replace("-", ".pre.")
    if _RUBYGEMS.fullmatch(normalized) is None:
        raise VersionComparisonError(f"invalid RubyGems version: {value!r}")
    segments: list[int | str] = [
        int(part) if part.isdigit() else part for part in _RUBYGEMS_SEGMENT.findall(normalized)
    ]
    string_start = next(
        (index for index, part in enumerate(segments) if isinstance(part, str)),
        len(segments),
    )
    numeric = segments[:string_start]
    prerelease = segments[string_start:]
    while numeric and numeric[-1] == 0:
        numeric.pop()
    while prerelease and prerelease[-1] == 0:
        prerelease.pop()
    return tuple([*numeric, *prerelease])


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)
