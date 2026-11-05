import pytest

from depfix.models import (
    AttemptResult,
    DependencyIssue,
    FixPlan,
    PlanningContext,
    ProjectState,
)
from depfix.planner import HeuristicPlanner


def _context(attempts: list[AttemptResult]) -> PlanningContext:
    return PlanningContext(
        issue=DependencyIssue(
            ecosystem="bundler",
            package="activestorage",
            safe_version="7.2.3.2",
        ),
        project=ProjectState(
            ecosystem="bundler",
            package_manager="bundler",
            manifest_path="rails/Gemfile",
            lockfile_path="rails/Gemfile.lock",
            manifest_text='gem "rails", "7.2.3.1"',
            direct_dependencies={"rails": "= 7.2.3.1"},
            locked_versions={"rails": "7.2.3.1", "activestorage": "7.2.3.1"},
            paths_to_target=[["rails", "activestorage"]],
            target_locked_version="7.2.3.1",
        ),
        attempts=attempts,
    )


@pytest.mark.asyncio
async def test_planner_escalates_from_child_pin_to_direct_parent() -> None:
    planner = HeuristicPlanner()
    first = await planner.propose(_context([]))
    assert [edit.package for edit in first.edits] == ["activestorage"]

    failed = AttemptResult(
        number=1,
        plan=FixPlan(rationale=first.rationale, edits=first.edits),
        success=False,
        phase="resolution",
        error="rails requires activestorage 7.2.3.1",
    )
    second = await planner.propose(_context([failed]))

    assert [(edit.package, edit.requirement) for edit in second.edits] == [
        ("rails", "7.2.3.2"),
        ("activestorage", ">= 7.2.3.2"),
    ]
