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
                    DependencyEdit(package=parent, requirement=issue.safe_version),
                    DependencyEdit(
                        package=issue.package,
                        requirement=f">= {issue.safe_version}",
                        operation="add",
                    ),
                ],
            )

        raise PlanningExhaustedError("no untried direct ancestor strategy remains")


class AIPlanner:
    def __init__(self, model: str, client: AsyncOpenAI | None = None) -> None:
        if not model.strip():
            raise ValueError("AI planner requires a model")
        self.model = model
        self.client = client or AsyncOpenAI()

    async def propose(self, context: PlanningContext) -> FixPlan:
        instructions = """
You are a dependency remediation planner. Propose one minimal Gemfile edit plan that
allows Bundler to select the requested safe package version. You may update top-level
parent dependencies and add a direct constraint for the vulnerable transitive gem.
Do not edit source code, remove dependencies, change gem sources, or claim success.
The deterministic executor will run Bundler and reject invalid plans. Use prior resolver
errors to choose a materially different plan. Prefer patch/minor parent upgrades before
major upgrades, unless the safe version requires a major line.
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
