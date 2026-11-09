import pytest

from bumpire.models import VersionRequirement
from bumpire.versions import VersionComparisonError, satisfies


def _exact(version: str) -> VersionRequirement:
    return VersionRequirement(kind="exact", version=version)


def _at_least(version: str) -> VersionRequirement:
    return VersionRequirement(kind="at_least", version=version)


def test_exact_policy_uses_locked_version_identity() -> None:
    assert satisfies("1.2.3+build.1", _exact("1.2.3+build.1"), "semver")
    assert not satisfies("1.2.3+build.2", _exact("1.2.3+build.1"), "semver")
    assert not satisfies("1.0.0", _exact("1.0"), "rubygems")


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("1.0.0-alpha", "1.0.0-alpha.1"),
        ("1.0.0-alpha.1", "1.0.0-alpha.beta"),
        ("1.0.0-alpha.beta", "1.0.0-beta"),
        ("1.0.0-beta", "1.0.0-beta.2"),
        ("1.0.0-beta.2", "1.0.0-beta.11"),
        ("1.0.0-beta.11", "1.0.0-rc.1"),
        ("1.0.0-rc.1", "1.0.0"),
    ],
)
def test_semver_precedence(older: str, newer: str) -> None:
    assert satisfies(newer, _at_least(older), "semver")
    assert not satisfies(older, _at_least(newer), "semver")


def test_semver_build_metadata_does_not_change_precedence() -> None:
    assert satisfies("1.2.3+build.1", _at_least("1.2.3+build.2"), "semver")


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("0.9", "1.0.a.2"),
        ("1.0.a.2", "1.0.b1"),
        ("1.0.b1", "1.0"),
        ("3.2", "3.10"),
    ],
)
def test_rubygems_precedence(older: str, newer: str) -> None:
    assert satisfies(newer, _at_least(older), "rubygems")
    assert not satisfies(older, _at_least(newer), "rubygems")


def test_at_least_rejects_versions_outside_the_selected_scheme() -> None:
    with pytest.raises(VersionComparisonError, match="semantic"):
        satisfies("release-1", _at_least("1.0.0"), "semver")
