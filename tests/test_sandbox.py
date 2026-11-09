import subprocess
from io import StringIO

from bumpire.events import EventLogger
from bumpire.sandbox import GitSandbox


def _git(repo, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_changed_files_includes_staged_unstaged_and_untracked(tmp_path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "manifest.txt").write_text("before\n", encoding="utf-8")
    (repo / "source.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")

    with GitSandbox(repo, ".", EventLogger(stream=StringIO())) as sandbox:
        assert sandbox.workdir is not None
        (sandbox.workdir / "manifest.txt").write_text("staged\n", encoding="utf-8")
        sandbox.stage(["manifest.txt"])
        (sandbox.workdir / "source.txt").write_text("unstaged\n", encoding="utf-8")
        (sandbox.workdir / "unexpected.txt").write_text("untracked\n", encoding="utf-8")

        assert sandbox.changed_files() == [
            "manifest.txt",
            "source.txt",
            "unexpected.txt",
        ]


def test_sandbox_is_pinned_to_the_requested_base_commit(tmp_path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "manifest.txt").write_text("first\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "first")
    first_commit = _git(repo, "rev-parse", "HEAD")
    (repo / "manifest.txt").write_text("second\n", encoding="utf-8")
    _git(repo, "commit", "-am", "second")

    with GitSandbox(
        repo,
        ".",
        EventLogger(stream=StringIO()),
        base_commit=first_commit,
    ) as sandbox:
        assert sandbox.workdir is not None
        assert (sandbox.workdir / "manifest.txt").read_text(encoding="utf-8") == "first\n"
        assert sandbox.command(["git", "rev-parse", "HEAD"]).stdout.strip() == first_commit


def test_staged_diff_is_not_truncated_at_the_command_log_limit(tmp_path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "manifest.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")

    with GitSandbox(repo, ".", EventLogger(stream=StringIO())) as sandbox:
        assert sandbox.workdir is not None
        (sandbox.workdir / "manifest.txt").write_text("x\n" * 20_000, encoding="utf-8")
        sandbox.stage(["manifest.txt"])
        diff = sandbox.staged_diff()

    assert diff.startswith("diff --git")
    assert len(diff) > 20_000
