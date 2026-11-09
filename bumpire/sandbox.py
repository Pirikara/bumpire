"""Disposable Git workspace used for every candidate fix."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Literal, Self

from bumpire.docker_runtime import ContainerCommand, DockerRuntime
from bumpire.events import EventLogger
from bumpire.process import CommandResult, run_command

_GIT = ["git", "-c", "core.hooksPath=/dev/null"]
_GIT_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


class GitSandbox:
    def __init__(
        self,
        source: Path,
        workspace: str,
        events: EventLogger,
        *,
        backend: Literal["local", "docker"] = "local",
        base_commit: str | None = None,
    ) -> None:
        self.source = source.resolve()
        self.workspace_rel = PurePosixPath(workspace or ".")
        if self.workspace_rel.is_absolute() or ".." in self.workspace_rel.parts:
            raise ValueError("workspace must be a relative path without '..'")
        if backend not in {"local", "docker"}:
            raise ValueError(f"unsupported sandbox backend: {backend}")
        self.events = events
        self.backend = backend
        if base_commit is not None and _GIT_OBJECT_ID.fullmatch(base_commit) is None:
            raise ValueError("base_commit must be a full hexadecimal Git object ID")
        self.base_commit = base_commit.lower() if base_commit is not None else None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.clone_root: Path | None = None
        self.workdir: Path | None = None

    def __enter__(self) -> Self:
        self.events.emit("sandbox", "Creating isolated workspace for dependency update")
        self._temporary = tempfile.TemporaryDirectory(prefix="bumpire-")
        destination = Path(self._temporary.name) / "repository"
        clone_args = [*_GIT, "clone", "--depth", "1"]
        if self.source.exists():
            clone_args.append("--no-local")
        else:
            clone_args.append("--filter=blob:limit=20m")
        clone_args.extend([str(self.source), str(destination)])
        self.events.emit("clone", "Cloning repository with Git hooks disabled")
        result = run_command(clone_args, cwd=Path(self._temporary.name))
        if not result.ok:
            self._temporary.cleanup()
            raise RuntimeError(f"git clone failed: {result.stderr or result.stdout}")

        if self.base_commit is not None:
            self._checkout_base_commit(destination)

        self.clone_root = destination
        self.workdir = (destination / Path(*self.workspace_rel.parts)).resolve()
        if not self.workdir.is_relative_to(destination.resolve()) or not self.workdir.is_dir():
            self._temporary.cleanup()
            raise ValueError(f"workspace does not exist in clone: {self.workspace_rel}")

        for key, value in (
            ("user.name", "bumpire[bot]"),
            ("user.email", "bumpire[bot]@users.noreply.github.com"),
            ("core.hooksPath", "/dev/null"),
        ):
            configured = run_command([*_GIT, "config", key, value], cwd=destination)
            if not configured.ok:
                self._temporary.cleanup()
                raise RuntimeError(f"git config failed: {configured.stderr}")
        self.events.emit("git", "Configured isolated Git identity and disabled hooks")
        return self

    def _checkout_base_commit(self, destination: Path) -> None:
        assert self.base_commit is not None
        exists = run_command(
            [*_GIT, "cat-file", "-e", f"{self.base_commit}^{{commit}}"],
            cwd=destination,
        )
        if not exists.ok:
            fetched = run_command(
                [*_GIT, "fetch", "--depth", "1", "origin", self.base_commit],
                cwd=destination,
            )
            if not fetched.ok:
                self._temporary.cleanup()
                raise RuntimeError(f"could not fetch pinned base commit: {fetched.stderr}")
        checked_out = run_command(
            [*_GIT, "checkout", "--detach", self.base_commit],
            cwd=destination,
        )
        if not checked_out.ok:
            self._temporary.cleanup()
            raise RuntimeError(f"could not check out pinned base commit: {checked_out.stderr}")
        actual = run_command([*_GIT, "rev-parse", "HEAD"], cwd=destination)
        if not actual.ok or actual.stdout.strip().lower() != self.base_commit:
            self._temporary.cleanup()
            raise RuntimeError("isolated workspace does not match the pinned base commit")
        self.events.emit("git", f"Pinned isolated workspace to {self.base_commit[:12]}")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()

    def command(self, args: list[str], *, timeout: int = 300) -> CommandResult:
        if self.workdir is None:
            raise RuntimeError("sandbox is not active")
        return run_command(args, cwd=self.workdir, timeout=timeout)

    def isolated_command(
        self,
        args: list[str],
        *,
        timeout: int = 300,
        extra_env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run a package manager without loading tool config from the cloned repo."""
        if self.clone_root is None:
            raise RuntimeError("sandbox is not active")
        return run_command(
            args,
            cwd=self.clone_root.parent,
            timeout=timeout,
            extra_env=extra_env,
        )

    def package_command(
        self,
        local_args: list[str],
        *,
        container: ContainerCommand,
        timeout: int = 300,
        local_env: dict[str, str] | None = None,
    ) -> CommandResult:
        if self.clone_root is None:
            raise RuntimeError("sandbox is not active")
        if self.backend == "docker":
            return DockerRuntime(self.events).run(
                container,
                clone_root=self.clone_root,
                workspace=self.workspace_rel,
                timeout=timeout,
            )
        return self.isolated_command(local_args, timeout=timeout, extra_env=local_env)

    def stage(self, paths: list[str]) -> CommandResult:
        if self.clone_root is None:
            raise RuntimeError("sandbox is not active")
        normalized: list[str] = []
        for item in paths:
            path = PurePosixPath(item)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe staged path: {item}")
            normalized.append(str(self.workspace_rel / path))
        return run_command([*_GIT, "add", "--", *normalized], cwd=self.clone_root)

    def staged_files(self) -> list[str]:
        if self.clone_root is None:
            raise RuntimeError("sandbox is not active")
        result = run_command([*_GIT, "diff", "--cached", "--name-only"], cwd=self.clone_root)
        if not result.ok:
            raise RuntimeError(result.stderr)
        return [line for line in result.stdout.splitlines() if line]

    def changed_files(self) -> list[str]:
        """Return every staged, unstaged, or untracked path in the disposable clone."""
        if self.clone_root is None:
            raise RuntimeError("sandbox is not active")
        commands = (
            [*_GIT, "diff", "--cached", "--name-only"],
            [*_GIT, "diff", "--name-only"],
            [*_GIT, "ls-files", "--others"],
        )
        changed: set[str] = set()
        for command in commands:
            result = run_command(command, cwd=self.clone_root)
            if not result.ok:
                raise RuntimeError(result.stderr)
            changed.update(line for line in result.stdout.splitlines() if line)
        return sorted(changed)

    def staged_diff(self) -> str:
        if self.clone_root is None:
            raise RuntimeError("sandbox is not active")
        result = run_command(
            [*_GIT, "diff", "--cached", "--no-ext-diff"],
            cwd=self.clone_root,
            output_limit=5_000_001,
        )
        if not result.ok:
            raise RuntimeError(result.stderr)
        if len(result.stdout) > 5_000_000:
            raise ValueError("staged diff exceeds the 5 MB artifact limit")
        return result.stdout
