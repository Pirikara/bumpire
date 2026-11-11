import json
from pathlib import Path

import pytest

from bumpire.profile import detect_profile, load_builtin_profiles
from bumpire.profile_executor import ProfileExecutor


def _profiles_by_name():
    return {profile.name: profile for profile in load_builtin_profiles()}


def test_builtin_profiles_are_declarative_and_complete() -> None:
    profiles = _profiles_by_name()

    assert set(profiles) == {
        "bun",
        "bundler",
        "cargo",
        "composer",
        "dart-pub",
        "npm",
        "pdm",
        "pip-tools",
        "pnpm",
        "poetry",
        "uv",
        "yarn",
    }
    assert "npm install --package-lock-only" in profiles["npm"].resolve_script
    assert "pnpm install --lockfile-only" in profiles["pnpm"].resolve_script
    assert "yarn install" in profiles["yarn"].resolve_script
    assert "bun install --lockfile-only" in profiles["bun"].resolve_script
    assert "bundle lock" in profiles["bundler"].resolve_script
    assert "cargo', 'update'" in profiles["cargo"].resolve_script
    assert "'/tmp/composer.phar'" in profiles["composer"].resolve_script
    assert "override:$package@$version" in profiles["dart-pub"].resolve_script
    assert '"update"' in profiles["poetry"].resolve_script
    assert '"uv", "lock"' in profiles["uv"].resolve_script
    assert "JSON.stringify" in profiles["npm"].resolved_graph_script
    assert "Bundler::LockfileParser" in profiles["bundler"].resolved_graph_script
    assert 'open("poetry.lock"' in profiles["poetry"].resolved_graph_script
    assert 'open("pdm.lock"' in profiles["pdm"].resolved_graph_script
    assert '"update"' in profiles["pdm"].resolve_script
    assert '"pip-compile"' in profiles["pip-tools"].resolve_script
    assert "requirements*.in" in profiles["pip-tools"].resolved_graph_script
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


def test_cargo_profile_extracts_toolchain_and_discovers_workspace_manifests(
    tmp_path: Path,
) -> None:
    member = tmp_path / "crates" / "member"
    member.mkdir(parents=True)
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/member"]\n',
        encoding="utf-8",
    )
    (member / "Cargo.toml").write_text(
        '[package]\nname = "member"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (tmp_path / "rust-toolchain.toml").write_text(
        '[toolchain]\nchannel = "1.99.1"\n',
        encoding="utf-8",
    )

    profile = detect_profile(tmp_path, ".")

    assert profile.name == "cargo"
    assert profile.runtime_version(tmp_path) == "1.99.1"
    assert profile.manager_version(tmp_path) == "1.99.1"
    assert profile.image(tmp_path) == "rust:1.99.1-slim"
    assert [path.relative_to(tmp_path).as_posix() for path in profile.manifest_paths(tmp_path)] == [
        "Cargo.toml",
        "crates/member/Cargo.toml",
    ]


@pytest.mark.parametrize("config_name", ["config", "config.toml"])
def test_cargo_driver_rejects_repository_credential_providers(
    tmp_path: Path,
    config_name: str,
) -> None:
    profile = _profiles_by_name()["cargo"]
    config = tmp_path / ".cargo" / config_name
    config.parent.mkdir()
    config.write_text(
        '[registry]\nglobal-credential-providers = ["cargo:token", "cargo:custom"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="credential providers"):
        profile.validate_workspace(tmp_path)


def test_composer_profile_extracts_php_version_and_uses_pinned_composer(
    tmp_path: Path,
) -> None:
    (tmp_path / "composer.json").write_text(
        '{"name":"example/project","require":{"php":"^8.3"}}\n',
        encoding="utf-8",
    )
    (tmp_path / "composer.lock").write_text(
        '{"packages":[],"packages-dev":[]}\n',
        encoding="utf-8",
    )
    (tmp_path / ".php-version").write_text("8.3.12\n", encoding="utf-8")

    profile = detect_profile(tmp_path, ".")

    assert profile.name == "composer"
    assert profile.runtime_version(tmp_path) == "8.3"
    assert profile.manager_version(tmp_path) == "2.11.1"
    assert profile.image(tmp_path) == "php:8.3-cli-alpine"


def test_composer_profile_uses_configured_platform_php(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text(
        '{"name":"example/project","config":{"platform":{"php":"8.2.17"}}}\n',
        encoding="utf-8",
    )
    (tmp_path / "composer.lock").write_text(
        '{"packages":[],"packages-dev":[]}\n',
        encoding="utf-8",
    )

    profile = detect_profile(tmp_path, ".")

    assert profile.runtime_version(tmp_path) == "8.2"
    assert profile.image(tmp_path) == "php:8.2-cli-alpine"


def test_dart_pub_profile_extracts_sdk_version(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text(
        "name: example\nenvironment:\n  sdk: '>=3.8.0 <4.0.0'\n",
        encoding="utf-8",
    )
    (tmp_path / "pubspec.lock").write_text("packages: {}\n", encoding="utf-8")
    (tmp_path / ".dart-version").write_text("3.13.7\n", encoding="utf-8")

    profile = detect_profile(tmp_path, ".")

    assert profile.name == "dart-pub"
    assert profile.runtime_version(tmp_path) == "3.13.7"
    assert profile.image(tmp_path) == "dart:3.13.7"


def test_dart_pub_profile_rejects_flutter_sdk_projects(tmp_path: Path) -> None:
    profile = _profiles_by_name()["dart-pub"]
    (tmp_path / "pubspec.yaml").write_text(
        "name: example\ndependencies:\n  flutter:\n    sdk: flutter\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not include the Flutter SDK"):
        profile.validate_workspace(tmp_path)


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


def test_poetry_profile_detects_exact_required_version(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """[project]
name = "example"
version = "0.1.0"

[tool.poetry]
requires-poetry = "==2.5.1"
""",
        encoding="utf-8",
    )
    (tmp_path / "poetry.lock").write_text("package = []\n", encoding="utf-8")

    profile = detect_profile(tmp_path, ".")

    assert profile.name == "poetry"
    assert profile.manager_version(tmp_path) == "2.5.1"
    assert profile.image(tmp_path) == "python:3.12-slim"


def test_pdm_profile_detects_lockfile_and_uses_pinned_default(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "pdm.lock").write_text(
        '[metadata]\ngroups = ["default"]\n\npackage = []\n',
        encoding="utf-8",
    )

    profile = detect_profile(tmp_path, ".")

    assert profile.name == "pdm"
    assert profile.manager_version(tmp_path) == "2.29.7"
    assert profile.image(tmp_path) == "python:3.12-slim"


def test_pip_tools_profile_detects_runtime_and_multiple_lockfiles(tmp_path: Path) -> None:
    (tmp_path / "requirements.in").write_text("requests>=2\n", encoding="utf-8")
    (tmp_path / "requirements-dev.in").write_text("pytest>=8\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("pytest==8.4.0\n", encoding="utf-8")
    (tmp_path / ".python-version").write_text("3.13.2\n", encoding="utf-8")

    profile = detect_profile(tmp_path, ".")

    assert profile.name == "pip-tools"
    assert profile.runtime_version(tmp_path) == "3.13"
    assert profile.manager_version(tmp_path) == "7.6.2"
    assert profile.image(tmp_path) == "python:3.13-slim"
    assert [path.name for path in profile.lockfile_paths(tmp_path)] == [
        "requirements-dev.txt",
        "requirements.txt",
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


def test_cargo_manifest_policy_allows_only_existing_dependency_versions() -> None:
    profile = _profiles_by_name()["cargo"]
    original = """[package]
name = "example"
version = "0.1.0"
rust-version = "1.85"

[dependencies]
serde = "1.0.210"
json = { package = "serde_json", version = "1.0.138", features = ["preserve_order"] }

[dev-dependencies]
tempfile = "3.14.0"

[workspace.dependencies]
tracing = { version = "0.1.40", default-features = false }

[target.'cfg(unix)'.dependencies]
libc = { version = "0.2.160", optional = true }

[patch.crates-io]
itoa = { version = "1.0.14" }
"""
    dependency_update = (
        original.replace('serde = "1.0.210"', 'serde = "1.0.215"')
        .replace('version = "1.0.138"', 'version = "1.0.140"')
        .replace('tempfile = "3.14.0"', 'tempfile = "3.15.0"')
        .replace('version = "0.1.40"', 'version = "0.1.41"')
        .replace('version = "0.2.160"', 'version = "0.2.161"')
        .replace('version = "1.0.14"', 'version = "1.0.15"')
    )

    profile.validate_manifest_replacement(original, dependency_update)

    with pytest.raises(ValueError, match="protected TOML field"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace('rust-version = "1.85"', 'rust-version = "1.86"'),
        )
    with pytest.raises(ValueError, match="source or selector fields"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(
                'features = ["preserve_order"]',
                'features = ["preserve_order", "raw_value"]',
            ),
        )
    with pytest.raises(ValueError, match="declarations may not be removed"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace('serde = "1.0.215"\n', ""),
        )
    with pytest.raises(ValueError, match="declarations may not be added"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(
                'serde = "1.0.215"',
                'serde = "1.0.215"\nregex = "1.11.0"',
            ),
        )
    with pytest.raises(ValueError, match="declarations may not be added"):
        profile.validate_manifest_replacement(
            original,
            dependency_update + '\n[build-dependencies]\ncc = "1.2.0"\n',
        )
    with pytest.raises(ValueError, match="source or selector fields"):
        existing = (
            'json = { package = "serde_json", version = "1.0.140", '
            'features = ["preserve_order"] }'
        )
        changed = (
            'json = { package = "serde_json", version = "1.0.140", '
            'path = "../json", features = ["preserve_order"] }'
        )
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(existing, changed),
        )


def test_composer_manifest_policy_allows_only_existing_package_constraints() -> None:
    profile = _profiles_by_name()["composer"]
    original = json.dumps(
        {
            "name": "example/project",
            "require": {
                "php": "^8.2",
                "ext-json": "*",
                "monolog/monolog": "^3.8",
            },
            "require-dev": {"phpunit/phpunit": "^11.0"},
            "scripts": {"test": "phpunit"},
            "repositories": [{"type": "composer", "url": "https://repo.packagist.org"}],
        }
    )
    dependency_update = json.dumps(
        {
            "name": "example/project",
            "require": {
                "php": "^8.2",
                "ext-json": "*",
                "monolog/monolog": "^3.9",
            },
            "require-dev": {"phpunit/phpunit": "^11.5"},
            "scripts": {"test": "phpunit"},
            "repositories": [{"type": "composer", "url": "https://repo.packagist.org"}],
        }
    )

    profile.validate_manifest_replacement(original, dependency_update)

    with pytest.raises(ValueError, match="protected JSON fields"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace("phpunit", "curl example.invalid"),
        )
    with pytest.raises(ValueError, match="dependencies may not be added"):
        added = json.loads(dependency_update)
        added["require"]["psr/log"] = "^3.0"
        profile.validate_manifest_replacement(original, json.dumps(added))
    with pytest.raises(ValueError, match="dependencies may not be added"):
        without_dev = json.loads(original)
        without_dev.pop("require-dev")
        with_new_section = json.loads(dependency_update)
        profile.validate_manifest_replacement(json.dumps(without_dev), json.dumps(with_new_section))
    with pytest.raises(ValueError, match="dependencies may not be removed"):
        removed = json.loads(dependency_update)
        removed["require"].pop("monolog/monolog")
        profile.validate_manifest_replacement(original, json.dumps(removed))
    with pytest.raises(ValueError, match="protected JSON dependencies"):
        platform_update = json.loads(dependency_update)
        platform_update["require"]["php"] = "^8.3"
        profile.validate_manifest_replacement(original, json.dumps(platform_update))


def test_dart_pub_manifest_policy_allows_only_existing_hosted_versions() -> None:
    profile = _profiles_by_name()["dart-pub"]
    original = """name: example
version: 1.0.0

environment:
  sdk: '>=3.8.0 <4.0.0'

dependencies:
  http: '>=1.2.0 <=1.3.0'
  local_package:
    path: ../local_package

dev_dependencies:
  lints: ^5.0.0 # keep this comment

dependency_overrides:
  meta: 1.18.0
"""
    dependency_update = original.replace(
        "http: '>=1.2.0 <=1.3.0'",
        "http: '1.3.0'",
    ).replace("lints: ^5.0.0", "lints: ^6.0.0")

    profile.validate_manifest_replacement(original, dependency_update)

    with pytest.raises(ValueError, match="protected YAML fields"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(">=3.8.0 <4.0.0", ">=3.9.0 <4.0.0"),
        )
    with pytest.raises(ValueError, match="dependencies may not be added"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(
                "  http: '1.3.0'",
                "  http: '1.3.0'\n  collection: ^1.19.0",
            ),
        )
    with pytest.raises(ValueError, match="dependencies may not be removed"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace("  http: '1.3.0'\n", ""),
        )
    with pytest.raises(ValueError, match="source or selector fields"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace("path: ../local_package", "path: ../other_package"),
        )
    with pytest.raises(ValueError, match="protected YAML fields"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace("meta: 1.18.0", "meta: 1.19.0"),
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
    with pytest.raises(ValueError, match="source declarations may not change"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(
                '"idna>=3.9"',
                '"idna @ https://example.invalid/idna.whl"',
            ),
        )


def test_poetry_manifest_policy_supports_dependency_tables_and_protects_config() -> None:
    profile = _profiles_by_name()["poetry"]
    original = """[project]
name = "example"
dependencies = ["Django==5.1.1", "idna>=3.9"]

[project.scripts]
example = "example:main"

[tool.poetry.dependencies]
python = ">=3.12"
requests = "^2.31"

[tool.poetry.group.dev]
optional = true

[tool.poetry.group.dev.dependencies]
pytest = "8.3.0"

[[tool.poetry.source]]
name = "pypi"
priority = "primary"
"""
    dependency_update = """[project]
name = "example"
dependencies = ["Django==5.2.1", "idna>=3.9"]

[project.scripts]
example = "example:main"

[tool.poetry.dependencies]
python = ">=3.12"
requests = "^2.32"
urllib3 = "2.6.0"

[tool.poetry.group.dev]
optional = true

[tool.poetry.group.dev.dependencies]
pytest = "8.4.0"

[[tool.poetry.source]]
name = "pypi"
priority = "primary"
"""

    profile.validate_manifest_replacement(original, dependency_update)

    protected_replacements = [
        dependency_update.replace('example = "example:main"', 'example = "other:main"'),
        dependency_update.replace('python = ">=3.12"', 'python = ">=3.13"'),
        dependency_update.replace("priority = \"primary\"", "priority = \"supplemental\""),
        dependency_update.replace("optional = true", "optional = false"),
    ]
    for replacement in protected_replacements:
        with pytest.raises(ValueError, match="protected TOML field"):
            profile.validate_manifest_replacement(original, replacement)

    with pytest.raises(ValueError, match="declarations may not be removed"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace('requests = "^2.32"\n', ""),
        )
    with pytest.raises(ValueError, match="declarations may not be removed"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace('pytest = "8.4.0"\n', ""),
        )
    with pytest.raises(ValueError, match="declarations may not be removed"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(', "idna>=3.9"', ""),
        )
    with pytest.raises(ValueError, match="source or selector fields may not change"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(
                'requests = "^2.32"',
                'requests = { version = "^2.32", source = "private" }',
            ),
        )
    with pytest.raises(ValueError, match="registry version constraint"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(
                'urllib3 = "2.6.0"',
                'urllib3 = { git = "https://example.invalid/urllib3.git" }',
            ),
        )
    with pytest.raises(ValueError, match="source declarations may not change"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(
                '"idna>=3.9"',
                '"idna @ https://example.invalid/idna.whl"',
            ),
        )

    private_original = original.replace(
        'requests = "^2.31"',
        'requests = { version = "^2.31", source = "private" }',
    )
    private_update = dependency_update.replace(
        'requests = "^2.32"',
        'requests = { version = "^2.32", source = "private" }',
    )
    profile.validate_manifest_replacement(private_original, private_update)
    with pytest.raises(ValueError, match="source or selector fields may not change"):
        profile.validate_manifest_replacement(
            private_original,
            private_update.replace('source = "private"', 'source = "other"'),
        )


def test_pdm_manifest_policy_supports_dependency_groups_and_protects_config() -> None:
    profile = _profiles_by_name()["pdm"]
    original = """[project]
name = "example"
requires-python = ">=3.12"
dependencies = ["Django==5.1.1", "idna>=3.9"]

[project.optional-dependencies]
docs = ["mkdocs==1.6.0"]

[project.scripts]
example = "example:main"

[dependency-groups]
dev = ["pytest==8.3.0", { include-group = "lint" }]
lint = ["ruff==0.11.0"]

[tool.pdm]
distribution = false

[tool.pdm.resolution]
respect-source-order = true

[tool.pdm.resolution.overrides]
urllib3 = "2.5.0"

[tool.pdm.dev-dependencies]
legacy = ["coverage==7.6.0"]

[[tool.pdm.source]]
name = "pypi"
url = "https://pypi.org/simple"
"""
    dependency_update = original.replace("Django==5.1.1", "Django==5.2.1")
    dependency_update = dependency_update.replace("mkdocs==1.6.0", "mkdocs==1.6.1")
    dependency_update = dependency_update.replace("pytest==8.3.0", "pytest==8.4.0")
    dependency_update = dependency_update.replace("ruff==0.11.0", "ruff==0.12.0")
    dependency_update = dependency_update.replace("urllib3 = \"2.5.0\"", "urllib3 = \"2.6.0\"")
    dependency_update = dependency_update.replace("coverage==7.6.0", "coverage==7.7.0")

    profile.validate_manifest_replacement(original, dependency_update)

    protected_replacements = [
        dependency_update.replace('requires-python = ">=3.12"', 'requires-python = ">=3.13"'),
        dependency_update.replace('example = "example:main"', 'example = "other:main"'),
        dependency_update.replace("respect-source-order = true", "respect-source-order = false"),
        dependency_update.replace("https://pypi.org/simple", "https://example.invalid/simple"),
    ]
    for replacement in protected_replacements:
        with pytest.raises(ValueError, match="protected TOML field"):
            profile.validate_manifest_replacement(original, replacement)

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
    with pytest.raises(ValueError, match="source declarations may not change"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(
                '"idna>=3.9"',
                '"idna @ https://example.invalid/idna.whl"',
            ),
        )
    with pytest.raises(ValueError, match="source or selector fields may not change"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(
                'urllib3 = "2.6.0"',
                'urllib3 = { version = "2.6.0", url = "https://example.invalid/pkg.whl" }',
            ),
        )


def test_pip_tools_manifest_policy_allows_versions_but_protects_sources() -> None:
    profile = _profiles_by_name()["pip-tools"]
    original = """--index-url https://pypi.org/simple
-r base.in

Django==5.1.1
idna>=3.9
packaging
"""
    dependency_update = original.replace("Django==5.1.1", "Django==5.2.1")
    dependency_update = dependency_update.replace("packaging\n", "packaging==24.1\n")

    profile.validate_manifest_replacement(original, dependency_update)

    with pytest.raises(ValueError, match="outside the profile policy"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace("https://pypi.org/simple", "https://example.invalid/simple"),
        )
    with pytest.raises(ValueError, match="outside the profile policy"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace(
                "idna>=3.9",
                "idna @ https://example.invalid/idna.whl",
            ),
        )
    with pytest.raises(ValueError, match="declarations may not be removed"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace("idna>=3.9\n", ""),
        )
    with pytest.raises(ValueError, match="declarations may not be added"):
        profile.validate_manifest_replacement(
            original,
            dependency_update.replace("packaging==24.1\n", "packaging==24.1\nurllib3<3\n"),
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
