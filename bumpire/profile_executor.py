"""One generic executor driven by declarative package-manager profiles."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal

from packaging.version import InvalidVersion, Version

from bumpire.docker_runtime import ContainerCommand
from bumpire.events import EventLogger
from bumpire.models import (
    AttemptResult,
    FileFixPlan,
    ProjectState,
    RemediationRequest,
    TargetResolution,
)
from bumpire.profile import PackageManagerProfile
from bumpire.sandbox import GitSandbox

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

    def inspect(self, request: RemediationRequest) -> ProjectState:
        if request.ecosystem != self.ecosystem:
            raise ValueError(f"profile {self.profile.name} cannot handle {request.ecosystem}")
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
        target_versions = {
            target.package: self.profile.locked_versions(lock_text, target.package)
            for target in request.targets
        }
        missing = sorted(package for package, versions in target_versions.items() if not versions)
        if missing:
            raise ValueError(f"target packages not found in lockfile: {missing}")
        self.events.emit(
            "detect",
            f"Detected {self.profile.name} through declarative package-manager profile",
        )
        self.events.emit("manifest", f"Loaded {len(documents)} allowed dependency manifests")
        self.events.emit("lockfile", f"Checking {self.profile.name} lockfile versions")
        return ProjectState(
            ecosystem=self.ecosystem,
            package_manager=self.profile.name,
            lockfile_path=self.profile.lockfiles[0],
            target_locked_versions=target_versions,
            manifest_documents=documents,
            profile_instructions=self.profile.instructions,
        )

    def execute(
        self,
        request: RemediationRequest,
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
            before = self._locked_versions_by_target(sandbox.workdir, request)
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
                )

            self.events.emit("resolve", f"Running {self.profile.name} profile resolver")
            resolved = sandbox.package_command(
                ["false"],
                container=ContainerCommand(
                    image=self.profile.image(sandbox.workdir),
                    args=("sh", "-ceu", self.profile.resolver_script, "bumpire-profile"),
                    environment={
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
                )

            after = self._locked_versions_by_target(sandbox.workdir, request)
            target_resolutions = self.evaluate_targets(request, before, after)
            failed_targets = [
                resolution.target.package
                for resolution in target_resolutions
                if not resolution.satisfied
            ]
            if failed_targets:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="verification",
                    error=f"targets did not reach their minimum versions: {failed_targets}",
                    stdout=resolved.stdout,
                    stderr=resolved.stderr,
                    target_resolutions=target_resolutions,
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
                    target_resolutions=target_resolutions,
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
                    target_resolutions=target_resolutions,
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
                    target_resolutions=target_resolutions,
                )
            self.events.emit("stage", "Adding profile-verified dependency files")
            return AttemptResult(
                number=attempt_number,
                plan=plan,
                success=True,
                phase="completed",
                stdout=resolved.stdout,
                stderr=resolved.stderr,
                target_resolutions=target_resolutions,
                changed_files=changed_files,
                diff=diff,
            )

    def _workdir(self, root: Path) -> Path:
        workdir = (root / self.workspace).resolve()
        if not workdir.is_relative_to(root.resolve()) or not workdir.is_dir():
            raise ValueError(f"workspace does not exist: {self.workspace}")
        return workdir

    def _locked_versions(self, workdir: Path, package: str) -> list[str]:
        versions: list[str] = []
        for relative in self.profile.lockfiles:
            path = workdir / relative
            if path.is_file():
                versions.extend(
                    self.profile.locked_versions(path.read_text(encoding="utf-8"), package)
                )
        return versions

    def _locked_versions_by_target(
        self,
        workdir: Path,
        request: RemediationRequest,
    ) -> dict[str, list[str]]:
        return {
            target.package: self._locked_versions(workdir, target.package)
            for target in request.targets
        }

    def request_is_satisfied(
        self,
        request: RemediationRequest,
        versions: dict[str, list[str]],
    ) -> bool:
        for target in request.targets:
            current_versions = versions.get(target.package, [])
            if not current_versions or not all(
                self._at_least(version, target.minimum_version) for version in current_versions
            ):
                return False
        return True

    def evaluate_targets(
        self,
        request: RemediationRequest,
        before: dict[str, list[str]],
        after: dict[str, list[str]],
    ) -> list[TargetResolution]:
        resolutions: list[TargetResolution] = []
        for target in request.targets:
            versions_before = before.get(target.package, [])
            versions_after = after.get(target.package, [])
            if not versions_before:
                satisfied = False
                outcome: Literal["safe", "removed", "below_minimum", "missing"] = "missing"
            elif not versions_after:
                satisfied = True
                outcome = "removed"
            elif all(self._at_least(version, target.minimum_version) for version in versions_after):
                satisfied = True
                outcome = "safe"
            else:
                satisfied = False
                outcome = "below_minimum"
            resolutions.append(
                TargetResolution(
                    target=target,
                    versions_before=versions_before,
                    versions_after=versions_after,
                    satisfied=satisfied,
                    outcome=outcome,
                )
            )
        return resolutions

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
