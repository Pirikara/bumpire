"""Project detection and adapter construction."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

from depfix.adapters.base import DependencyAdapter
from depfix.bundler import BundlerAdapter
from depfix.events import EventLogger
from depfix.npm import NpmAdapter

Ecosystem = Literal["bundler", "npm"]


def create_adapter(
    repo: Path,
    workspace: str,
    events: EventLogger,
    requested: str = "auto",
) -> DependencyAdapter:
    workdir = (repo / workspace).resolve()
    if not workdir.is_relative_to(repo.resolve()) or not workdir.is_dir():
        raise ValueError(f"workspace does not exist: {workspace}")

    detected: Ecosystem | None = None
    if (workdir / "package.json").is_file() and (workdir / "package-lock.json").is_file():
        detected = "npm"
    elif (workdir / "Gemfile").is_file() and (workdir / "Gemfile.lock").is_file():
        detected = "bundler"

    if requested != "auto":
        if requested not in {"bundler", "npm"}:
            raise ValueError(f"unsupported ecosystem: {requested}")
        selected = cast(Ecosystem, requested)
        if detected is not None and selected != detected:
            raise ValueError(
                f"requested ecosystem {selected!r} does not match detected {detected!r}"
            )
    elif detected is None:
        raise ValueError("could not detect a supported package manager")
    else:
        selected = detected

    if selected == "npm":
        return NpmAdapter(repo, workspace, events)
    return BundlerAdapter(repo, workspace, events)
