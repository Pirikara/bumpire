"""Agent loop for declarative package-manager profiles."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from agents import (
    Agent,
    MaxTurnsExceeded,
    RunContextWrapper,
    Runner,
    function_tool,
    set_tracing_disabled,
)

from depfix.agent_runtime import build_model
from depfix.events import EventLogger
from depfix.models import (
    AttemptResult,
    DependencyIssue,
    FileFixPlan,
    FileReplacement,
    FixResult,
    ProjectState,
)
from depfix.profile_adapter import ProfileAdapter

_OBSERVATION_LIMIT = 6_000


@dataclass(slots=True)
class ProfileAgentContext:
    adapter: ProfileAdapter
    issue: DependencyIssue
    project: ProjectState
    events: EventLogger
    max_attempts: int
    attempts: list[AttemptResult] = field(default_factory=list)
    seen_plans: set[tuple[tuple[str, str], ...]] = field(default_factory=set)

    @property
    def successful_attempt(self) -> AttemptResult | None:
        return next((attempt for attempt in self.attempts if attempt.success), None)

    async def try_plan(self, plan: FileFixPlan) -> dict[str, object]:
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
                "message": (
                    "this exact file set has already been attempted; propose a different edit"
                ),
            }
        self.seen_plans.add(fingerprint)
        number = len(self.attempts) + 1
        self.events.emit("plan", plan.rationale, attempt=number, planner="profile-agent")
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
                "message": (
                    "dependency resolution verification passed; stop and report completion "
                    "without claiming untested runtime or API compatibility"
                ),
            }
        return {
            "accepted": True,
            "success": False,
            "phase": attempt.phase,
            "error": attempt.error,
            "resolver_stdout": attempt.stdout[-_OBSERVATION_LIMIT:],
            "resolver_stderr": attempt.stderr[-_OBSERVATION_LIMIT:],
            "message": "use the resolver evidence and propose a materially different manifest edit",
        }


@function_tool
async def try_profile_fix(
    wrapper: RunContextWrapper[ProfileAgentContext],
    rationale: str,
    files: list[FileReplacement],
) -> str:
    """Try complete replacement contents for dependency manifests in a fresh sandbox.

    Args:
        rationale: Why these manifest replacements should produce the safe dependency graph.
        files: Full new contents of each dependency manifest that needs to change.
    """
    try:
        plan = FileFixPlan(rationale=rationale, files=files)
    except ValueError as exc:
        return json.dumps(
            {"accepted": False, "success": False, "message": str(exc)},
            ensure_ascii=False,
        )
    observation = await wrapper.context.try_plan(plan)
    return json.dumps(observation, ensure_ascii=False)


class ProfileAgentAutofixRunner:
    def __init__(
        self,
        adapter: ProfileAdapter,
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
        context = ProfileAgentContext(
            adapter=self.adapter,
            issue=issue,
            project=project,
            events=self.events,
            max_attempts=self.max_attempts,
        )
        agent = Agent[ProfileAgentContext](
            name="Profile-driven dependency AutoFix planner",
            model=self.model,
            instructions=self._instructions(project),
            tools=[try_profile_fix],
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
    def _instructions(project: ProjectState) -> str:
        profile_instructions = project.profile_instructions or ""
        return f"""
You are a dependency remediation planner operating through one constrained tool.
You MUST call try_profile_fix before claiming success. The input contains every manifest
you may replace. Pass the complete new content of only the manifest files that need a
change; preserve unrelated content and formatting exactly when practical. Never return
a partial snippet. Never edit a lockfile directly: the trusted resolver regenerates it.

Start with the smallest reasonable dependency-manifest change. If resolution fails,
inspect the returned stdout and stderr and try a materially different edit. Do not add,
remove, or modify source code, scripts, registry sources, runtime configuration, or
unrelated dependencies.

Package-manager profile guidance:
{profile_instructions}

Only success=true from try_profile_fix proves completion. Resolver success proves only
that the dependency graph resolves, the target reaches the safe version, and changed
files pass the allowlist. Never claim API, behavioral, or runtime compatibility because
application tests run later in the destination repository's CI.
""".strip()

    @staticmethod
    def _input(issue: DependencyIssue, project: ProjectState) -> str:
        return json.dumps(
            {
                "task": "produce and verify a minimal dependency security fix",
                "issue": issue.model_dump(mode="json"),
                "project": {
                    "ecosystem": project.ecosystem,
                    "package_manager_profile": project.package_manager,
                    "lockfile_path": project.lockfile_path,
                    "target_locked_version": project.target_locked_version,
                    "allowed_manifest_documents": project.manifest_documents,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
