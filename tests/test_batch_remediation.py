from io import StringIO
from pathlib import Path

from bumpire.events import EventLogger
from bumpire.models import DependencyTarget, RemediationRequest
from bumpire.profile import load_builtin_profiles
from bumpire.profile_executor import ProfileExecutor


def _executor(tmp_path: Path) -> ProfileExecutor:
    npm = next(profile for profile in load_builtin_profiles() if profile.name == "npm")
    return ProfileExecutor(tmp_path, ".", EventLogger(stream=StringIO()), npm)


def _request() -> RemediationRequest:
    return RemediationRequest(
        ecosystem="npm",
        targets=[
            DependencyTarget(package="alpha", minimum_version="2.0.0"),
            DependencyTarget(package="beta", minimum_version="3.0.0"),
        ],
    )


def test_batch_is_satisfied_only_when_every_occurrence_is_safe(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    request = _request()

    assert executor.request_is_satisfied(
        request,
        {"alpha": ["2.0.0", "2.1.0"], "beta": ["3.0.1"]},
    )
    assert not executor.request_is_satisfied(
        request,
        {"alpha": ["2.0.0", "1.9.9"], "beta": ["3.0.1"]},
    )
    assert not executor.request_is_satisfied(request, {"alpha": ["2.0.0"], "beta": []})


def test_batch_evaluation_reports_partial_failure_atomically(tmp_path: Path) -> None:
    resolutions = _executor(tmp_path).evaluate_targets(
        _request(),
        {"alpha": ["1.0.0"], "beta": ["2.0.0"]},
        {"alpha": ["2.0.0"], "beta": ["2.9.0"]},
    )

    assert [resolution.satisfied for resolution in resolutions] == [True, False]
    assert [resolution.outcome for resolution in resolutions] == ["safe", "below_minimum"]


def test_transitive_target_removal_is_a_satisfied_outcome(tmp_path: Path) -> None:
    resolutions = _executor(tmp_path).evaluate_targets(
        _request(),
        {"alpha": ["1.0.0"], "beta": ["2.0.0"]},
        {"alpha": [], "beta": ["3.0.0"]},
    )

    assert resolutions[0].satisfied is True
    assert resolutions[0].outcome == "removed"
