from io import StringIO

import pytest

from depfix.agent_runtime import AutofixAgentContext, build_model, try_dependency_fix
from depfix.events import EventLogger
from depfix.models import (
    AttemptResult,
    DependencyEdit,
    DependencyIssue,
    FixPlan,
    ProjectState,
)


class SuccessfulAdapter:
    ecosystem = "npm"

    def inspect(self, issue: DependencyIssue) -> ProjectState:
        raise AssertionError("not used by this unit test")

    def execute(
        self,
        issue: DependencyIssue,
        plan: FixPlan,
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
    return ProjectState(
        ecosystem="npm",
        package_manager="npm",
        manifest_path="package.json",
        lockfile_path="package-lock.json",
        manifest_text='{"dependencies":{"lodash":"4.17.20"}}',
        direct_dependencies={"lodash": "4.17.20"},
        locked_versions={"lodash": "4.17.20"},
        paths_to_target=[["lodash"]],
        target_locked_version="4.17.20",
    )


def test_build_model_routes_through_litellm() -> None:
    model = build_model("litellm/openrouter/openrouter/free")

    assert model.model == "openrouter/openrouter/free"


def test_resolver_tool_schema_is_strict_and_structured() -> None:
    schema = try_dependency_fix.params_json_schema

    assert schema["additionalProperties"] is False
    assert schema["properties"]["edits"]["items"]["$ref"] == "#/$defs/DependencyEdit"


@pytest.mark.asyncio
async def test_context_accepts_only_one_verified_fix() -> None:
    issue = DependencyIssue(ecosystem="npm", package="lodash", safe_version="4.18.1")
    context = AutofixAgentContext(
        adapter=SuccessfulAdapter(),
        issue=issue,
        project=_project(),
        events=EventLogger(stream=StringIO()),
        max_attempts=3,
    )
    plan = FixPlan(
        rationale="update the direct dependency",
        edits=[
            DependencyEdit(
                package="lodash",
                requirement="4.18.1",
                manifest_path="package.json",
                section="dependencies",
            )
        ],
    )

    first = await context.try_plan(plan)
    second = await context.try_plan(plan)

    assert first["success"] is True
    assert "without claiming untested runtime or API compatibility" in first["message"]
    assert second == {
        "accepted": False,
        "success": True,
        "message": "a fix has already passed deterministic verification",
        "target_after": "4.18.1",
    }
    assert len(context.attempts) == 1
