import subprocess
from io import StringIO

from depfix.events import EventLogger
from depfix.sandbox import GitSandbox


def _git(repo, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


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
