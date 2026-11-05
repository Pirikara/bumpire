from io import StringIO

import pytest

from bumpire.agent_runtime import AutofixAgentContext, build_model, try_dependency_fix
from bumpire.events import EventLogger
from bumpire.models import (
    AttemptResult,
    DependencyIssue,
    FileFixPlan,
    FileReplacement,
    ProjectState,
)


class SuccessfulExecutor:
    def execute(
        self,
        issue: DependencyIssue,
        plan: FileFixPlan,
        *,
        attempt_number: int,
    ) -> AttemptResult:
        return AttemptResult(
            number=attempt_number,
            plan=plan,
            success=True,
            phase="completed",
            target_before="4.17.20",
            target_after=issue.safe_version,
            changed_files=["package.json", "package-lock.json"],
            diff="verified diff",
        )


def _project() -> ProjectState:
    manifest = '{"dependencies":{"lodash":"4.17.20"}}'
    return ProjectState(
        ecosystem="npm",
        package_manager="npm",
        lockfile_path="package-lock.json",
        target_locked_version="4.17.20",
        manifest_documents={"package.json": manifest},
    )


def test_tool_schema_accepts_full_manifest_replacements() -> None:
    schema = try_dependency_fix.params_json_schema

    assert schema["additionalProperties"] is False
    assert schema["properties"]["files"]["items"]["$ref"] == "#/$defs/FileReplacement"


@pytest.mark.asyncio
async def test_context_accepts_only_one_verified_fix() -> None:
    issue = DependencyIssue(ecosystem="npm", package="lodash", safe_version="4.18.1")
    context = AutofixAgentContext(
        executor=SuccessfulExecutor(),  # type: ignore[arg-type]
        issue=issue,
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


def test_build_model_routes_through_litellm() -> None:
    model = build_model("litellm/openrouter/openrouter/free")

    assert model.model == "openrouter/openrouter/free"
