"""One generic executor driven by declarative package-manager profiles."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal

from bumpire.docker_runtime import ContainerCommand
from bumpire.events import EventLogger
from bumpire.models import (
    AttemptResult,
    DependencyTarget,
    FileFixPlan,
    ProjectState,
    TargetVerification,
    UpdateRequest,
)
from bumpire.process import run_command
from bumpire.profile import PackageManagerProfile
from bumpire.sandbox import GitSandbox
from bumpire.versions import VersionComparisonError, satisfies

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

    def inspect(self, request: UpdateRequest) -> ProjectState:
        if request.ecosystem != self.ecosystem:
            raise ValueError(f"profile {self.profile.name} cannot handle {request.ecosystem}")
        base_commit, base_ref = self._repository_state()
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

        target_versions = self._locked_versions_by_target(workdir, request)
        missing = sorted(package for package, versions in target_versions.items() if not versions)
        if missing:
            raise ValueError(f"target packages not found in lockfile: {missing}")
        self._validate_comparable_versions(request, target_versions)
        confirmed_commit, confirmed_ref = self._repository_state()
        if (confirmed_commit, confirmed_ref) != (base_commit, base_ref):
            raise ValueError("repository HEAD changed while dependency state was being inspected")
        self.events.emit(
            "detect",
            f"Detected {self.profile.name} through declarative package-manager profile",
        )
        self.events.emit("manifest", f"Loaded {len(documents)} allowed dependency manifests")
        self.events.emit("lockfile", f"Checking {self.profile.name} lockfile versions")
        return ProjectState(
            base_commit=base_commit,
            base_ref=base_ref,
            workspace=self.workspace,
            ecosystem=self.ecosystem,
            package_manager=self.profile.name,
            lockfile_path=self.profile.lockfiles[0],
            target_locked_versions=target_versions,
            manifest_documents=documents,
            profile_instructions=self.profile.instructions,
        )

    def execute(
        self,
        request: UpdateRequest,
        plan: FileFixPlan,
        *,
        attempt_number: int,
        base_commit: str,
    ) -> AttemptResult:
        with GitSandbox(
            self.repo,
            self.workspace,
            self.events,
            backend=self.sandbox_backend,
            base_commit=base_commit,
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
            target_verifications = self.evaluate_targets(request, before, after)
            failed_targets = [
                verification.target.package
                for verification in target_verifications
                if not verification.satisfied
            ]
            if failed_targets:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="verification",
                    error=f"targets did not satisfy their version policies: {failed_targets}",
                    stdout=resolved.stdout,
                    stderr=resolved.stderr,
                    target_verifications=target_verifications,
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
                    target_verifications=target_verifications,
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
                    target_verifications=target_verifications,
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
                    target_verifications=target_verifications,
                )
            self.events.emit("stage", "Adding profile-verified dependency files")
            return AttemptResult(
                number=attempt_number,
                plan=plan,
                success=True,
                phase="completed",
                stdout=resolved.stdout,
                stderr=resolved.stderr,
                target_verifications=target_verifications,
                changed_files=changed_files,
                diff=diff,
            )

    def _workdir(self, root: Path) -> Path:
        workdir = (root / self.workspace).resolve()
        if not workdir.is_relative_to(root.resolve()) or not workdir.is_dir():
            raise ValueError(f"workspace does not exist: {self.workspace}")
        return workdir

    def _repository_state(self) -> tuple[str, str | None]:
        status = run_command(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=self.repo,
        )
        if not status.ok:
            raise ValueError(f"could not inspect repository state: {status.stderr}")
        if status.stdout.strip():
            raise ValueError("repository must have a clean working tree before inspection")
        commit = run_command(["git", "rev-parse", "--verify", "HEAD"], cwd=self.repo)
        if not commit.ok:
            raise ValueError(f"could not resolve repository HEAD: {commit.stderr}")
        branch = run_command(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=self.repo,
        )
        base_ref = branch.stdout.strip() if branch.ok else None
        return commit.stdout.strip(), base_ref

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
        request: UpdateRequest,
    ) -> dict[str, list[str]]:
        return {
            target.package: self._locked_versions(workdir, target.package)
            for target in request.targets
        }

    def request_is_satisfied(
        self,
        request: UpdateRequest,
        versions: dict[str, list[str]],
    ) -> bool:
        for target in request.targets:
            current_versions = versions.get(target.package, [])
            if not current_versions or not all(
                self._satisfies(version, target) for version in current_versions
            ):
                return False
        return True

    def evaluate_targets(
        self,
        request: UpdateRequest,
        before: dict[str, list[str]],
        after: dict[str, list[str]],
    ) -> list[TargetVerification]:
        verifications: list[TargetVerification] = []
        for target in request.targets:
            versions_before = before.get(target.package, [])
            versions_after = after.get(target.package, [])
            error: str | None = None
            if not versions_before:
                satisfied = False
                outcome = "missing"
            elif not versions_after:
                satisfied = target.allow_removal
                outcome = "removed" if satisfied else "removal_disallowed"
            else:
                try:
                    satisfied = all(self._satisfies(version, target) for version in versions_after)
                    outcome = "satisfied" if satisfied else "constraint_mismatch"
                except VersionComparisonError as exc:
                    satisfied = False
                    outcome = "invalid_version"
                    error = str(exc)
            verifications.append(
                TargetVerification(
                    target=target,
                    versions_before=versions_before,
                    versions_after=versions_after,
                    satisfied=satisfied,
                    outcome=outcome,
                    error=error,
                )
            )
        return verifications

    def _repo_path(self, path: str) -> str:
        return str(PurePosixPath(self.workspace) / PurePosixPath(path))

    def _satisfies(self, actual: str, target: DependencyTarget) -> bool:
        return satisfies(actual, target.requirement, self.profile.version_scheme)

    def _validate_comparable_versions(
        self,
        request: UpdateRequest,
        versions: dict[str, list[str]],
    ) -> None:
        try:
            for target in request.targets:
                for version in versions[target.package]:
                    self._satisfies(version, target)
        except VersionComparisonError as exc:
            raise ValueError(
                f"profile {self.profile.name} cannot evaluate the requested version policy: {exc}"
            ) from exc

    @staticmethod
    def _preserve_final_newline(original: str, replacement: str) -> str:
        if original.endswith("\n") and not replacement.endswith("\n"):
            return f"{replacement}\n"
        if not original.endswith("\n") and replacement.endswith("\n"):
            return replacement.rstrip("\n")
        return replacement
