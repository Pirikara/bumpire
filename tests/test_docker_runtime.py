from pathlib import PurePosixPath

import pytest

from bumpire.docker_runtime import ContainerCommand, DockerRuntime


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
        container_name="bumpire-test",
    )

    assert args[args.index("--name") + 1] == "bumpire-test"
    assert "--read-only" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert args[args.index("--network") + 1] == "bridge"
    assert "/bumpire-tools:rw,exec,nosuid,nodev,size=256m" in args
    assert args[args.index("--workdir") + 1] == "/workspace/web"
    assert "dev.bumpire.ephemeral=true" in args
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


def test_graph_extractor_can_run_without_network_or_workspace_writes(tmp_path) -> None:
    clone = tmp_path / "repository"
    (clone / ".git").mkdir(parents=True)

    args = DockerRuntime.build_command(
        ContainerCommand(
            image="node:22-bookworm-slim",
            args=("node", "extract.js"),
            network="none",
            writable_workspace=False,
        ),
        clone_root=clone,
        workspace=PurePosixPath("."),
    )

    assert args[args.index("--network") + 1] == "none"
    assert f"type=bind,src={clone.resolve()},dst=/workspace,readonly" in args
    assert f"type=bind,src={(clone / '.git').resolve()},dst=/workspace/.git,readonly" not in args
