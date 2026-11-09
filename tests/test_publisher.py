import hashlib
import subprocess
from io import StringIO
from pathlib import Path

import pytest

from bumpire.events import EventLogger
from bumpire.models import (
    DependencyTarget,
    TargetVerification,
    UpdateRequest,
    VerifiedUpdateArtifact,
    VersionRequirement,
)
from bumpire.process import CommandResult
from bumpire.publisher import (
    GhPullRequestClient,
    GitHubPublisher,
    GitHubRepository,
    PullRequestReference,
    parse_github_remote,
    publication_body,
    publication_branch,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


class FakePullRequests:
    def __init__(self) -> None:
        self.pulls: dict[str, PullRequestReference] = {}
        self.preflight_calls = 0
        self.create_calls = 0

    def preflight(self) -> None:
        self.preflight_calls += 1

    def find(self, branch: str) -> PullRequestReference | None:
        return self.pulls.get(branch)

    def create(self, *, base: str, head: str, title: str, body: str) -> str:
        self.create_calls += 1
        url = f"https://github.test/example/project/pull/{self.create_calls}"
        self.pulls[head] = PullRequestReference(url=url, state="OPEN")
        assert base == "main"
        assert title == "Update lodash to 4.18.1"
        assert "Application compatibility" in body
        return url


def _repositories(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.invalid")
    (source / "manifest.txt").write_text("lodash=4.17.20\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(source), str(remote))
    _git(source, "remote", "add", "origin", str(remote))
    return source, remote


def _artifact(source: Path, *, replacement: str = "lodash=4.18.1\n") -> VerifiedUpdateArtifact:
    base_commit = _git(source, "rev-parse", "HEAD").strip()
    path = source / "manifest.txt"
    original = path.read_text(encoding="utf-8")
    path.write_text(replacement, encoding="utf-8")
    diff = _git(source, "diff", "--no-ext-diff")
    path.write_text(original, encoding="utf-8")
    target = DependencyTarget(
        package="lodash",
        requirement=VersionRequirement(version="4.18.1"),
    )
    request = UpdateRequest(ecosystem="npm", targets=[target])
    return VerifiedUpdateArtifact(
        base_commit=base_commit,
        base_ref="main",
        workspace=".",
        ecosystem="npm",
        package_manager="npm",
        request=request,
        target_verifications=[
            TargetVerification(
                target=target,
                versions_before=["4.17.20"],
                versions_after=["4.18.1"],
                satisfied=True,
                outcome="satisfied",
            )
        ],
        changed_files=["manifest.txt"],
        diff=diff,
        diff_sha256=hashlib.sha256(diff.encode()).hexdigest(),
    )


def _publisher(source: Path, pulls: FakePullRequests) -> GitHubPublisher:
    return GitHubPublisher(
        source,
        EventLogger(stream=StringIO()),
        pull_requests=pulls,
        repository=GitHubRepository(host="github.test", owner="example", name="project"),
    )


def test_publisher_pushes_verified_branch_and_creates_draft_pr(tmp_path: Path) -> None:
    source, remote = _repositories(tmp_path)
    artifact = _artifact(source)
    pulls = FakePullRequests()

    result = _publisher(source, pulls).publish(artifact)

    assert result.reused_branch is False
    assert result.reused_pull_request is False
    assert result.pull_request_url.endswith("/pull/1")
    assert pulls.preflight_calls == 1
    assert pulls.create_calls == 1
    assert _git(remote, "show", f"refs/heads/{result.branch}:manifest.txt") == "lodash=4.18.1\n"
    assert _git(source, "status", "--porcelain") == ""


def test_publisher_is_idempotent_for_the_same_artifact(tmp_path: Path) -> None:
    source, _ = _repositories(tmp_path)
    artifact = _artifact(source)
    pulls = FakePullRequests()
    publisher = _publisher(source, pulls)

    first = publisher.publish(artifact)
    second = publisher.publish(artifact)

    assert second.branch == first.branch
    assert second.commit == first.commit
    assert second.pull_request_url == first.pull_request_url
    assert second.reused_branch is True
    assert second.reused_pull_request is True
    assert pulls.create_calls == 1


def test_publisher_rejects_remote_base_drift_before_push(tmp_path: Path) -> None:
    source, remote = _repositories(tmp_path)
    artifact = _artifact(source)
    (source / "other.txt").write_text("new base\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "advance base")
    _git(source, "push", "origin", "main")

    with pytest.raises(RuntimeError, match="base branch has moved"):
        _publisher(source, FakePullRequests()).publish(artifact)

    assert _git(remote, "for-each-ref", "--format=%(refname)", "refs/heads/bumpire/") == ""


def test_publisher_rejects_a_deterministic_branch_collision(tmp_path: Path) -> None:
    source, _ = _repositories(tmp_path)
    artifact = _artifact(source)
    publisher = _publisher(source, FakePullRequests())
    publisher.publish(artifact)
    conflicting = _artifact(source, replacement="lodash=4.18.1\nunrelated=true\n")

    with pytest.raises(RuntimeError, match="branch collision"):
        publisher.publish(conflicting)


@pytest.mark.parametrize(
    ("remote", "host", "owner", "name"),
    [
        ("https://github.com/Pirikara/bumpire.git", "github.com", "Pirikara", "bumpire"),
        ("git@github.com:Pirikara/bumpire.git", "github.com", "Pirikara", "bumpire"),
        (
            "ssh://git@github.example.com/team/project.git",
            "github.example.com",
            "team",
            "project",
        ),
    ],
)
def test_parse_github_remote(remote: str, host: str, owner: str, name: str) -> None:
    assert parse_github_remote(remote) == GitHubRepository(host=host, owner=owner, name=name)


def test_publication_metadata_is_deterministic_and_excludes_agent_rationale(tmp_path: Path) -> None:
    source, _ = _repositories(tmp_path)
    artifact = _artifact(source)

    assert publication_branch(artifact) == publication_branch(artifact)
    body = publication_body(artifact)
    assert "lodash" in body
    assert "4.17.20" in body
    assert "4.18.1" in body
    assert "rationale" not in body.lower()


def test_gh_client_always_creates_a_draft_pull_request(monkeypatch, tmp_path: Path) -> None:
    captured: list[str] = []

    def fake_run(args, *, cwd, timeout=300, **kwargs):
        captured.extend(args)
        return CommandResult(tuple(args), 0, "https://github.com/example/project/pull/1\n", "")

    monkeypatch.setattr("bumpire.publisher.run_command", fake_run)
    client = GhPullRequestClient(
        GitHubRepository(host="github.com", owner="example", name="project"),
        tmp_path,
    )

    url = client.create(base="main", head="bumpire/update", title="Update", body="Body")

    assert url.endswith("/pull/1")
    assert "--draft" in captured


def test_gh_client_preflight_uses_repo_view_positional_argument(
    monkeypatch, tmp_path: Path
) -> None:
    captured: list[str] = []

    def fake_run(args, *, cwd, timeout=300, **kwargs):
        captured.extend(args)
        return CommandResult(tuple(args), 0, '{"nameWithOwner":"example/project"}\n', "")

    monkeypatch.setattr("bumpire.publisher.shutil.which", lambda executable: "/usr/bin/gh")
    monkeypatch.setattr("bumpire.publisher.run_command", fake_run)
    client = GhPullRequestClient(
        GitHubRepository(host="github.com", owner="example", name="project"),
        tmp_path,
    )

    client.preflight()

    assert captured[:4] == ["gh", "repo", "view", "example/project"]
    assert "--repo" not in captured
