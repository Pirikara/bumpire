"""Publish verified update artifacts as idempotent GitHub draft pull requests."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from bumpire.events import EventLogger
from bumpire.models import VerifiedUpdateArtifact
from bumpire.process import run_command

_GIT = ["git", "-c", "core.hooksPath=/dev/null"]
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    host: str
    owner: str
    name: str

    @property
    def gh_slug(self) -> str:
        slug = f"{self.owner}/{self.name}"
        return slug if self.host == "github.com" else f"{self.host}/{slug}"


@dataclass(frozen=True, slots=True)
class PullRequestReference:
    url: str
    state: str


@dataclass(frozen=True, slots=True)
class PublicationResult:
    branch: str
    commit: str
    pull_request_url: str
    reused_branch: bool
    reused_pull_request: bool


class PullRequestClient(Protocol):
    def preflight(self) -> None: ...

    def find(self, branch: str) -> PullRequestReference | None: ...

    def create(self, *, base: str, head: str, title: str, body: str) -> str: ...


class GhPullRequestClient:
    def __init__(self, repository: GitHubRepository, cwd: Path) -> None:
        self.repository = repository
        self.cwd = cwd

    def preflight(self) -> None:
        if shutil.which("gh") is None:
            raise RuntimeError("GitHub publishing requires the gh CLI")
        result = run_command(
            ["gh", "repo", "view", self.repository.gh_slug, "--json", "nameWithOwner"],
            cwd=self.cwd,
            timeout=30,
        )
        if not result.ok:
            raise RuntimeError("GitHub authentication or repository access check failed")

    def find(self, branch: str) -> PullRequestReference | None:
        result = run_command(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repository.gh_slug,
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "1",
                "--json",
                "url,state",
            ],
            cwd=self.cwd,
            timeout=30,
        )
        if not result.ok:
            raise RuntimeError("could not check for an existing GitHub pull request")
        try:
            pulls = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub CLI returned invalid pull request data") from exc
        if not pulls:
            return None
        return PullRequestReference(url=pulls[0]["url"], state=pulls[0]["state"])

    def create(self, *, base: str, head: str, title: str, body: str) -> str:
        result = run_command(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                self.repository.gh_slug,
                "--base",
                base,
                "--head",
                head,
                "--draft",
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=self.cwd,
            timeout=60,
        )
        if not result.ok:
            raise RuntimeError("GitHub draft pull request creation failed")
        url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if not url.startswith(("https://", "http://")):
            raise RuntimeError("GitHub CLI did not return a pull request URL")
        return url


class GitHubPublisher:
    def __init__(
        self,
        source_repo: Path,
        events: EventLogger,
        *,
        remote: str = "origin",
        pull_requests: PullRequestClient | None = None,
        repository: GitHubRepository | None = None,
    ) -> None:
        self.source_repo = source_repo.resolve()
        self.events = events
        if _REMOTE_NAME.fullmatch(remote) is None:
            raise ValueError("remote must be a simple Git remote name")
        self.remote = remote
        self.remote_url = self._remote_url()
        self.repository = repository or parse_github_remote(self.remote_url)
        self.pull_requests = pull_requests or GhPullRequestClient(
            self.repository,
            self.source_repo,
        )

    def publish(
        self,
        artifact: VerifiedUpdateArtifact,
        *,
        base_ref: str | None = None,
    ) -> PublicationResult:
        base = base_ref or artifact.base_ref
        if base is None:
            raise ValueError("publishing a detached artifact requires an explicit base branch")
        self._validate_branch(base)
        self.pull_requests.preflight()
        self._verify_remote_base(base, artifact.base_commit)

        branch = publication_branch(artifact)
        title = publication_title(artifact)
        body = publication_body(artifact)
        self.events.emit(
            "publish",
            f"Preparing verified update for {self.repository.owner}/{self.repository.name}",
            base=base,
            branch=branch,
        )

        with tempfile.TemporaryDirectory(prefix="bumpire-publish-") as temporary:
            root = Path(temporary)
            clone = root / "repository"
            patch = root / "update.patch"
            patch.write_text(artifact.diff, encoding="utf-8")
            self._clone_base(clone, base)
            self._run_git(clone, "checkout", "--detach", artifact.base_commit)
            self._run_git(clone, "checkout", "-b", branch)
            self._run_git(clone, "apply", "--index", "--whitespace=nowarn", str(patch))
            self._verify_applied_artifact(clone, artifact)
            self._configure_identity(clone)
            self._run_git(clone, "commit", "-m", title)
            candidate_commit = self._git_output(clone, "rev-parse", "HEAD")
            candidate_tree = self._git_output(clone, "rev-parse", "HEAD^{tree}")
            self._verify_remote_base(base, artifact.base_commit)
            remote_commit = self._remote_branch_commit(clone, branch)
            reused_branch = remote_commit is not None
            if remote_commit is not None:
                self._verify_existing_branch(
                    clone,
                    branch=branch,
                    remote_commit=remote_commit,
                    base_commit=artifact.base_commit,
                    candidate_tree=candidate_tree,
                )
                published_commit = remote_commit
            else:
                pushed = run_command(
                    [*_GIT, "push", "origin", f"HEAD:refs/heads/{branch}"],
                    cwd=clone,
                    timeout=120,
                )
                if not pushed.ok:
                    remote_commit = self._remote_branch_commit(clone, branch)
                    if remote_commit is None:
                        raise RuntimeError("could not push the verified update branch")
                    self._verify_existing_branch(
                        clone,
                        branch=branch,
                        remote_commit=remote_commit,
                        base_commit=artifact.base_commit,
                        candidate_tree=candidate_tree,
                    )
                    reused_branch = True
                    published_commit = remote_commit
                else:
                    published_commit = candidate_commit

        existing = self.pull_requests.find(branch)
        if existing is not None:
            self.events.emit("publish", f"Reusing existing pull request {existing.url}")
            return PublicationResult(
                branch=branch,
                commit=published_commit,
                pull_request_url=existing.url,
                reused_branch=reused_branch,
                reused_pull_request=True,
            )
        url = self.pull_requests.create(base=base, head=branch, title=title, body=body)
        self.events.emit("publish", f"Created draft pull request {url}")
        return PublicationResult(
            branch=branch,
            commit=published_commit,
            pull_request_url=url,
            reused_branch=reused_branch,
            reused_pull_request=False,
        )

    def _remote_url(self) -> str:
        result = run_command(
            [*_GIT, "remote", "get-url", self.remote],
            cwd=self.source_repo,
        )
        if not result.ok or not result.stdout.strip():
            raise ValueError(f"Git remote does not exist: {self.remote}")
        return result.stdout.strip()

    def _verify_remote_base(self, base: str, expected_commit: str) -> None:
        result = run_command(
            [*_GIT, "ls-remote", self.remote, f"refs/heads/{base}"],
            cwd=self.source_repo,
            timeout=60,
        )
        if not result.ok:
            raise RuntimeError("could not resolve the remote base branch")
        lines = [line for line in result.stdout.splitlines() if line]
        actual = lines[0].split()[0].lower() if len(lines) == 1 else None
        if actual != expected_commit:
            raise RuntimeError(
                "remote base branch has moved; generate a new verified update before publishing"
            )

    def _clone_base(self, destination: Path, base: str) -> None:
        result = run_command(
            [
                *_GIT,
                "clone",
                "--filter=blob:limit=20m",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                base,
                "--no-checkout",
                self.remote_url,
                str(destination),
            ],
            cwd=destination.parent,
            timeout=120,
        )
        if not result.ok:
            raise RuntimeError("could not clone the remote base branch for publishing")

    def _verify_applied_artifact(
        self,
        clone: Path,
        artifact: VerifiedUpdateArtifact,
    ) -> None:
        files = self._git_output(clone, "diff", "--cached", "--name-only").splitlines()
        if files != artifact.changed_files:
            raise RuntimeError("applied patch changed files outside the verified artifact")
        diff = self._git_output(clone, "diff", "--cached", "--no-ext-diff", preserve=True)
        digest = hashlib.sha256(diff.encode()).hexdigest()
        if digest != artifact.diff_sha256 or diff != artifact.diff:
            raise RuntimeError("applied patch does not match the verified artifact")

    def _configure_identity(self, clone: Path) -> None:
        self._run_git(clone, "config", "user.name", "bumpire[bot]")
        self._run_git(clone, "config", "user.email", "bumpire[bot]@users.noreply.github.com")

    def _remote_branch_commit(self, clone: Path, branch: str) -> str | None:
        result = run_command(
            [*_GIT, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=clone,
            timeout=60,
        )
        if not result.ok:
            raise RuntimeError("could not check the remote update branch")
        line = result.stdout.strip()
        return line.split()[0].lower() if line else None

    def _verify_existing_branch(
        self,
        clone: Path,
        *,
        branch: str,
        remote_commit: str,
        base_commit: str,
        candidate_tree: str,
    ) -> None:
        fetched = run_command(
            [*_GIT, "fetch", "--depth", "2", "origin", f"refs/heads/{branch}"],
            cwd=clone,
            timeout=60,
        )
        if not fetched.ok:
            raise RuntimeError("could not inspect the existing remote update branch")
        actual_commit = self._git_output(clone, "rev-parse", "FETCH_HEAD")
        commit_line = self._git_output(clone, "rev-list", "--parents", "-n", "1", "FETCH_HEAD")
        commit_parts = commit_line.split()
        actual_parent = commit_parts[1] if len(commit_parts) == 2 else None
        actual_tree = self._git_output(clone, "rev-parse", "FETCH_HEAD^{tree}")
        if (
            actual_commit != remote_commit
            or actual_parent != base_commit
            or actual_tree != candidate_tree
        ):
            raise RuntimeError(f"remote branch collision: {branch}")

    def _validate_branch(self, branch: str) -> None:
        if not branch or any(character in branch for character in ("\x00", "\n", "\r")):
            raise ValueError("base branch must be a non-empty single-line value")
        result = run_command(
            [*_GIT, "check-ref-format", "--branch", branch],
            cwd=self.source_repo,
        )
        if not result.ok:
            raise ValueError(f"invalid base branch: {branch!r}")

    @staticmethod
    def _run_git(cwd: Path, *args: str) -> None:
        result = run_command([*_GIT, *args], cwd=cwd, timeout=120)
        if not result.ok:
            raise RuntimeError(f"Git publishing step failed: {args[0]}")

    @staticmethod
    def _git_output(cwd: Path, *args: str, preserve: bool = False) -> str:
        result = run_command(
            [*_GIT, *args],
            cwd=cwd,
            timeout=120,
            output_limit=5_000_000 if preserve else 20_000,
        )
        if not result.ok:
            raise RuntimeError(f"Git publishing step failed: {args[0]}")
        return result.stdout if preserve else result.stdout.strip()


def parse_github_remote(remote_url: str) -> GitHubRepository:
    if "://" in remote_url:
        parsed = urlparse(remote_url)
        host = parsed.hostname or ""
        path = parsed.path
    else:
        match = re.fullmatch(r"(?:[^@]+@)?(?P<host>[^:]+):(?P<path>.+)", remote_url)
        if match is None:
            raise ValueError("Git remote is not a supported GitHub URL")
        host = match.group("host")
        path = match.group("path")
    parts = [part for part in path.strip("/").removesuffix(".git").split("/") if part]
    if not host or len(parts) != 2:
        raise ValueError("Git remote must identify one GitHub owner and repository")
    return GitHubRepository(host=host.lower(), owner=parts[0], name=parts[1])


def publication_branch(artifact: VerifiedUpdateArtifact) -> str:
    packages = "-".join(sorted(target.package for target in artifact.request.targets)[:2])
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", packages).strip(".-").lower() or "dependencies"
    slug = slug[:48].rstrip(".-")
    identity = json.dumps(
        {
            "base_commit": artifact.base_commit,
            "workspace": artifact.workspace,
            "request": {
                "ecosystem": artifact.request.ecosystem,
                "targets": sorted(
                    (target.model_dump(mode="json") for target in artifact.request.targets),
                    key=lambda target: target["package"],
                ),
            },
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"bumpire/{slug}-{digest}"


def publication_title(artifact: VerifiedUpdateArtifact) -> str:
    targets = artifact.request.targets
    if len(targets) == 1:
        target = targets[0]
        operator = "" if target.requirement.kind == "exact" else ">="
        return f"Update {target.package} to {operator}{target.requirement.version}"
    return f"Update {len(targets)} dependencies with Bumpire"


def publication_body(artifact: VerifiedUpdateArtifact) -> str:
    rows = []
    for verification in artifact.target_verifications:
        target = verification.target
        operator = "==" if target.requirement.kind == "exact" else ">="
        before = ", ".join(verification.versions_before)
        after = ", ".join(verification.versions_after) or "removed"
        rows.append(
            f"| `{_markdown(target.package)}` "
            f"| `{operator}{_markdown(target.requirement.version)}` "
            f"| `{_markdown(before)}` | `{_markdown(after)}` |"
        )
    return "\n".join(
        [
            "## Dependency update",
            "",
            "| Package | Policy | Before | After |",
            "|---|---:|---:|---:|",
            *rows,
            "",
            "## Bumpire verification",
            "",
            f"- Base commit: `{artifact.base_commit}`",
            f"- Workspace: `{_markdown(artifact.workspace)}`",
            f"- Package-manager profile: `{_markdown(artifact.package_manager)}`",
            f"- Patch SHA-256: `{artifact.diff_sha256}`",
            f"- Changed files: {len(artifact.changed_files)}",
            "",
            "Bumpire verified dependency resolution, requested version policies, and the",
            "changed-file allowlist. Application compatibility remains the responsibility",
            "of this repository's CI.",
        ]
    )


def _markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("`", "\\`")
