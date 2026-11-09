import subprocess

from bumpire.process import run_command


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


def test_command_can_opt_into_a_larger_bounded_output(tmp_path) -> None:
    result = run_command(
        ["python3", "-c", "print('x' * 30000)"],
        cwd=tmp_path,
        output_limit=40_000,
    )

    assert result.ok
    assert len(result.stdout) == 30_001
