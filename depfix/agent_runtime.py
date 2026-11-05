"""OpenAI Agents SDK runtime backed by LiteLLM-compatible model providers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import litellm
from agents import (
    Agent,
    MaxTurnsExceeded,
    RunContextWrapper,
    Runner,
    function_tool,
    set_tracing_disabled,
)
from agents.extensions.models.litellm_model import LitellmModel

from depfix.adapters import DependencyAdapter
from depfix.events import EventLogger
from depfix.models import (
    AttemptResult,
    DependencyEdit,
    DependencyIssue,
    FixPlan,
    FixResult,
    ProjectState,
)

_OBSERVATION_LIMIT = 6_000


def configure_litellm() -> None:
    """Prefer cross-provider compatibility without logging prompt bodies."""
    litellm.drop_params = True
    litellm.modify_params = True
    litellm.turn_off_message_logging = True
    litellm.suppress_debug_info = True


def build_model(
    model_name: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LitellmModel:
    name = model_name.strip()
    if name.startswith("litellm/"):
        name = name.removeprefix("litellm/")
    if not name:
        raise ValueError("agent planner requires --model")
    configure_litellm()
    return LitellmModel(model=name, api_key=api_key, base_url=base_url)


@dataclass(slots=True)
class AutofixAgentContext:
    adapter: DependencyAdapter
    issue: DependencyIssue
    project: ProjectState
    events: EventLogger
    max_attempts: int
    attempts: list[AttemptResult] = field(default_factory=list)
    seen_plans: set[tuple[tuple[str, str, str, str, str], ...]] = field(default_factory=set)

    @property
    def successful_attempt(self) -> AttemptResult | None:
        return next((attempt for attempt in self.attempts if attempt.success), None)

    async def try_plan(self, plan: FixPlan) -> dict[str, object]:
        successful = self.successful_attempt
        if successful is not None:
            return {
                "accepted": False,
                "success": True,
                "message": "a fix has already passed deterministic verification",
                "target_after": successful.target_after,
            }
        if len(self.attempts) >= self.max_attempts:
            return {
                "accepted": False,
                "success": False,
                "message": "attempt budget exhausted",
            }

        fingerprint = plan.fingerprint()
        if fingerprint in self.seen_plans:
            return {
                "accepted": False,
                "success": False,
                "message": "this exact plan has already been attempted; propose a different plan",
            }
        self.seen_plans.add(fingerprint)
        number = len(self.attempts) + 1
        self.events.emit("plan", plan.rationale, attempt=number, planner="agent")
        attempt = await asyncio.to_thread(
            self.adapter.execute,
            self.issue,
            plan,
            attempt_number=number,
        )
        self.attempts.append(attempt)
        if attempt.success:
            return {
                "accepted": True,
                "success": True,
                "phase": attempt.phase,
                "target_before": attempt.target_before,
                "target_after": attempt.target_after,
                "changed_files": attempt.changed_files,
                "message": "deterministic verification passed; stop and report completion",
            }
        return {
            "accepted": True,
            "success": False,
            "phase": attempt.phase,
            "error": attempt.error,
            "resolver_stdout": attempt.stdout[-_OBSERVATION_LIMIT:],
            "resolver_stderr": attempt.stderr[-_OBSERVATION_LIMIT:],
            "message": "inspect the resolver evidence and propose a materially different plan",
        }


@function_tool
async def try_dependency_fix(
    wrapper: RunContextWrapper[AutofixAgentContext],
    rationale: str,
    edits: list[DependencyEdit],
) -> str:
    """Try one dependency fix in a fresh sandbox and return resolver/verification evidence.

    Args:
        rationale: Why this edit set should resolve the vulnerable dependency safely.
        edits: Constrained dependency-manifest edits to test together in one attempt.
    """
    try:
        plan = FixPlan(rationale=rationale, edits=edits)
    except ValueError as exc:
        return json.dumps(
            {"accepted": False, "success": False, "message": str(exc)},
            ensure_ascii=False,
        )
    observation = await wrapper.context.try_plan(plan)
    return json.dumps(observation, ensure_ascii=False)


class AgentAutofixRunner:
    def __init__(
        self,
        adapter: DependencyAdapter,
        events: EventLogger,
        *,
        model_name: str,
        max_attempts: int = 5,
        max_turns: int = 12,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if max_turns < 2:
            raise ValueError("max_turns must be at least 2")
        self.adapter = adapter
        self.events = events
        self.model = build_model(model_name, api_key=api_key, base_url=base_url)
        self.max_attempts = max_attempts
        self.max_turns = max_turns

    async def run(self, issue: DependencyIssue) -> FixResult:
        project = self.adapter.inspect(issue)
        context = AutofixAgentContext(
            adapter=self.adapter,
            issue=issue,
            project=project,
            events=self.events,
            max_attempts=self.max_attempts,
        )
        agent = Agent[AutofixAgentContext](
            name="Dependency AutoFix planner",
            model=self.model,
            instructions=self._instructions(),
            tools=[try_dependency_fix],
        )
        set_tracing_disabled(True)
        runner_error: str | None = None
        try:
            await Runner.run(
                agent,
                self._input(issue, project),
                context=context,
                max_turns=self.max_turns,
            )
        except MaxTurnsExceeded:
            runner_error = f"agent turn budget exhausted ({self.max_turns})"
        except Exception as exc:  # provider SDKs expose different error hierarchies
            runner_error = f"agent runtime failed: {exc}"

        successful = context.successful_attempt
        if successful is not None:
            self.events.emit(
                "finish",
                f"Verified {issue.package} {successful.target_before} → {successful.target_after}",
            )
            return FixResult(
                status="fixed",
                issue=issue,
                attempts=context.attempts,
                diff=successful.diff,
                changed_files=successful.changed_files,
            )
        if runner_error is not None:
            status = "exhausted" if context.attempts else "failed"
            return FixResult(
                status=status,
                issue=issue,
                attempts=context.attempts,
                error=runner_error,
            )
        return FixResult(
            status="exhausted",
            issue=issue,
            attempts=context.attempts,
            error="agent stopped without producing a deterministically verified fix",
        )

    @staticmethod
    def _instructions() -> str:
        return """
You are a dependency remediation planner operating through a constrained tool.
You MUST call try_dependency_fix before claiming success. Start with the smallest
reasonable dependency-manifest edit. If the resolver rejects it, use the returned
stdout/stderr and project constraints to propose a materially different plan.

You may only edit dependency requirements in manifest paths and sections shown in
the project context. Never edit source code, lockfiles directly, registry sources,
scripts, or runtime configuration. Do not remove dependencies. For local npm peer
conflicts, you may align the local package's peerDependencies. For Bundler umbrella
gems, you may align a direct parent and add a safe child constraint.

Only the tool's success=true result proves completion. When it succeeds, stop making
tool calls and return a short completion message. If the attempt budget is exhausted,
state that no verified fix was found.
""".strip()

    @staticmethod
    def _input(issue: DependencyIssue, project: ProjectState) -> str:
        compact_project = {
            "ecosystem": project.ecosystem,
            "package_manager": project.package_manager,
            "manifest_path": project.manifest_path,
            "lockfile_path": project.lockfile_path,
            "manifest_text": project.manifest_text,
            "direct_dependencies": project.direct_dependencies,
            "paths_to_target": project.paths_to_target,
            "target_locked_version": project.target_locked_version,
            "constraints": [item.model_dump(mode="json") for item in project.constraints],
        }
        return json.dumps(
            {
                "task": "produce and verify a minimal dependency security fix",
                "issue": issue.model_dump(mode="json"),
                "project": compact_project,
            },
            ensure_ascii=False,
            indent=2,
        )
