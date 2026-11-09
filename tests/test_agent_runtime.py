from io import StringIO

import pytest

from bumpire.agent_runtime import (
    AgentUpdateRunner,
    UpdateAgentContext,
    build_model,
    try_dependency_update,
)
from bumpire.events import EventLogger
from bumpire.models import (
    AttemptResult,
    DependencyTarget,
    FileFixPlan,
    FileReplacement,
    ProjectState,
    TargetVerification,
    UpdateRequest,
    VersionRequirement,
)


class SuccessfulExecutor:
    def execute(
        self,
        request: UpdateRequest,
        plan: FileFixPlan,
        *,
        attempt_number: int,
    ) -> AttemptResult:
        return AttemptResult(
            number=attempt_number,
            plan=plan,
            success=True,
            phase="completed",
            target_verifications=[
                TargetVerification(
                    target=request.targets[0],
                    versions_before=["4.17.20"],
                    versions_after=[request.targets[0].requirement.version],
                    satisfied=True,
                    outcome="satisfied",
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


def test_tool_schema_accepts_full_manifest_replacements() -> None:
    schema = try_dependency_update.params_json_schema

    assert schema["additionalProperties"] is False
    assert schema["properties"]["files"]["items"]["$ref"] == "#/$defs/FileReplacement"


@pytest.mark.asyncio
async def test_context_accepts_only_one_verified_fix() -> None:
    request = _request()
    context = UpdateAgentContext(
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


class AlreadySatisfiedExecutor:
    def inspect(self, request: UpdateRequest) -> ProjectState:
        return ProjectState(
            ecosystem=request.ecosystem,
            package_manager="npm",
            lockfile_path="package-lock.json",
            target_locked_versions={"lodash": ["4.18.1"]},
            manifest_documents={"package.json": "{}"},
        )

    def request_is_satisfied(
        self,
        request: UpdateRequest,
        versions: dict[str, list[str]],
    ) -> bool:
        return True


@pytest.mark.asyncio
async def test_runner_returns_already_satisfied_without_model_or_agent_turn() -> None:
    runner = AgentUpdateRunner(
        AlreadySatisfiedExecutor(),  # type: ignore[arg-type]
        EventLogger(stream=StringIO()),
        model_name="",
    )

    result = await runner.run(_request())

    assert result.status == "already_satisfied"
    assert result.attempts == []


def test_build_model_routes_through_litellm() -> None:
    model = build_model("litellm/openrouter/openrouter/free")

    assert model.model == "openrouter/openrouter/free"
