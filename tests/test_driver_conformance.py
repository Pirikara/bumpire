import json
import os
from dataclasses import dataclass
from io import StringIO
from pathlib import Path, PurePosixPath

import pytest

from bumpire.docker_runtime import ContainerCommand, DockerRuntime
from bumpire.events import EventLogger
from bumpire.profile import load_builtin_profiles


@dataclass(frozen=True)
class DriverCase:
    id: str
    profile: str
    files: dict[str, str]
    targets: list[str]
    expected: dict[str, list[str]]
    updates: list[dict[str, str]] | None = None
    expected_after: dict[str, list[str]] | None = None


_CASES = [
    DriverCase(
        id="npm-v3",
        profile="npm",
        files={
            "package.json": '{"engines":{"node":"22.19.0"}}',
            "package-lock.json": json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "node_modules/lodash": {"version": "4.18.1"},
                        "node_modules/consumer/node_modules/lodash": {
                            "version": "4.17.20"
                        },
                        "node_modules/@scope/pkg": {"version": "1.2.3"},
                    },
                }
            ),
        },
        targets=["lodash", "@scope/pkg"],
        expected={"lodash": ["4.17.20", "4.18.1"], "@scope/pkg": ["1.2.3"]},
    ),
    DriverCase(
        id="bundler-platform",
        profile="bundler",
        files={
            "Gemfile": 'gem "nokogiri"\n',
            "Gemfile.lock": """GEM
  specs:
    nokogiri (1.18.10-arm64-darwin)

BUNDLED WITH
   4.0.10
""",
            ".ruby-version": "3.3.10\n",
        },
        targets=["nokogiri"],
        expected={"nokogiri": ["1.18.10"]},
    ),
    DriverCase(
        id="cargo-workspace",
        profile="cargo",
        files={
            filename: (Path(__file__).parent / "fixtures" / "cargo" / filename).read_text(
                encoding="utf-8"
            )
            for filename in (
                "Cargo.toml",
                "Cargo.lock",
                "app/Cargo.toml",
                "app/src/lib.rs",
            )
        },
        targets=["serde_json", "itoa"],
        expected={"serde_json": ["1.0.138"], "itoa": ["1.0.14"]},
        updates=[
            {"package": "serde_json", "version": "1.0.140"},
            {"package": "itoa", "version": "1.0.15"},
        ],
        expected_after={"serde_json": ["1.0.140"], "itoa": ["1.0.15"]},
    ),
    DriverCase(
        id="composer-lock",
        profile="composer",
        files={
            filename: (Path(__file__).parent / "fixtures" / "composer" / filename).read_text(
                encoding="utf-8"
            )
            for filename in (".php-version", "composer.json", "composer.lock")
        },
        targets=["monolog/monolog", "psr/log", "psr/container"],
        expected={
            "monolog/monolog": ["3.8.1"],
            "psr/log": ["3.0.0"],
            "psr/container": ["2.0.1"],
        },
        updates=[
            {"package": "monolog/monolog", "version": "3.9.0"},
            {"package": "psr/log", "version": "3.0.2"},
            {"package": "psr/container", "version": "2.0.2"},
        ],
        expected_after={
            "monolog/monolog": ["3.9.0"],
            "psr/log": ["3.0.2"],
            "psr/container": ["2.0.2"],
        },
    ),
    DriverCase(
        id="dart-pub-lock",
        profile="dart-pub",
        files={
            filename: (Path(__file__).parent / "fixtures" / "dart-pub" / filename).read_text(
                encoding="utf-8"
            )
            for filename in (".dart-version", "pubspec.yaml", "pubspec.lock")
        },
        targets=["http", "http_parser", "lints"],
        expected={"http": ["1.2.0"], "http_parser": ["4.0.2"], "lints": ["5.0.0"]},
        updates=[
            {"package": "http", "version": "1.3.0"},
            {"package": "http_parser", "version": "4.1.2"},
            {"package": "lints", "version": "6.0.0"},
        ],
        expected_after={
            "http": ["1.3.0"],
            "http_parser": ["4.1.2"],
            "lints": ["6.0.0"],
        },
    ),
    DriverCase(
        id="pnpm-v9",
        profile="pnpm",
        files={
            "package.json": '{"packageManager":"pnpm@12.14.0"}',
            "pnpm-lock.yaml": """lockfileVersion: '9.0'

importers:
  .: {}

packages:
  '@scope/pkg@1.2.3': {}
  lodash@4.17.20: {}
  lodash@4.18.1:
    resolution: {integrity: sha512-example}

snapshots:
  '@scope/pkg@1.2.3': {}
  lodash@4.17.20: {}
  lodash@4.18.1: {}
""",
        },
        targets=["lodash", "@scope/pkg"],
        expected={"lodash": ["4.17.20", "4.18.1"], "@scope/pkg": ["1.2.3"]},
    ),
    DriverCase(
        id="yarn-classic",
        profile="yarn",
        files={
            "package.json": '{"packageManager":"yarn@1.22.22"}',
            "yarn.lock": """# yarn lockfile v1

"@scope/pkg@^1.0.0":
  version "1.2.3"

lodash@^4.17.0:
  version "4.17.20"
""",
        },
        targets=["lodash", "@scope/pkg"],
        expected={"lodash": ["4.17.20"], "@scope/pkg": ["1.2.3"]},
    ),
    DriverCase(
        id="yarn-berry",
        profile="yarn",
        files={
            "package.json": '{\n  "packageManager": "yarn@4.10.3"\n}\n',
            "yarn.lock": """__metadata:
  version: 8

"@scope/pkg@npm:^1.0.0":
  version: 1.2.3
  resolution: "@scope/pkg@npm:1.2.3"
  languageName: node
  linkType: hard

"lodash@npm:^4.17.0":
  version: 4.18.1
  resolution: "lodash@npm:4.18.1"
  languageName: node
  linkType: hard

"lodash@virtual:example#npm:^4.17.0":
  version: 4.18.1
  resolution: "lodash@virtual:example#npm:4.18.1"
  languageName: node
  linkType: hard
""",
        },
        targets=["lodash", "@scope/pkg"],
        expected={"lodash": ["4.18.1"], "@scope/pkg": ["1.2.3"]},
    ),
    DriverCase(
        id="bun-v1",
        profile="bun",
        files={
            "package.json": '{"packageManager":"bun@1.4.4"}',
            "bun.lock": """{
  "lockfileVersion": 1,
  "packages": {
    "@scope/pkg": ["@scope/pkg@1.2.3", "", {}, ""],
    "lodash": ["lodash@4.18.1", "", {}, ""],
  },
}
""",
        },
        targets=["lodash", "@scope/pkg"],
        expected={"lodash": ["4.18.1"], "@scope/pkg": ["1.2.3"]},
    ),
    DriverCase(
        id="uv-workspace-metadata",
        profile="uv",
        files={
            "pyproject.toml": (
                Path(__file__).parent / "fixtures" / "uv" / "pyproject.toml"
            ).read_text(encoding="utf-8"),
            "uv.lock": (Path(__file__).parent / "fixtures" / "uv" / "uv.lock").read_text(
                encoding="utf-8"
            ),
            ".python-version": "3.12\n",
        },
        targets=["IDNA", "packaging"],
        expected={"IDNA": ["3.9"], "packaging": ["24.0"]},
        updates=[
            {"package": "idna", "version": "3.10"},
            {"package": "packaging", "version": "24.1"},
        ],
        expected_after={"idna": ["3.10"], "packaging": ["24.1"]},
    ),
    DriverCase(
        id="poetry-lock-v2",
        profile="poetry",
        files={
            "pyproject.toml": (
                Path(__file__).parent / "fixtures" / "poetry" / "pyproject.toml"
            ).read_text(encoding="utf-8"),
            "poetry.lock": (
                Path(__file__).parent / "fixtures" / "poetry" / "poetry.lock"
            ).read_text(encoding="utf-8"),
        },
        targets=["IDNA", "packaging"],
        expected={"IDNA": ["3.9"], "packaging": ["24.0"]},
        updates=[
            {"package": "idna", "version": "3.10"},
            {"package": "packaging", "version": "24.1"},
        ],
        expected_after={"idna": ["3.10"], "packaging": ["24.1"]},
    ),
    DriverCase(
        id="pdm-lock-v4",
        profile="pdm",
        files={
            "pyproject.toml": (
                Path(__file__).parent / "fixtures" / "pdm" / "pyproject.toml"
            ).read_text(encoding="utf-8"),
            "pdm.lock": (
                Path(__file__).parent / "fixtures" / "pdm" / "pdm.lock"
            ).read_text(encoding="utf-8"),
            ".pdm-python": "/repository/interpreter-must-remain-unchanged\n",
        },
        targets=["IDNA", "packaging"],
        expected={"IDNA": ["3.9"], "packaging": ["24.0"]},
        updates=[
            {"package": "idna", "version": "3.10"},
            {"package": "packaging", "version": "24.1"},
        ],
        expected_after={"idna": ["3.10"], "packaging": ["24.1"]},
    ),
    DriverCase(
        id="pip-tools-requirements",
        profile="pip-tools",
        files={
            filename: (Path(__file__).parent / "fixtures" / "pip-tools" / filename).read_text(
                encoding="utf-8"
            )
            for filename in (
                ".python-version",
                "requirements.in",
                "requirements.txt",
                "requirements-dev.in",
                "requirements-dev.txt",
            )
        },
        targets=["IDNA", "packaging"],
        expected={"IDNA": ["3.9"], "packaging": ["24.0"]},
        updates=[
            {"package": "idna", "version": "3.10"},
            {"package": "packaging", "version": "24.1"},
        ],
        expected_after={"idna": ["3.10"], "packaging": ["24.1"]},
    ),
]

_RESOLVER_CASES = [
    case
    for case in _CASES
    if case.profile in {
        "bun",
        "cargo",
        "composer",
        "dart-pub",
        "pdm",
        "pip-tools",
        "pnpm",
        "poetry",
        "uv",
        "yarn",
    }
]


def _write_fixture(root: Path, files: dict[str, str]) -> None:
    (root / ".git").mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.mark.driver
@pytest.mark.skipif(
    os.environ.get("BUMPIRE_DRIVER_CONFORMANCE") != "1",
    reason="set BUMPIRE_DRIVER_CONFORMANCE=1 to run Docker driver contracts",
)
@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.id)
def test_resolved_graph_driver_contract(case: DriverCase, tmp_path: Path) -> None:
    profiles = {profile.name: profile for profile in load_builtin_profiles()}
    profile = profiles[case.profile]
    _write_fixture(tmp_path, case.files)

    result = DockerRuntime(EventLogger(stream=StringIO())).run(
        ContainerCommand(
            image=profile.image(tmp_path),
            args=("sh", "-ceu", profile.resolved_graph_script, "bumpire-driver"),
            environment={"BUMPIRE_TARGETS_JSON": json.dumps(case.targets)},
            network="none",
            writable_workspace=False,
        ),
        clone_root=tmp_path,
        workspace=PurePosixPath("."),
        timeout=120,
    )

    assert result.ok, result.stderr or result.stdout
    assert profile.parse_resolved_graph(result.stdout, case.targets) == case.expected


@pytest.mark.driver
@pytest.mark.skipif(
    os.environ.get("BUMPIRE_DRIVER_CONFORMANCE") != "1",
    reason="set BUMPIRE_DRIVER_CONFORMANCE=1 to run Docker driver contracts",
)
@pytest.mark.parametrize("case", _RESOLVER_CASES, ids=lambda case: case.id)
def test_driver_resolver_contract(case: DriverCase, tmp_path: Path) -> None:
    profiles = {profile.name: profile for profile in load_builtin_profiles()}
    profile = profiles[case.profile]
    _write_fixture(tmp_path, case.files)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    lockfiles_before = {
        path.relative_to(tmp_path).as_posix()
        for path in profile.lockfile_paths(tmp_path)
    }

    result = DockerRuntime(EventLogger(stream=StringIO())).run(
        ContainerCommand(
            image=profile.image(tmp_path),
            args=("sh", "-ceu", profile.resolve_script, "bumpire-driver"),
            environment={
                "BUMPIRE_MANAGER_VERSION": profile.manager_version(tmp_path),
                "BUMPIRE_UPDATE_TARGETS_JSON": json.dumps(case.updates or []),
            },
        ),
        clone_root=tmp_path,
        workspace=PurePosixPath("."),
        timeout=600,
    )

    assert result.ok, result.stderr or result.stdout
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    changed = {
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    }
    lockfiles_after = {
        path.relative_to(tmp_path).as_posix()
        for path in profile.lockfile_paths(tmp_path)
    }
    allowed_lockfiles = lockfiles_before | lockfiles_after
    assert changed <= allowed_lockfiles
    if case.profile == "pip-tools":
        assert b"--hash=" in after["requirements-dev.txt"]

    if case.expected_after is not None:
        targets = list(case.expected_after)
        graph = DockerRuntime(EventLogger(stream=StringIO())).run(
            ContainerCommand(
                image=profile.image(tmp_path),
                args=("sh", "-ceu", profile.resolved_graph_script, "bumpire-driver"),
                environment={"BUMPIRE_TARGETS_JSON": json.dumps(targets)},
                network="none",
                writable_workspace=False,
            ),
            clone_root=tmp_path,
            workspace=PurePosixPath("."),
            timeout=120,
        )
        assert graph.ok, graph.stderr or graph.stdout
        assert profile.parse_resolved_graph(graph.stdout, targets) == case.expected_after


@pytest.mark.driver
@pytest.mark.skipif(
    os.environ.get("BUMPIRE_DRIVER_CONFORMANCE") != "1",
    reason="set BUMPIRE_DRIVER_CONFORMANCE=1 to run Docker driver contracts",
)
def test_pdm_resolver_rechecks_compatibility_without_exact_override(tmp_path: Path) -> None:
    profile = {profile.name: profile for profile in load_builtin_profiles()}["pdm"]
    fixture = Path(__file__).parent / "fixtures" / "pdm"
    _write_fixture(
        tmp_path,
        {
            "pyproject.toml": (fixture / "pyproject.toml").read_text(encoding="utf-8"),
            "pdm.lock": (fixture / "pdm.lock").read_text(encoding="utf-8"),
        },
    )

    result = DockerRuntime(EventLogger(stream=StringIO())).run(
        ContainerCommand(
            image=profile.image(tmp_path),
            args=("sh", "-ceu", profile.resolve_script, "bumpire-driver"),
            environment={
                "BUMPIRE_MANAGER_VERSION": profile.manager_version(tmp_path),
                "BUMPIRE_UPDATE_TARGETS_JSON": json.dumps(
                    [{"package": "idna", "version": "2.0"}]
                ),
            },
        ),
        clone_root=tmp_path,
        workspace=PurePosixPath("."),
        timeout=600,
    )

    if not result.ok:
        return

    graph = DockerRuntime(EventLogger(stream=StringIO())).run(
        ContainerCommand(
            image=profile.image(tmp_path),
            args=("sh", "-ceu", profile.resolved_graph_script, "bumpire-driver"),
            environment={"BUMPIRE_TARGETS_JSON": '["idna"]'},
            network="none",
            writable_workspace=False,
        ),
        clone_root=tmp_path,
        workspace=PurePosixPath("."),
        timeout=120,
    )
    assert graph.ok, graph.stderr or graph.stdout
    assert profile.parse_resolved_graph(graph.stdout, ["idna"]) != {"idna": ["2.0"]}


@pytest.mark.driver
@pytest.mark.skipif(
    os.environ.get("BUMPIRE_DRIVER_CONFORMANCE") != "1",
    reason="set BUMPIRE_DRIVER_CONFORMANCE=1 to run Docker driver contracts",
)
def test_pip_tools_resolver_rejects_incompatible_exact_upgrade(tmp_path: Path) -> None:
    profile = {profile.name: profile for profile in load_builtin_profiles()}["pip-tools"]
    fixture = Path(__file__).parent / "fixtures" / "pip-tools"
    filenames = (
        ".python-version",
        "requirements.in",
        "requirements.txt",
        "requirements-dev.in",
        "requirements-dev.txt",
    )
    _write_fixture(
        tmp_path,
        {
            filename: (fixture / filename).read_text(encoding="utf-8")
            for filename in filenames
        },
    )

    result = DockerRuntime(EventLogger(stream=StringIO())).run(
        ContainerCommand(
            image=profile.image(tmp_path),
            args=("sh", "-ceu", profile.resolve_script, "bumpire-driver"),
            environment={
                "BUMPIRE_MANAGER_VERSION": profile.manager_version(tmp_path),
                "BUMPIRE_UPDATE_TARGETS_JSON": json.dumps(
                    [{"package": "idna", "version": "2.0"}]
                ),
            },
        ),
        clone_root=tmp_path,
        workspace=PurePosixPath("."),
        timeout=600,
    )

    assert not result.ok


@pytest.mark.driver
@pytest.mark.skipif(
    os.environ.get("BUMPIRE_DRIVER_CONFORMANCE") != "1",
    reason="set BUMPIRE_DRIVER_CONFORMANCE=1 to run Docker driver contracts",
)
def test_cargo_resolver_rejects_incompatible_exact_upgrade(tmp_path: Path) -> None:
    profile = {profile.name: profile for profile in load_builtin_profiles()}["cargo"]
    fixture = Path(__file__).parent / "fixtures" / "cargo"
    filenames = ("Cargo.toml", "Cargo.lock", "app/Cargo.toml", "app/src/lib.rs")
    _write_fixture(
        tmp_path,
        {
            filename: (fixture / filename).read_text(encoding="utf-8")
            for filename in filenames
        },
    )

    result = DockerRuntime(EventLogger(stream=StringIO())).run(
        ContainerCommand(
            image=profile.image(tmp_path),
            args=("sh", "-ceu", profile.resolve_script, "bumpire-driver"),
            environment={
                "BUMPIRE_MANAGER_VERSION": profile.manager_version(tmp_path),
                "BUMPIRE_UPDATE_TARGETS_JSON": json.dumps(
                    [{"package": "itoa", "version": "0.4.8"}]
                ),
            },
        ),
        clone_root=tmp_path,
        workspace=PurePosixPath("."),
        timeout=600,
    )

    assert not result.ok


@pytest.mark.driver
@pytest.mark.skipif(
    os.environ.get("BUMPIRE_DRIVER_CONFORMANCE") != "1",
    reason="set BUMPIRE_DRIVER_CONFORMANCE=1 to run Docker driver contracts",
)
def test_composer_resolver_rejects_incompatible_exact_upgrade(tmp_path: Path) -> None:
    profile = {profile.name: profile for profile in load_builtin_profiles()}["composer"]
    fixture = Path(__file__).parent / "fixtures" / "composer"
    filenames = (".php-version", "composer.json", "composer.lock")
    _write_fixture(
        tmp_path,
        {
            filename: (fixture / filename).read_text(encoding="utf-8")
            for filename in filenames
        },
    )

    result = DockerRuntime(EventLogger(stream=StringIO())).run(
        ContainerCommand(
            image=profile.image(tmp_path),
            args=("sh", "-ceu", profile.resolve_script, "bumpire-driver"),
            environment={
                "BUMPIRE_MANAGER_VERSION": profile.manager_version(tmp_path),
                "BUMPIRE_UPDATE_TARGETS_JSON": json.dumps(
                    [{"package": "psr/log", "version": "1.1.4"}]
                ),
            },
        ),
        clone_root=tmp_path,
        workspace=PurePosixPath("."),
        timeout=600,
    )

    assert not result.ok


@pytest.mark.driver
@pytest.mark.skipif(
    os.environ.get("BUMPIRE_DRIVER_CONFORMANCE") != "1",
    reason="set BUMPIRE_DRIVER_CONFORMANCE=1 to run Docker driver contracts",
)
def test_dart_pub_resolver_does_not_retain_incompatible_override(tmp_path: Path) -> None:
    profile = {profile.name: profile for profile in load_builtin_profiles()}["dart-pub"]
    fixture = Path(__file__).parent / "fixtures" / "dart-pub"
    filenames = (".dart-version", "pubspec.yaml", "pubspec.lock")
    _write_fixture(
        tmp_path,
        {
            filename: (fixture / filename).read_text(encoding="utf-8")
            for filename in filenames
        },
    )

    result = DockerRuntime(EventLogger(stream=StringIO())).run(
        ContainerCommand(
            image=profile.image(tmp_path),
            args=("sh", "-ceu", profile.resolve_script, "bumpire-driver"),
            environment={
                "BUMPIRE_MANAGER_VERSION": profile.manager_version(tmp_path),
                "BUMPIRE_UPDATE_TARGETS_JSON": json.dumps(
                    [{"package": "http_parser", "version": "3.1.4"}]
                ),
            },
        ),
        clone_root=tmp_path,
        workspace=PurePosixPath("."),
        timeout=600,
    )

    assert result.ok, result.stderr or result.stdout
    graph = DockerRuntime(EventLogger(stream=StringIO())).run(
        ContainerCommand(
            image=profile.image(tmp_path),
            args=("sh", "-ceu", profile.resolved_graph_script, "bumpire-driver"),
            environment={"BUMPIRE_TARGETS_JSON": '["http_parser"]'},
            network="none",
            writable_workspace=False,
        ),
        clone_root=tmp_path,
        workspace=PurePosixPath("."),
        timeout=120,
    )
    assert graph.ok, graph.stderr or graph.stdout
    assert profile.parse_resolved_graph(graph.stdout, ["http_parser"]) != {
        "http_parser": ["3.1.4"]
    }
