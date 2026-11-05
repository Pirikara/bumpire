"""Bounded subprocess execution."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_OUTPUT_LIMIT = 20_000


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 300,
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        args=tuple(args),
        returncode=completed.returncode,
        stdout=completed.stdout[-_OUTPUT_LIMIT:],
        stderr=completed.stderr[-_OUTPUT_LIMIT:],
    )
