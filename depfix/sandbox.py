"""Disposable Git workspace used for every candidate fix."""

from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

from depfix.events import EventLogger
from depfix.process import CommandResult, run_command

_GIT = ["git", "-c", "core.hooksPath=/dev/null"]


class GitSandbox:
    def __init__(self, source: Path, workspace: str, events: EventLogger) -> None:
        self.source = source.resolve()
        self.workspace_rel = PurePosixPath(workspace or ".")
        if self.workspace_rel.is_absolute() or ".." in self.workspace_rel.parts:
            raise ValueError("workspace must be a relative path without '..'")
        self.events = events
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.clone_root: Path | None = None
        self.workdir: Path | None = None

    def __enter__(self) -> Self:
        self.events.emit("sandbox", "Creating isolated workspace for AutoFix")
        self._temporary = tempfile.TemporaryDirectory(prefix="depfix-")
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

        self.clone_root = destination
        self.workdir = (destination / Path(*self.workspace_rel.parts)).resolve()
        if not self.workdir.is_relative_to(destination.resolve()) or not self.workdir.is_dir():
            self._temporary.cleanup()
            raise ValueError(f"workspace does not exist in clone: {self.workspace_rel}")

        for key, value in (
            ("user.name", "depfix-agent[bot]"),
            ("user.email", "depfix-agent[bot]@users.noreply.github.com"),
            ("core.hooksPath", "/dev/null"),
        ):
            configured = run_command([*_GIT, "config", key, value], cwd=destination)
            if not configured.ok:
                self._temporary.cleanup()
                raise RuntimeError(f"git config failed: {configured.stderr}")
        self.events.emit("git", "Configured isolated Git identity and disabled hooks")
        return self

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

    def staged_diff(self) -> str:
        if self.clone_root is None:
            raise RuntimeError("sandbox is not active")
        result = run_command([*_GIT, "diff", "--cached", "--no-ext-diff"], cwd=self.clone_root)
        if not result.ok:
            raise RuntimeError(result.stderr)
        return result.stdout
