import pytest

from depfix.models import (
    AttemptResult,
    DependencyConstraint,
    DependencyIssue,
    FixPlan,
    PlanningContext,
    ProjectState,
)
from depfix.planner import HeuristicPlanner


def _context(attempts: list[AttemptResult]) -> PlanningContext:
    return PlanningContext(
        issue=DependencyIssue(
            ecosystem="npm",
            package="lodash",
            safe_version="4.17.21",
        ),
        project=ProjectState(
            ecosystem="npm",
            package_manager="npm",
            manifest_path="package.json",
            lockfile_path="package-lock.json",
            manifest_text='{"dependencies":{"lodash":"4.17.20"}}',
            direct_dependencies={"lodash": "4.17.20"},
            locked_versions={"lodash": "4.17.20"},
            paths_to_target=[
                ["lodash"],
                ["packages/peer/package.json", "lodash"],
            ],
            target_locked_version="4.17.20",
            constraints=[
                DependencyConstraint(
                    package="lodash",
                    requirement="4.17.20",
                    manifest_path="package.json",
                    section="dependencies",
                    is_root=True,
                ),
                DependencyConstraint(
                    package="lodash",
                    requirement="4.17.20",
                    manifest_path="packages/peer/package.json",
                    section="peerDependencies",
                ),
            ],
        ),
        attempts=attempts,
    )


@pytest.mark.asyncio
async def test_planner_escalates_from_root_update_to_local_peer_alignment() -> None:
    planner = HeuristicPlanner()
    first = await planner.propose(_context([]))
    assert [(edit.manifest_path, edit.section) for edit in first.edits] == [
        ("package.json", "dependencies")
    ]

    failed = AttemptResult(
        number=1,
        plan=FixPlan(rationale=first.rationale, edits=first.edits),
        success=False,
        phase="resolution",
        error="ERESOLVE conflicting peer dependency",
    )
    second = await planner.propose(_context([failed]))

    assert [(edit.manifest_path, edit.section) for edit in second.edits] == [
        ("package.json", "dependencies"),
        ("packages/peer/package.json", "peerDependencies"),
    ]
