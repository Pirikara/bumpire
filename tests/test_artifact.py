import hashlib

import pytest
from pydantic import ValidationError

from bumpire.models import (
    AttemptResult,
    DependencyTarget,
    FileFixPlan,
    FileReplacement,
    ProjectState,
    TargetVerification,
    UpdateRequest,
    UpdateResult,
    VerifiedUpdateArtifact,
    VersionRequirement,
)


def _request() -> UpdateRequest:
    return UpdateRequest(
        ecosystem="npm",
        targets=[
            DependencyTarget(
                package="lodash",
                requirement=VersionRequirement(version="4.18.1"),
            )
        ],
    )


def _attempt() -> AttemptResult:
    target = _request().targets[0]
    return AttemptResult(
        number=1,
        plan=FileFixPlan(
            rationale="update lodash",
            files=[FileReplacement(path="package.json", content="{}\n")],
        ),
        success=True,
        phase="completed",
        target_verifications=[
            TargetVerification(
                target=target,
                versions_before=["4.17.20"],
                versions_after=["4.18.1"],
                satisfied=True,
                outcome="satisfied",
            )
        ],
        changed_files=["package-lock.json", "package.json"],
        diff="diff --git a/package.json b/package.json\n",
    )


def _project() -> ProjectState:
    return ProjectState(
        base_commit="a" * 40,
        base_ref="main",
        workspace=".",
        ecosystem="npm",
        package_manager="npm",
        lockfile_path="package-lock.json",
        target_locked_versions={"lodash": ["4.17.20"]},
    )


def test_verified_artifact_binds_diff_to_base_commit_and_request() -> None:
    artifact = VerifiedUpdateArtifact.from_attempt(
        project=_project(),
        request=_request(),
        attempt=_attempt(),
    )

    assert artifact.base_commit == "a" * 40
    assert artifact.base_ref == "main"
    assert artifact.diff_sha256 == hashlib.sha256(artifact.diff.encode()).hexdigest()
    assert artifact.request.targets[0].package == "lodash"


def test_verified_artifact_rejects_tampered_diff() -> None:
    artifact = VerifiedUpdateArtifact.from_attempt(
        project=_project(),
        request=_request(),
        attempt=_attempt(),
    )
    payload = artifact.model_dump()
    payload["diff"] = "tampered"

    with pytest.raises(ValidationError, match="does not match"):
        VerifiedUpdateArtifact.model_validate(payload)


def test_verified_artifact_rejects_mismatched_target_evidence() -> None:
    artifact = VerifiedUpdateArtifact.from_attempt(
        project=_project(),
        request=_request(),
        attempt=_attempt(),
    )
    payload = artifact.model_dump()
    payload["target_verifications"][0]["target"]["package"] = "other"

    with pytest.raises(ValidationError, match="do not match"):
        VerifiedUpdateArtifact.model_validate(payload)


def test_updated_result_requires_matching_artifact() -> None:
    attempt = _attempt()
    artifact = VerifiedUpdateArtifact.from_attempt(
        project=_project(),
        request=_request(),
        attempt=attempt,
    )

    result = UpdateResult(
        status="updated",
        request=_request(),
        attempts=[attempt],
        diff=attempt.diff,
        changed_files=attempt.changed_files,
        artifact=artifact,
    )

    assert result.artifact == artifact
    with pytest.raises(ValidationError, match="requires a verified"):
        UpdateResult(status="updated", request=_request(), attempts=[attempt])


def test_target_verification_rejects_contradictory_success_state() -> None:
    with pytest.raises(ValidationError, match="contradicts"):
        TargetVerification(
            target=_request().targets[0],
            versions_before=["4.17.20"],
            versions_after=["4.17.20"],
            satisfied=True,
            outcome="constraint_mismatch",
        )
