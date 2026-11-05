"""npm adapter for package.json and package-lock.json projects."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

from depfix.docker_runtime import ContainerCommand
from depfix.events import EventLogger
from depfix.json_manifest import DEPENDENCY_SECTIONS, apply_dependency_edits, load_json_object
from depfix.models import (
    AttemptResult,
    DependencyConstraint,
    DependencyIssue,
    FixPlan,
    ProjectState,
)
from depfix.sandbox import GitSandbox

_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class NpmAdapter:
    ecosystem = "npm"
    manifest_name = "package.json"
    lockfile_name = "package-lock.json"

    def __init__(
        self,
        repo: Path,
        workspace: str,
        events: EventLogger,
        *,
        sandbox_backend: Literal["local", "docker"] = "local",
    ) -> None:
        self.repo = repo.resolve()
        self.workspace = workspace
        self.events = events
        self.sandbox_backend = sandbox_backend

    def inspect(self, issue: DependencyIssue) -> ProjectState:
        if issue.ecosystem != self.ecosystem:
            raise ValueError(f"npm adapter cannot handle {issue.ecosystem}")
        workdir = (self.repo / self.workspace).resolve()
        if not workdir.is_relative_to(self.repo) or not workdir.is_dir():
            raise ValueError(f"workspace does not exist: {self.workspace}")
        manifest_path = workdir / self.manifest_name
        lockfile_path = workdir / self.lockfile_name
        if not manifest_path.is_file() or not lockfile_path.is_file():
            raise ValueError("npm project requires package.json and package-lock.json")

        self.events.emit("detect", "Detected usage of npm package manager")
        self.events.emit("manifest", f"Viewing file {self.workspace}/{self.manifest_name}")
        manifest = load_json_object(manifest_path)
        manifest_text = manifest_path.read_text(encoding="utf-8")
        lock = self._load_lock(lockfile_path)
        self.events.emit("lockfile", "Checking npm lockfile versions")

        constraints = self._constraints(workdir, issue.package)
        direct = self._direct_dependencies(manifest)
        return ProjectState(
            ecosystem="npm",
            package_manager="npm",
            manifest_path=self.manifest_name,
            lockfile_path=self.lockfile_name,
            manifest_text=manifest_text,
            direct_dependencies=direct,
            locked_versions=self._locked_versions(lock),
            paths_to_target=self._paths_to_target(manifest, constraints, issue.package),
            target_locked_version=self._target_version(lock, issue.package),
            constraints=constraints,
        )

    def execute(
        self,
        issue: DependencyIssue,
        plan: FixPlan,
        *,
        attempt_number: int,
    ) -> AttemptResult:
        with GitSandbox(
            self.repo,
            self.workspace,
            self.events,
            backend=self.sandbox_backend,
        ) as sandbox:
            assert sandbox.workdir is not None
            manifest_path = sandbox.workdir / self.manifest_name
            lockfile_path = sandbox.workdir / self.lockfile_name
            before_lock = self._load_lock(lockfile_path)
            target_before = self._target_version(before_lock, issue.package)

            self.events.emit(
                "agent",
                "Running constrained npm dependency update loop",
                attempt=attempt_number,
            )
            try:
                edited_manifests = apply_dependency_edits(sandbox.workdir, plan.edits)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="manifest",
                    error=str(exc),
                    target_before=target_before,
                )
            self.events.emit(
                "edit",
                "Updating package manifests",
                files=edited_manifests,
            )
            staged_manifest = sandbox.stage(edited_manifests)
            if not staged_manifest.ok:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="manifest",
                    error="failed to stage package manifests",
                    stderr=staged_manifest.stderr,
                    target_before=target_before,
                )

            root_manifest = load_json_object(manifest_path)
            command = self._npm_command(root_manifest, sandbox.workdir)
            self.events.emit("resolve", f"Updating {issue.package} in lockfile")
            resolved = sandbox.package_command(
                command,
                container=self._npm_container(root_manifest),
                timeout=600,
            )
            if not resolved.ok:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="resolution",
                    error="npm could not resolve the proposed dependency graph",
                    stdout=resolved.stdout,
                    stderr=resolved.stderr,
                    target_before=target_before,
                )

            self.events.emit("lockfile", "Checking npm lockfile versions")
            after_lock = self._load_lock(lockfile_path)
            target_after = self._target_version(after_lock, issue.package)
            if target_after is None or not self._at_least(target_after, issue.safe_version):
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="verification",
                    error=(
                        f"resolved {issue.package}={target_after!r}, "
                        f"expected >= {issue.safe_version}"
                    ),
                    stdout=resolved.stdout,
                    stderr=resolved.stderr,
                    target_before=target_before,
                    target_after=target_after,
                )

            staged = sandbox.stage([*edited_manifests, self.lockfile_name])
            if not staged.ok:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="review",
                    error="failed to stage resolved files",
                    stderr=staged.stderr,
                    target_before=target_before,
                    target_after=target_after,
                )

            self.events.emit("review", "Reviewing the dependency update")
            changed_files = sandbox.changed_files()
            allowed = {self._repo_path(path) for path in edited_manifests}
            allowed.add(self._repo_path(self.lockfile_name))
            unexpected = sorted(set(changed_files) - allowed)
            if unexpected:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="review",
                    error=f"unexpected files changed: {unexpected}",
                    target_before=target_before,
                    target_after=target_after,
                    changed_files=changed_files,
                )

            diff = sandbox.staged_diff()
            if not diff.strip():
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="review",
                    error="resolver produced no staged diff",
                    target_before=target_before,
                    target_after=target_after,
                )
            self.events.emit("stage", "Adding verified manifests and lockfile to staging area")
            return AttemptResult(
                number=attempt_number,
                plan=plan,
                success=True,
                phase="completed",
                stdout=resolved.stdout,
                stderr=resolved.stderr,
                target_before=target_before,
                target_after=target_after,
                changed_files=changed_files,
                diff=diff,
            )

    def _constraints(self, workdir: Path, target: str) -> list[DependencyConstraint]:
        constraints: list[DependencyConstraint] = []
        for path in sorted(workdir.rglob(self.manifest_name)):
            relative = path.relative_to(workdir)
            if any(part in {"node_modules", ".git"} for part in relative.parts):
                continue
            manifest = load_json_object(path)
            for section_name in DEPENDENCY_SECTIONS:
                section = manifest.get(section_name)
                if isinstance(section, dict) and isinstance(section.get(target), str):
                    constraints.append(
                        DependencyConstraint(
                            package=target,
                            requirement=section[target],
                            manifest_path=relative.as_posix(),
                            section=section_name,
                            is_root=relative == Path(self.manifest_name),
                        )
                    )
        return constraints

    @staticmethod
    def _direct_dependencies(manifest: dict[str, Any]) -> dict[str, str]:
        direct: dict[str, str] = {}
        for section_name in DEPENDENCY_SECTIONS:
            section = manifest.get(section_name)
            if isinstance(section, dict):
                direct.update(
                    {name: value for name, value in section.items() if isinstance(value, str)}
                )
        return direct

    @staticmethod
    def _load_lock(path: Path) -> dict[str, Any]:
        return load_json_object(path)

    @staticmethod
    def _locked_versions(lock: dict[str, Any]) -> dict[str, str]:
        versions: dict[str, str] = {}
        packages = lock.get("packages")
        if isinstance(packages, dict):
            for path, value in packages.items():
                if not isinstance(path, str) or not path.startswith("node_modules/"):
                    continue
                if isinstance(value, dict) and isinstance(value.get("version"), str):
                    versions[path.removeprefix("node_modules/")] = value["version"]
        dependencies = lock.get("dependencies")
        if isinstance(dependencies, dict):
            for name, value in dependencies.items():
                if isinstance(value, dict) and isinstance(value.get("version"), str):
                    versions.setdefault(name, value["version"])
        return versions

    @classmethod
    def _target_version(cls, lock: dict[str, Any], target: str) -> str | None:
        return cls._locked_versions(lock).get(target)

    @staticmethod
    def _paths_to_target(
        manifest: dict[str, Any],
        constraints: list[DependencyConstraint],
        target: str,
    ) -> list[list[str]]:
        paths: list[list[str]] = []
        direct = NpmAdapter._direct_dependencies(manifest)
        if target in direct:
            paths.append([target])
        for constraint in constraints:
            if constraint.is_root:
                continue
            paths.append([constraint.manifest_path, target])
        return paths

    @staticmethod
    def _npm_command(manifest: dict[str, Any], workdir: Path) -> list[str]:
        npm = ["npm"]
        engines = manifest.get("engines")
        node_version = engines.get("node") if isinstance(engines, dict) else None
        if (
            isinstance(node_version, str)
            and _EXACT_VERSION.fullmatch(node_version)
            and shutil.which("mise")
        ):
            npm = ["mise", "x", f"node@{node_version}", "--", "npm"]
        return [
            *npm,
            "--prefix",
            str(workdir),
            "install",
            "--package-lock-only",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ]

    @staticmethod
    def _npm_container(manifest: dict[str, Any]) -> ContainerCommand:
        engines = manifest.get("engines")
        node_version = engines.get("node") if isinstance(engines, dict) else None
        if not isinstance(node_version, str) or not _EXACT_VERSION.fullmatch(node_version):
            node_version = "22"
        return ContainerCommand(
            image=f"node:{node_version}-bookworm-slim",
            args=(
                "npm",
                "install",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
            ),
            environment={
                "npm_config_cache": "/tmp/npm-cache",
                "npm_config_update_notifier": "false",
            },
        )

    def _repo_path(self, path: str) -> str:
        return str(PurePosixPath(self.workspace) / PurePosixPath(path))

    @staticmethod
    def _at_least(actual: str, minimum: str) -> bool:
        try:
            return Version(actual) >= Version(minimum)
        except InvalidVersion:
            return actual == minimum
