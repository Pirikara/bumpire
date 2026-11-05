"""Bundler adapter: inspect, edit, resolve, verify, and stage a preview."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Literal

from packaging.version import InvalidVersion, Version

from depfix.bundler_lock import BundlerLock
from depfix.docker_runtime import ContainerCommand
from depfix.events import EventLogger
from depfix.gemfile import apply_edits
from depfix.models import AttemptResult, DependencyIssue, FixPlan, ProjectState
from depfix.sandbox import GitSandbox


class BundlerAdapter:
    ecosystem = "bundler"
    manifest_name = "Gemfile"
    lockfile_name = "Gemfile.lock"

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
            raise ValueError(f"Bundler adapter cannot handle {issue.ecosystem}")
        workdir = (self.repo / self.workspace).resolve()
        if not workdir.is_relative_to(self.repo) or not workdir.is_dir():
            raise ValueError(f"workspace does not exist: {self.workspace}")
        manifest = workdir / self.manifest_name
        lockfile = workdir / self.lockfile_name
        if not manifest.is_file() or not lockfile.is_file():
            raise ValueError("Bundler project requires Gemfile and Gemfile.lock")
        self.events.emit("detect", "Detected usage of Bundler package manager")
        self.events.emit("manifest", f"Viewing file {self.workspace}/{self.manifest_name}")
        manifest_text = manifest.read_text(encoding="utf-8")
        lock = BundlerLock.parse(lockfile.read_text(encoding="utf-8"))
        self.events.emit("lockfile", "Checking Bundler lockfile versions")
        return lock.project_state(
            manifest_path=self.manifest_name,
            lockfile_path=self.lockfile_name,
            manifest_text=manifest_text,
            target=issue.package,
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
            before_lock = BundlerLock.parse(lockfile_path.read_text(encoding="utf-8"))
            target_before = (
                before_lock.specs[issue.package].version
                if issue.package in before_lock.specs
                else None
            )

            self.events.emit(
                "agent",
                "Running constrained Bundler dependency update loop",
                attempt=attempt_number,
            )
            updated = apply_edits(manifest_path.read_text(encoding="utf-8"), plan.edits)
            manifest_path.write_text(updated, encoding="utf-8")
            self.events.emit("edit", f"Updating file {self.workspace}/{self.manifest_name}")
            staged_manifest = sandbox.stage([self.manifest_name])
            if not staged_manifest.ok:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="manifest",
                    error="failed to stage Gemfile",
                    stderr=staged_manifest.stderr,
                    target_before=target_before,
                )

            command = self._bundle_command(before_lock)
            self.events.emit("resolve", f"Updating {issue.package} in lockfile")
            resolved = sandbox.package_command(
                command,
                container=self._bundle_container(before_lock),
                timeout=600,
                local_env={"BUNDLE_GEMFILE": str(manifest_path)},
            )
            if not resolved.ok:
                return AttemptResult(
                    number=attempt_number,
                    plan=plan,
                    success=False,
                    phase="resolution",
                    error="Bundler could not resolve the proposed dependency graph",
                    stdout=resolved.stdout,
                    stderr=resolved.stderr,
                    target_before=target_before,
                )

            self.events.emit("lockfile", "Checking Bundler lockfile versions")
            after_lock = BundlerLock.parse(lockfile_path.read_text(encoding="utf-8"))
            target_after = (
                after_lock.specs[issue.package].version
                if issue.package in after_lock.specs
                else None
            )
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

            staged = sandbox.stage([self.manifest_name, self.lockfile_name])
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
            allowed = {
                str(Path(self.workspace) / self.manifest_name),
                str(Path(self.workspace) / self.lockfile_name),
            }
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
            self.events.emit("stage", "Adding verified manifest and lockfile to staging area")
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

    def _bundle_command(self, lock: BundlerLock) -> list[str]:
        ruby_version_file = self.repo / self.workspace / ".ruby-version"
        ruby_version = (
            ruby_version_file.read_text(encoding="utf-8").strip()
            if ruby_version_file.is_file()
            else ""
        )
        bundle = "bundle"
        if lock.bundler_version:
            bundle = f"bundle _{lock.bundler_version}_"
        bundle_parts = bundle.split()
        if ruby_version and shutil.which("mise"):
            return ["mise", "x", f"ruby@{ruby_version}", "--", *bundle_parts, "lock"]
        return [*bundle_parts, "lock"]

    def _bundle_container(self, lock: BundlerLock) -> ContainerCommand:
        ruby_version_file = self.repo / self.workspace / ".ruby-version"
        ruby_version = (
            ruby_version_file.read_text(encoding="utf-8").strip()
            if ruby_version_file.is_file()
            else "3.3"
        )
        ruby_version = ruby_version.removeprefix("ruby-")
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", ruby_version):
            raise ValueError(f"unsupported Ruby version for Docker: {ruby_version!r}")

        bundler_version = lock.bundler_version or ""
        if bundler_version and not re.fullmatch(r"[0-9A-Za-z.+-]+", bundler_version):
            raise ValueError(f"unsafe Bundler version: {bundler_version!r}")
        script = """
mkdir -p "$HOME" /tmp/gems
export GEM_HOME=/tmp/gems
export PATH="$GEM_HOME/bin:$PATH"
if [ -n "${DEPFIX_BUNDLER_VERSION:-}" ]; then
  gem install bundler --version "$DEPFIX_BUNDLER_VERSION" --no-document
  exec bundle "_${DEPFIX_BUNDLER_VERSION}_" lock
fi
exec bundle lock
""".strip()
        return ContainerCommand(
            image=f"ruby:{ruby_version}-slim",
            args=("sh", "-ceu", script, "depfix-bundler"),
            environment={
                "BUNDLE_GEMFILE": "Gemfile",
                "DEPFIX_BUNDLER_VERSION": bundler_version,
            },
        )

    @staticmethod
    def _at_least(actual: str, minimum: str) -> bool:
        try:
            return Version(actual) >= Version(minimum)
        except InvalidVersion:
            return actual == minimum
