from io import StringIO

import pytest

from bumpire.agent_runtime import (
    AgentAutofixRunner,
    AutofixAgentContext,
    build_model,
    try_dependency_fix,
)
from bumpire.events import EventLogger
from bumpire.models import (
    AttemptResult,
    DependencyTarget,
    FileFixPlan,
    FileReplacement,
    ProjectState,
    RemediationRequest,
    TargetResolution,
)


class SuccessfulExecutor:
    def execute(
        self,
        request: RemediationRequest,
        plan: FileFixPlan,
        *,
        attempt_number: int,
    ) -> AttemptResult:
        return AttemptResult(
            number=attempt_number,
            plan=plan,
            success=True,
            phase="completed",
            target_resolutions=[
                TargetResolution(
                    target=request.targets[0],
                    versions_before=["4.17.20"],
                    versions_after=[request.targets[0].minimum_version],
                    satisfied=True,
                    outcome="safe",
                )
            ],
            changed_files=["package.json", "package-lock.json"],
            diff="verified diff",
        )


def _project() -> ProjectState:
    manifest = '{"dependencies":{"lodash":"4.17.20"}}'
    return ProjectState(
        ecosystem="npm",
        package_manager="npm",
        lockfile_path="package-lock.json",
        target_locked_versions={"lodash": ["4.17.20"]},
        manifest_documents={"package.json": manifest},
    )


def _request() -> RemediationRequest:
    return RemediationRequest(
        ecosystem="npm",
        targets=[DependencyTarget(package="lodash", minimum_version="4.18.1")],
    )


def test_tool_schema_accepts_full_manifest_replacements() -> None:
    schema = try_dependency_fix.params_json_schema

    assert schema["additionalProperties"] is False
    assert schema["properties"]["files"]["items"]["$ref"] == "#/$defs/FileReplacement"


@pytest.mark.asyncio
async def test_context_accepts_only_one_verified_fix() -> None:
    request = _request()
    context = AutofixAgentContext(
        executor=SuccessfulExecutor(),  # type: ignore[arg-type]
        request=request,
        project=_project(),
        events=EventLogger(stream=StringIO()),
        max_attempts=3,
    )
    plan = FileFixPlan(
        rationale="update the direct dependency",
        files=[
            FileReplacement(
                path="package.json",
                content='{"dependencies":{"lodash":"4.18.1"}}',
            )
        ],
    )

    first = await context.try_plan(plan)
    second = await context.try_plan(plan)

    assert first["success"] is True
    assert "without claiming untested runtime or API compatibility" in first["message"]
    assert second["accepted"] is False
    assert second["success"] is True
    assert len(context.attempts) == 1


class AlreadySafeExecutor:
    def inspect(self, request: RemediationRequest) -> ProjectState:
        return ProjectState(
            ecosystem=request.ecosystem,
            package_manager="npm",
            lockfile_path="package-lock.json",
            target_locked_versions={"lodash": ["4.18.1"]},
            manifest_documents={"package.json": "{}"},
        )

    def request_is_satisfied(
        self,
        request: RemediationRequest,
        versions: dict[str, list[str]],
    ) -> bool:
        return True


@pytest.mark.asyncio
async def test_runner_returns_already_safe_without_agent_turn() -> None:
    runner = AgentAutofixRunner(
        AlreadySafeExecutor(),  # type: ignore[arg-type]
        EventLogger(stream=StringIO()),
        model_name="openrouter/openrouter/free",
    )

    result = await runner.run(_request())

    assert result.status == "already_safe"
    assert result.attempts == []


def test_build_model_routes_through_litellm() -> None:
    model = build_model("litellm/openrouter/openrouter/free")

    assert model.model == "openrouter/openrouter/free"
