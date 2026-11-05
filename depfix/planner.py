"""Deterministic and AI planners for the candidate-fix loop."""

from __future__ import annotations

import json
from typing import Protocol

from openai import AsyncOpenAI

from depfix.models import DependencyEdit, FixPlan, PlanningContext


class PlanningExhaustedError(RuntimeError):
    pass


class Planner(Protocol):
    async def propose(self, context: PlanningContext) -> FixPlan: ...


class HeuristicPlanner:
    """Small baseline planner that makes the agent loop testable without an API."""

    async def propose(self, context: PlanningContext) -> FixPlan:
        if context.issue.ecosystem == "npm":
            return self._propose_npm(context)
        if context.issue.ecosystem == "bundler":
            return self._propose_bundler(context)
        raise PlanningExhaustedError(
            f"no heuristic planner for ecosystem: {context.issue.ecosystem}"
        )

    @staticmethod
    def _propose_bundler(context: PlanningContext) -> FixPlan:
        issue = context.issue
        tried = {attempt.plan.fingerprint() for attempt in context.attempts}

        direct_target = issue.package in context.project.direct_dependencies
        direct_plan = FixPlan(
            rationale=(
                f"Update the directly declared {issue.package} requirement to the safe version."
                if direct_target
                else f"Pin transitive dependency {issue.package} to its safe version first."
            ),
            edits=[
                DependencyEdit(
                    package=issue.package,
                    requirement=f">= {issue.safe_version}",
                    operation="set" if direct_target else "add",
                    manifest_path=context.project.manifest_path,
                    section="dependencies",
                )
            ],
        )
        if direct_plan.fingerprint() not in tried:
            return direct_plan

        attempted_packages = {
            edit.package for attempt in context.attempts for edit in attempt.plan.edits
        }
        for path in context.project.paths_to_target:
            parent = path[0]
            if parent == issue.package or parent in attempted_packages:
                continue
            return FixPlan(
                rationale=(
                    f"{parent} is a direct ancestor of {issue.package}. Align the parent and "
                    "pin the vulnerable child so the package manager can resolve the safe graph."
                ),
                edits=[
                    DependencyEdit(
                        package=parent,
                        requirement=issue.safe_version,
                        manifest_path=context.project.manifest_path,
                        section="dependencies",
                    ),
                    DependencyEdit(
                        package=issue.package,
                        requirement=f">= {issue.safe_version}",
                        operation="add",
                        manifest_path=context.project.manifest_path,
                        section="dependencies",
                    ),
                ],
            )

        raise PlanningExhaustedError("no untried direct ancestor strategy remains")

    @staticmethod
    def _propose_npm(context: PlanningContext) -> FixPlan:
        issue = context.issue
        tried = {attempt.plan.fingerprint() for attempt in context.attempts}
        root_constraint = next(
            (constraint for constraint in context.project.constraints if constraint.is_root),
            None,
        )
        root_edit = DependencyEdit(
            package=issue.package,
            requirement=issue.safe_version,
            operation="set" if root_constraint else "add",
            manifest_path=(
                root_constraint.manifest_path
                if root_constraint is not None
                else context.project.manifest_path
            ),
            section=root_constraint.section if root_constraint is not None else "dependencies",
        )
        direct_plan = FixPlan(
            rationale=f"Update {issue.package} to the safe version in the root package manifest.",
            edits=[root_edit],
        )
        if direct_plan.fingerprint() not in tried:
            return direct_plan

        peer_constraints = [
            constraint
            for constraint in context.project.constraints
            if not constraint.is_root and constraint.section == "peerDependencies"
        ]
        if peer_constraints:
            peer_plan = FixPlan(
                rationale=(
                    f"Align local peer constraints with {issue.package} {issue.safe_version} "
                    "after npm rejected the root-only update."
                ),
                edits=[
                    root_edit,
                    *[
                        DependencyEdit(
                            package=issue.package,
                            requirement=issue.safe_version,
                            manifest_path=constraint.manifest_path,
                            section=constraint.section,
                        )
                        for constraint in peer_constraints
                    ],
                ],
            )
            if peer_plan.fingerprint() not in tried:
                return peer_plan

        raise PlanningExhaustedError("no untried npm peer-constraint strategy remains")


class AIPlanner:
    def __init__(self, model: str, client: AsyncOpenAI | None = None) -> None:
        if not model.strip():
            raise ValueError("AI planner requires a model")
        self.model = model
        self.client = client or AsyncOpenAI()

    async def propose(self, context: PlanningContext) -> FixPlan:
        instructions = """
You are a dependency remediation planner. Propose one minimal dependency-manifest edit
plan that allows the detected package manager to select the requested safe version.
For Bundler, you may align top-level parent gems and add a direct constraint. For npm,
you may update dependency fields in the root or local workspace package.json manifests,
including peerDependencies when resolver evidence requires it. Use only manifest paths
present in the planning context. Do not edit source code, remove dependencies, change
registry sources, or claim success. The deterministic executor runs the package manager
and rejects invalid plans. Use prior resolver errors to choose a materially different plan.
""".strip()
        response = await self.client.responses.parse(
            model=self.model,
            instructions=instructions,
            input=json.dumps(context.model_dump(mode="json"), ensure_ascii=False),
            text_format=FixPlan,
        )
        if response.output_parsed is None:
            raise RuntimeError("AI planner returned no structured plan")
        return response.output_parsed
