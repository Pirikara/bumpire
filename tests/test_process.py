import subprocess

from depfix.process import run_command


def test_timeout_becomes_bounded_command_result(monkeypatch, tmp_path) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["resolver"],
            timeout=3,
            output="partial stdout",
            stderr="partial stderr",
        )

    monkeypatch.setattr(subprocess, "run", timeout)

    result = run_command(["resolver"], cwd=tmp_path, timeout=3)

    assert result.returncode == 124
    assert result.stdout == "partial stdout"
    assert result.stderr.endswith("command timed out after 3 seconds")
