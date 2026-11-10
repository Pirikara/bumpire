"""Hardened Docker execution for untrusted package-manager resolution."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from bumpire.events import EventLogger
from bumpire.process import CommandResult, run_command

_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]*$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class ContainerCommand:
    image: str
    args: tuple[str, ...]
    environment: dict[str, str] = field(default_factory=dict)
    network: Literal["bridge", "none"] = "bridge"
    writable_workspace: bool = True


class DockerRuntime:
    def __init__(self, events: EventLogger) -> None:
        self.events = events

    @staticmethod
    def ensure_available() -> None:
        if shutil.which("docker") is None:
            raise RuntimeError("Docker sandbox requested but docker is not installed")
        result = run_command(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            cwd=Path.cwd(),
            timeout=15,
        )
        if not result.ok:
            raise RuntimeError(
                "Docker sandbox requested but the daemon is unavailable: "
                f"{result.stderr or result.stdout}"
            )

    def run(
        self,
        command: ContainerCommand,
        *,
        clone_root: Path,
        workspace: PurePosixPath,
        timeout: int,
    ) -> CommandResult:
        container_name = f"bumpire-{uuid.uuid4().hex[:16]}"
        args = self.build_command(
            command,
            clone_root=clone_root,
            workspace=workspace,
            container_name=container_name,
        )
        self.events.emit(
            "container",
            f"Running trusted driver command in Docker image {command.image}",
            read_only_root=True,
            capabilities="none",
        )
        result = run_command(args, cwd=clone_root.parent, timeout=timeout)
        if result.returncode == 124:
            run_command(
                ["docker", "rm", "--force", container_name],
                cwd=clone_root.parent,
                timeout=30,
            )
        return result

    @staticmethod
    def build_command(
        command: ContainerCommand,
        *,
        clone_root: Path,
        workspace: PurePosixPath,
        container_name: str | None = None,
    ) -> list[str]:
        if not _IMAGE.fullmatch(command.image):
            raise ValueError(f"unsafe Docker image reference: {command.image!r}")
        if command.network not in {"bridge", "none"}:
            raise ValueError(f"unsafe Docker network mode: {command.network!r}")
        if workspace.is_absolute() or ".." in workspace.parts:
            raise ValueError("unsafe Docker workspace")
        clone_root = clone_root.resolve()
        git_metadata = clone_root / ".git"
        if not git_metadata.is_dir():
            raise ValueError("isolated clone is missing .git metadata")

        container_workdir = PurePosixPath("/workspace") / workspace
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        args = [
            "docker",
            "run",
            "--rm",
            *(["--name", container_name] if container_name else []),
            "--init",
            "--pull",
            "missing",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--network",
            command.network,
            "--user",
            f"{uid}:{gid}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=512m",
            "--mount",
            (
                f"type=bind,src={clone_root},dst=/workspace"
                + ("" if command.writable_workspace else ",readonly")
            ),
            *(
                [
                    "--mount",
                    f"type=bind,src={git_metadata},dst=/workspace/.git,readonly",
                ]
                if command.writable_workspace
                else []
            ),
            "--workdir",
            container_workdir.as_posix(),
            "--env",
            "HOME=/tmp/bumpire-home",
            "--label",
            "dev.bumpire.ephemeral=true",
        ]
        for name, value in sorted(command.environment.items()):
            if not _ENV_NAME.fullmatch(name) or "\x00" in value:
                raise ValueError(f"unsafe container environment entry: {name!r}")
            args.extend(["--env", f"{name}={value}"])
        args.extend([command.image, *command.args])
        return args
