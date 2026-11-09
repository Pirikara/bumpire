import json
from pathlib import Path

import pytest

from bumpire.profile import detect_profile, load_builtin_profiles
from bumpire.profile_executor import ProfileExecutor


def _profiles_by_name():
    return {profile.name: profile for profile in load_builtin_profiles()}


def test_builtin_profiles_are_declarative_and_complete() -> None:
    profiles = _profiles_by_name()

    assert set(profiles) == {"bundler", "npm"}
    assert profiles["npm"].version_scheme == "semver"
    assert profiles["bundler"].version_scheme == "rubygems"
    assert "npm install --package-lock-only" in profiles["npm"].resolver_script
    assert "bundle lock" in profiles["bundler"].resolver_script


def test_npm_profile_detects_versions_manifests_and_locked_target(tmp_path: Path) -> None:
    nested = tmp_path / "packages" / "peer"
    nested.mkdir(parents=True)
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "engines": {"node": "22.19.0"},
                "packageManager": "npm@10.9.3",
            }
        ),
        encoding="utf-8",
    )
    (nested / "package.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/lodash": {"version": "4.17.20"}},
            }
        ),
        encoding="utf-8",
    )

    profile = detect_profile(tmp_path, ".")

    assert profile.name == "npm"
    assert profile.runtime_version(tmp_path) == "22.19.0"
    assert profile.manager_version(tmp_path) == "10.9.3"
    assert profile.image(tmp_path) == "node:22.19.0-bookworm-slim"
    assert [path.relative_to(tmp_path).as_posix() for path in profile.manifest_paths(tmp_path)] == [
        "package.json",
        "packages/peer/package.json",
    ]
    assert profile.locked_versions(
        (tmp_path / "package-lock.json").read_text(encoding="utf-8"),
        "lodash",
    ) == ["4.17.20"]


def test_bundler_profile_extracts_versions_and_locked_target(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text('gem "rails", "7.2.3.1"\n', encoding="utf-8")
    (tmp_path / ".ruby-version").write_text("ruby-3.3.10\n", encoding="utf-8")
    (tmp_path / "Gemfile.lock").write_text(
        """GEM
  specs:
    activestorage (7.2.3.1)

BUNDLED WITH
   4.0.10
""",
        encoding="utf-8",
    )

    profile = detect_profile(tmp_path, ".")

    assert profile.name == "bundler"
    assert profile.runtime_version(tmp_path) == "ruby-3.3.10"
    assert profile.manager_version(tmp_path) == "4.0.10"
    assert profile.image(tmp_path) == "ruby:3.3.10-slim"
    assert profile.locked_versions(
        (tmp_path / "Gemfile.lock").read_text(encoding="utf-8"),
        "activestorage",
    ) == ["7.2.3.1"]


def test_bundler_profile_excludes_platform_from_locked_version() -> None:
    profile = _profiles_by_name()["bundler"]

    assert profile.locked_versions(
        "    nokogiri (1.18.10-arm64-darwin)\n",
        "nokogiri",
    ) == ["1.18.10"]


def test_npm_profile_extracts_every_nested_locked_occurrence() -> None:
    profile = _profiles_by_name()["npm"]
    lockfile = json.dumps(
        {
            "packages": {
                "node_modules/lodash": {"version": "4.18.1"},
                "node_modules/consumer/node_modules/lodash": {"version": "4.17.20"},
            }
        }
    )

    assert profile.locked_versions(lockfile, "lodash") == ["4.18.1", "4.17.20"]


def test_profile_detection_rejects_unknown_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no declarative package-manager profile"):
        detect_profile(tmp_path, ".")


def test_explicit_ecosystem_disambiguates_mixed_workspace(tmp_path: Path) -> None:
    for filename in ("package.json", "package-lock.json"):
        (tmp_path / filename).write_text("{}\n", encoding="utf-8")
    (tmp_path / "Gemfile").write_text('gem "rails"\n', encoding="utf-8")
    (tmp_path / "Gemfile.lock").write_text("GEM\n", encoding="utf-8")

    with pytest.raises(ValueError, match="multiple package-manager profiles"):
        detect_profile(tmp_path, ".")
    assert detect_profile(tmp_path, ".", "npm").name == "npm"
    assert detect_profile(tmp_path, ".", "bundler").name == "bundler"


def test_npm_manifest_policy_allows_dependencies_but_protects_scripts() -> None:
    profile = _profiles_by_name()["npm"]
    original = json.dumps({"scripts": {"test": "pytest"}, "dependencies": {"lodash": "4.17.20"}})
    dependency_update = json.dumps(
        {"scripts": {"test": "pytest"}, "dependencies": {"lodash": "4.18.1"}}
    )
    script_update = json.dumps(
        {"scripts": {"test": "curl example.invalid"}, "dependencies": {"lodash": "4.18.1"}}
    )
    dependency_removal = json.dumps({"scripts": {"test": "pytest"}, "dependencies": {}})

    profile.validate_manifest_replacement(original, dependency_update)
    with pytest.raises(ValueError, match="protected JSON fields"):
        profile.validate_manifest_replacement(original, script_update)
    with pytest.raises(ValueError, match="dependencies may not be removed"):
        profile.validate_manifest_replacement(original, dependency_removal)


def test_npm_manifest_policy_rejects_new_protected_null_field() -> None:
    profile = _profiles_by_name()["npm"]

    with pytest.raises(ValueError, match="protected JSON fields"):
        profile.validate_manifest_replacement(
            '{"dependencies":{"lodash":"4.17.20"}}',
            '{"scripts":null,"dependencies":{"lodash":"4.18.1"}}',
        )


def test_bundler_manifest_policy_allows_gems_but_protects_sources() -> None:
    profile = _profiles_by_name()["bundler"]
    original = 'source "https://rubygems.org"\n\ngem "rails", "7.2.3.1"\ngem "puma", "6.4.0"\n'

    profile.validate_manifest_replacement(
        original,
        'source "https://rubygems.org"\n\ngem "rails", "7.2.3.2"\ngem "puma", "6.4.0"\n',
    )
    with pytest.raises(ValueError, match="outside the profile policy"):
        profile.validate_manifest_replacement(
            original,
            'source "https://example.invalid"\n\ngem "rails", "7.2.3.2"\ngem "puma", "6.4.0"\n',
        )
    with pytest.raises(ValueError, match="declarations may not be removed"):
        profile.validate_manifest_replacement(
            original,
            'source "https://rubygems.org"\n\ngem "rails", "7.2.3.2"\n',
        )


@pytest.mark.parametrize(
    ("original", "replacement", "expected"),
    [
        ("old\n", "new", "new\n"),
        ("old", "new\n", "new"),
        ("old\n", "new\n", "new\n"),
    ],
)
def test_profile_executor_preserves_final_newline(
    original: str,
    replacement: str,
    expected: str,
) -> None:
    assert ProfileExecutor._preserve_final_newline(original, replacement) == expected
