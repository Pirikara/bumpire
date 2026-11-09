from io import StringIO
from pathlib import Path

from bumpire.events import EventLogger
from bumpire.models import DependencyTarget, UpdateRequest, VersionRequirement
from bumpire.profile import load_builtin_profiles
from bumpire.profile_executor import ProfileExecutor


def _executor(tmp_path: Path) -> ProfileExecutor:
    npm = next(profile for profile in load_builtin_profiles() if profile.name == "npm")
    return ProfileExecutor(tmp_path, ".", EventLogger(stream=StringIO()), npm)


def _request(*, allow_alpha_removal: bool = False) -> UpdateRequest:
    return UpdateRequest(
        ecosystem="npm",
        targets=[
            DependencyTarget(
                package="alpha",
                requirement=VersionRequirement(kind="at_least", version="2.0.0"),
                allow_removal=allow_alpha_removal,
            ),
            DependencyTarget(
                package="beta",
                requirement=VersionRequirement(kind="at_least", version="3.0.0"),
            ),
        ],
    )


def test_batch_is_satisfied_only_when_every_occurrence_matches_policy(tmp_path: Path) -> None:
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


def test_exact_policy_rejects_a_later_version(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    request = UpdateRequest(
        ecosystem="npm",
        targets=[
            DependencyTarget(
                package="alpha",
                requirement=VersionRequirement(kind="exact", version="2.0.0"),
            )
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
        "constraint_mismatch",
    ]


def test_target_removal_is_rejected_by_default(tmp_path: Path) -> None:
    verifications = _executor(tmp_path).evaluate_targets(
        _request(),
        {"alpha": ["1.0.0"], "beta": ["2.0.0"]},
        {"alpha": [], "beta": ["3.0.0"]},
    )

    assert verifications[0].satisfied is False
    assert verifications[0].outcome == "removal_disallowed"


def test_target_removal_can_be_explicitly_allowed(tmp_path: Path) -> None:
    verifications = _executor(tmp_path).evaluate_targets(
        _request(allow_alpha_removal=True),
        {"alpha": ["1.0.0"], "beta": ["2.0.0"]},
        {"alpha": [], "beta": ["3.0.0"]},
    )

    assert verifications[0].satisfied is True
    assert verifications[0].outcome == "removed"
