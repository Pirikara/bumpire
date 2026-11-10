import json
import subprocess
from io import StringIO
from pathlib import Path

import pytest

from bumpire.events import EventLogger
from bumpire.models import DependencyTarget, UpdateRequest
from bumpire.profile import detect_profile
from bumpire.profile_executor import ProfileExecutor


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _npm_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "package.json").write_text(
        json.dumps({"dependencies": {"lodash": "4.17.20"}}),
        encoding="utf-8",
    )
    (repo / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/lodash": {"version": "4.17.20"}},
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _request() -> UpdateRequest:
    return UpdateRequest(
        ecosystem="npm",
        targets=[
            DependencyTarget(
                package="lodash",
                version="4.18.1",
            )
        ],
    )


def test_inspection_binds_project_state_to_repository_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _npm_repo(tmp_path)
    executor = ProfileExecutor(
        repo,
        ".",
        EventLogger(stream=StringIO()),
        detect_profile(repo, "."),
    )
    monkeypatch.setattr(
        executor,
        "_resolved_versions",
        lambda sandbox, request: {"lodash": ["4.17.20"]},
    )

    project = executor.inspect(_request())

    assert project.base_commit == _git(repo, "rev-parse", "HEAD")
    assert project.base_ref == "main"
    assert project.workspace == "."


def test_inspection_rejects_a_dirty_working_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _npm_repo(tmp_path)
    (repo / "notes.txt").write_text("untracked\n", encoding="utf-8")
    executor = ProfileExecutor(
        repo,
        ".",
        EventLogger(stream=StringIO()),
        detect_profile(repo, "."),
    )
    monkeypatch.setattr(
        executor,
        "_resolved_versions",
        lambda sandbox, request: {"lodash": ["4.17.20"]},
    )

    with pytest.raises(ValueError, match="clean working tree"):
        executor.inspect(_request())
