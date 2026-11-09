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
    output_limit: int = _OUTPUT_LIMIT,
) -> CommandResult:
    if output_limit < 1:
        raise ValueError("output_limit must be positive")
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_text(exc.stdout)
        stderr = _timeout_text(exc.stderr)
        message = f"command timed out after {timeout} seconds"
        return CommandResult(
            args=tuple(args),
            returncode=124,
            stdout=stdout[-output_limit:],
            stderr=f"{stderr}\n{message}".strip()[-output_limit:],
        )
    return CommandResult(
        args=tuple(args),
        returncode=completed.returncode,
        stdout=completed.stdout[-output_limit:],
        stderr=completed.stderr[-output_limit:],
    )


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
