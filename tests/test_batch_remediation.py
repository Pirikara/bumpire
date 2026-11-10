from io import StringIO
from pathlib import Path

from bumpire.events import EventLogger
from bumpire.models import DependencyTarget, UpdateRequest
from bumpire.profile import load_builtin_profiles
from bumpire.profile_executor import ProfileExecutor


def _executor(tmp_path: Path) -> ProfileExecutor:
    npm = next(profile for profile in load_builtin_profiles() if profile.name == "npm")
    return ProfileExecutor(tmp_path, ".", EventLogger(stream=StringIO()), npm)


def _request() -> UpdateRequest:
    return UpdateRequest(
        ecosystem="npm",
        targets=[
            DependencyTarget(
                package="alpha",
                version="2.0.0",
            ),
            DependencyTarget(
                package="beta",
                version="3.0.0",
            ),
        ],
    )


def test_batch_is_satisfied_only_when_every_occurrence_matches_exact_version(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    request = _request()

    assert executor.request_is_satisfied(
        request,
        {"alpha": ["2.0.0"], "beta": ["3.0.0"]},
    )
    assert not executor.request_is_satisfied(
        request,
        {"alpha": ["2.0.0", "2.0.1"], "beta": ["3.0.0"]},
    )
    assert not executor.request_is_satisfied(request, {"alpha": ["2.0.0"], "beta": []})


def test_exact_policy_rejects_a_later_version(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    request = UpdateRequest(
        ecosystem="npm",
        targets=[
            DependencyTarget(package="alpha", version="2.0.0")
        ],
    )

    assert executor.request_is_satisfied(request, {"alpha": ["2.0.0"]})
    assert not executor.request_is_satisfied(request, {"alpha": ["2.0.1"]})


def test_batch_evaluation_reports_partial_failure_atomically(tmp_path: Path) -> None:
    verifications = _executor(tmp_path).evaluate_targets(
        _request(),
        {"alpha": ["1.0.0"], "beta": ["2.0.0"]},
        {"alpha": ["2.0.0"], "beta": ["2.9.0"]},
    )

    assert [verification.satisfied for verification in verifications] == [True, False]
    assert [verification.outcome for verification in verifications] == [
        "satisfied",
        "version_mismatch",
    ]


def test_target_missing_after_resolution_is_rejected(tmp_path: Path) -> None:
    verifications = _executor(tmp_path).evaluate_targets(
        _request(),
        {"alpha": ["1.0.0"], "beta": ["2.0.0"]},
        {"alpha": [], "beta": ["3.0.0"]},
    )

    assert verifications[0].satisfied is False
    assert verifications[0].outcome == "missing_after"


def test_target_missing_before_resolution_is_reported(tmp_path: Path) -> None:
    verifications = _executor(tmp_path).evaluate_targets(
        _request(),
        {"alpha": [], "beta": ["2.0.0"]},
        {"alpha": ["2.0.0"], "beta": ["3.0.0"]},
    )

    assert verifications[0].satisfied is False
    assert verifications[0].outcome == "missing_before"
