"""One generic executor driven by declarative package-manager profiles."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal

from packaging.version import InvalidVersion, Version

from bumpire.docker_runtime import ContainerCommand
from bumpire.events import EventLogger
from bumpire.models import AttemptResult, DependencyIssue, FileFixPlan, ProjectState
from bumpire.profile import PackageManagerProfile
from bumpire.sandbox import GitSandbox

_VERSION_MARKER = re.compile(r"^BUMPIRE_RESOLVED_VERSION=(?P<version>.+)$", re.MULTILINE)
_MAX_CONTEXT_BYTES = 1_000_000


class ProfileExecutor:
    def __init__(
        self,
        repo: Path,
        workspace: str,
        events: EventLogger,
        profile: PackageManagerProfile,
        *,
        sandbox_backend: Literal["docker"] = "docker",
    ) -> None:
        if sandbox_backend != "docker":
            raise ValueError("generic profile execution requires the Docker sandbox")
        self.repo = repo.resolve()
        self.workspace = workspace
        self.events = events
        self.profile = profile
        self.ecosystem = profile.ecosystem
        self.sandbox_backend = sandbox_backend

    def inspect(self, issue: DependencyIssue) -> ProjectState:
        if issue.ecosystem != self.ecosystem:
            raise ValueError(f"profile {self.profile.name} cannot handle {issue.ecosystem}")
        workdir = self._workdir(self.repo)
        manifests = self.profile.manifest_paths(workdir)
        if not manifests:
            raise ValueError(f"profile {self.profile.name} found no dependency manifests")

        documents: dict[str, str] = {}
        total_bytes = 0
        for path in manifests:
            content = path.read_text(encoding="utf-8")
            total_bytes += len(content.encode())
            if total_bytes > _MAX_CONTEXT_BYTES:
                raise ValueError("dependency manifest context exceeds 1 MB")
            documents[path.relative_to(workdir).as_posix()] = content

        lockfile_path = workdir / self.profile.lockfiles[0]
        lock_text = lockfile_path.read_text(encoding="utf-8")
        self.events.emit(
            "detect",
            f"Detected {self.profile.name} through declarative package-manager profile",
        )
        self.events.emit("manifest", f"Loaded {len(documents)} allowed dependency manifests")
        self.events.emit("lockfile", f"Checking {self.profile.name} lockfile version")
        return ProjectState(
            ecosystem=self.ecosystem,
            package_manager=self.profile.name,
            lockfile_path=self.profile.lockfiles[0],
            target_locked_version=self.profile.locked_version(lock_text, issue.package),
            manifest_documents=documents,
            profile_instructions=self.profile.instructions,
        )

    def execute(
        self,
        issue: DependencyIssue,
        plan: FileFixPlan,
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
            before = self._locked_version(sandbox.workdir, issue.package)
            edited_paths: list[str] = []
            try:
                for replacement in plan.files:
                    relative = PurePosixPath(replacement.path)
                    if not self.profile.allows_manifest(relative.as_posix()):
                        raise ValueError(f"profile does not allow manifest: {relative}")
                    path = (sandbox.workdir / Path(*relative.parts)).resolve()
                    if not path.is_relative_to(sandbox.workdir.resolve()) or not path.is_file():
                        raise ValueError(f"manifest does not exist: {relative}")
                    original = path.read_text(encoding="utf-8")
                    content = self._preserve_final_newline(original, replacement.content)
                    self.profile.validate_manifest_replacement(original, content)
                    path.write_text(content, encoding="utf-8")
                    edited_paths.append(relative.as_posix())
            except (OSError, ValueError) as exc:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="manifest",
                    error=str(exc),
                    target_before=before,
                )

            self.events.emit(
                "edit",
                "Replacing agent-selected dependency manifests",
                files=edited_paths,
            )
            staged_manifest = sandbox.stage(edited_paths)
            if not staged_manifest.ok:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="manifest",
                    error="failed to stage dependency manifests",
                    stderr=staged_manifest.stderr,
                    target_before=before,
                )

            self.events.emit("resolve", f"Running {self.profile.name} profile resolver")
            resolved = sandbox.package_command(
                ["false"],
                container=ContainerCommand(
                    image=self.profile.image(sandbox.workdir),
                    args=("sh", "-ceu", self.profile.resolver_script, "bumpire-profile"),
                    environment={
                        "BUMPIRE_PACKAGE": issue.package,
                        "BUMPIRE_SAFE_VERSION": issue.safe_version,
                        "BUMPIRE_MANAGER_VERSION": self.profile.manager_version(sandbox.workdir),
                    },
                ),
                timeout=600,
            )
            if not resolved.ok:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="resolution",
                    error=f"{self.profile.name} could not resolve the proposed dependency graph",
                    stdout=resolved.stdout,
                    stderr=resolved.stderr,
                    target_before=before,
                )

            marker = _VERSION_MARKER.search(resolved.stdout)
            target_after = marker.group("version").strip() if marker is not None else None
            if target_after is None or not self._at_least(target_after, issue.safe_version):
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="verification",
                    error=(
                        f"profile reported {issue.package}={target_after!r}, "
                        f"expected >= {issue.safe_version}"
                    ),
                    stdout=resolved.stdout,
                    stderr=resolved.stderr,
                    target_before=before,
                    target_after=target_after,
                )

            staged = sandbox.stage([*edited_paths, *self.profile.lockfiles])
            if not staged.ok:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="review",
                    error="failed to stage profile-resolved files",
                    stderr=staged.stderr,
                    target_before=before,
                    target_after=target_after,
                )

            self.events.emit("review", "Reviewing the profile-driven dependency update")
            changed_files = sandbox.changed_files()
            allowed = {self._repo_path(path) for path in edited_paths}
            allowed.update(self._repo_path(path) for path in self.profile.lockfiles)
            unexpected = sorted(set(changed_files) - allowed)
            if unexpected:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="review",
                    error=f"unexpected files changed: {unexpected}",
                    target_before=before,
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
                    target_before=before,
                    target_after=target_after,
                )
            self.events.emit("stage", "Adding profile-verified dependency files")
            return AttemptResult(
                number=attempt_number,
                plan=plan,
                success=True,
                phase="completed",
                stdout=resolved.stdout,
                stderr=resolved.stderr,
                target_before=before,
                target_after=target_after,
                changed_files=changed_files,
                diff=diff,
            )

    def _workdir(self, root: Path) -> Path:
        workdir = (root / self.workspace).resolve()
        if not workdir.is_relative_to(root.resolve()) or not workdir.is_dir():
            raise ValueError(f"workspace does not exist: {self.workspace}")
        return workdir

    def _locked_version(self, workdir: Path, package: str) -> str | None:
        for relative in self.profile.lockfiles:
            path = workdir / relative
            if path.is_file():
                version = self.profile.locked_version(path.read_text(encoding="utf-8"), package)
                if version is not None:
                    return version
        return None

    def _repo_path(self, path: str) -> str:
        return str(PurePosixPath(self.workspace) / PurePosixPath(path))

    @staticmethod
    def _at_least(actual: str, minimum: str) -> bool:
        try:
            return Version(actual) >= Version(minimum)
        except InvalidVersion:
            return actual == minimum

    @staticmethod
    def _preserve_final_newline(original: str, replacement: str) -> str:
        if original.endswith("\n") and not replacement.endswith("\n"):
            return f"{replacement}\n"
        if not original.endswith("\n") and replacement.endswith("\n"):
            return replacement.rstrip("\n")
        return replacement
