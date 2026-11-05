from io import StringIO
from pathlib import PurePosixPath

import pytest

from depfix.bundler import BundlerAdapter
from depfix.bundler_lock import BundlerLock
from depfix.docker_runtime import ContainerCommand, DockerRuntime
from depfix.events import EventLogger
from depfix.npm import NpmAdapter


def test_docker_command_has_isolation_boundaries(tmp_path) -> None:
    clone = tmp_path / "repository"
    (clone / ".git").mkdir(parents=True)
    command = ContainerCommand(
        image="node:22.19.0-bookworm-slim",
        args=("npm", "install", "--package-lock-only", "--ignore-scripts"),
        environment={"npm_config_cache": "/tmp/npm-cache"},
    )

    args = DockerRuntime.build_command(
        command,
        clone_root=clone,
        workspace=PurePosixPath("web"),
        container_name="depfix-test",
    )

    assert args[args.index("--name") + 1] == "depfix-test"
    assert "--read-only" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert args[args.index("--network") + 1] == "bridge"
    assert args[args.index("--workdir") + 1] == "/workspace/web"
    assert f"type=bind,src={clone.resolve()},dst=/workspace" in args
    assert f"type=bind,src={(clone / '.git').resolve()},dst=/workspace/.git,readonly" in args
    assert args[-5:] == [
        "node:22.19.0-bookworm-slim",
        "npm",
        "install",
        "--package-lock-only",
        "--ignore-scripts",
    ]


def test_docker_command_rejects_unsafe_image(tmp_path) -> None:
    clone = tmp_path / "repository"
    (clone / ".git").mkdir(parents=True)

    with pytest.raises(ValueError, match="unsafe Docker image"):
        DockerRuntime.build_command(
            ContainerCommand(image="node:22; touch /tmp/pwned", args=("true",)),
            clone_root=clone,
            workspace=PurePosixPath("."),
        )


def test_npm_container_uses_exact_engine_version() -> None:
    command = NpmAdapter._npm_container({"engines": {"node": "22.19.0"}})

    assert command.image == "node:22.19.0-bookworm-slim"
    assert "--ignore-scripts" in command.args


def test_bundler_container_uses_repo_ruby_and_lockfile_bundler(tmp_path) -> None:
    workspace = tmp_path / "rails"
    workspace.mkdir()
    (workspace / ".ruby-version").write_text("3.3.10\n", encoding="utf-8")
    adapter = BundlerAdapter(
        tmp_path,
        "rails",
        EventLogger(stream=StringIO()),
        sandbox_backend="docker",
    )
    lock = BundlerLock(specs={}, direct_dependencies={}, bundler_version="4.0.10")

    command = adapter._bundle_container(lock)

    assert command.image == "ruby:3.3.10-slim"
    assert command.environment["DEPFIX_BUNDLER_VERSION"] == "4.0.10"
