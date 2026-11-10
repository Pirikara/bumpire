import json
from pathlib import Path

import pytest

from bumpire.profile import detect_profile, load_builtin_profiles
from bumpire.profile_executor import ProfileExecutor


def _profiles_by_name():
    return {profile.name: profile for profile in load_builtin_profiles()}


def test_builtin_profiles_are_declarative_and_complete() -> None:
    profiles = _profiles_by_name()

    assert set(profiles) == {"bun", "bundler", "npm", "pnpm", "uv", "yarn"}
    assert "npm install --package-lock-only" in profiles["npm"].resolve_script
    assert "pnpm install --lockfile-only" in profiles["pnpm"].resolve_script
    assert "yarn install" in profiles["yarn"].resolve_script
    assert "bun install --lockfile-only" in profiles["bun"].resolve_script
    assert "bundle lock" in profiles["bundler"].resolve_script
    assert '"uv", "lock"' in profiles["uv"].resolve_script
    assert "JSON.stringify" in profiles["npm"].resolved_graph_script
    assert "Bundler::LockfileParser" in profiles["bundler"].resolved_graph_script
    assert "uv workspace metadata" in profiles["uv"].resolved_graph_script


@pytest.mark.parametrize(
    ("manager", "lockfile", "version", "expected_image"),
    [
        ("pnpm", "pnpm-lock.yaml", "12.14.0", "node:22-bookworm-slim"),
        ("yarn", "yarn.lock", "4.10.3", "node:22-bookworm-slim"),
        ("bun", "bun.lock", "1.4.4", "oven/bun:1.4.4-slim"),
    ],
)
def test_javascript_driver_detection_and_image_selection(
    tmp_path: Path,
    manager: str,
    lockfile: str,
    version: str,
    expected_image: str,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"packageManager": f"{manager}@{version}"}),
        encoding="utf-8",
    )
    (tmp_path / lockfile).write_text("{}\n", encoding="utf-8")

    profile = detect_profile(tmp_path, ".")

    assert profile.name == manager
    assert profile.manager_version(tmp_path) == version
    assert profile.image(tmp_path) == expected_image


def test_yarn_driver_rejects_repository_controlled_plugins(tmp_path: Path) -> None:
    profile = _profiles_by_name()["yarn"]
    (tmp_path / ".yarnrc.yml").write_text(
        "plugins:\n  - path: ./malicious-plugin.cjs\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="custom Yarn plugins"):
        profile.validate_workspace(tmp_path)


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


def test_uv_profile_extracts_versions_and_discovers_workspace_manifests(
    tmp_path: Path,
) -> None:
    member = tmp_path / "packages" / "member"
    member.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\ndependencies = ["idna>=3.9"]\n',
        encoding="utf-8",
    )
    (member / "pyproject.toml").write_text(
        '[project]\nname = "member"\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / ".python-version").write_text("3.13.2\n", encoding="utf-8")
    (tmp_path / "uv.toml").write_text(
        'required-version = "==0.13.7"\n',
        encoding="utf-8",
    )

    profile = detect_profile(tmp_path, ".")

    assert profile.name == "uv"
    assert profile.runtime_version(tmp_path) == "3.13"
    assert profile.manager_version(tmp_path) == "0.13.7"
    assert profile.image(tmp_path) == "ghcr.io/astral-sh/uv:0.13.7-python3.13-trixie-slim"
    assert [path.relative_to(tmp_path).as_posix() for path in profile.manifest_paths(tmp_path)] == [
        "packages/member/pyproject.toml",
        "pyproject.toml",
    ]


def test_driver_protocol_normalizes_resolved_versions() -> None:
    profile = _profiles_by_name()["npm"]

    versions = profile.parse_resolved_graph(
        json.dumps(
            {
                "schema_version": 1,
                "packages": {"lodash": ["4.18.1", "4.17.20", "4.18.1"]},
            }
        ),
        ["lodash"],
    )

    assert versions == {"lodash": ["4.17.20", "4.18.1"]}


def test_driver_protocol_rejects_missing_or_unexpected_targets() -> None:
    profile = _profiles_by_name()["npm"]

    with pytest.raises(ValueError, match="does not match requested targets"):
        profile.parse_resolved_graph(
            '{"schema_version":1,"packages":{"other":["1.0.0"]}}',
            ["lodash"],
        )


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


@pytest.mark.parametrize("profile_name", ["bun", "npm", "pnpm", "yarn"])
def test_javascript_manifest_policy_allows_dependencies_but_protects_scripts(
    profile_name: str,
) -> None:
    profile = _profiles_by_name()[profile_name]
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


@pytest.mark.parametrize("profile_name", ["bun", "npm", "pnpm", "yarn"])
def test_javascript_manifest_policy_rejects_new_protected_null_field(
    profile_name: str,
) -> None:
    profile = _profiles_by_name()[profile_name]

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


def test_uv_manifest_policy_allows_dependencies_but_protects_project_code() -> None:
    profile = _profiles_by_name()["uv"]
    original = """[project]
name = "example"
dependencies = ["Django==5.1.1", "idna>=3.9"]

[project.scripts]
example = "example:main"

[dependency-groups]
dev = ["pytest==8.3.0", { include-group = "lint" }]
lint = ["ruff==0.11.0"]

[tool.uv]
constraint-dependencies = ["urllib3<3"]

[[tool.uv.index]]
url = "https://pypi.org/simple"
"""
    dependency_update = """[project]
name = "example"
dependencies = ["Django==5.2.1", "idna>=3.9"]

[project.scripts]
example = "example:main"

[dependency-groups]
dev = ["pytest==8.4.0", { include-group = "lint" }]
lint = ["ruff==0.11.0"]

[tool.uv]
constraint-dependencies = ["urllib3<3"]
override-dependencies = ["asgiref==3.9.1"]

[[tool.uv.index]]
url = "https://pypi.org/simple"
"""

    profile.validate_manifest_replacement(original, dependency_update)

    with pytest.raises(ValueError, match="protected TOML fields"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace('example = "example:main"', 'example = "other:main"'),
        )
    with pytest.raises(ValueError, match="protected TOML fields"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace("https://pypi.org/simple", "https://example.invalid/simple"),
        )
    with pytest.raises(ValueError, match="declarations may not be removed"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(', "idna>=3.9"', ""),
        )
    with pytest.raises(ValueError, match="declarations may not be removed"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(', { include-group = "lint" }', ""),
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
