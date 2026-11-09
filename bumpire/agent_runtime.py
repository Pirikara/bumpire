"""Agent loop for declarative package-manager profiles."""

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

from bumpire.events import EventLogger
from bumpire.models import (
    AttemptResult,
    FileFixPlan,
    FileReplacement,
    ProjectState,
    UpdateRequest,
    UpdateResult,
)
from bumpire.profile_executor import ProfileExecutor

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
        raise ValueError("agent requires --model")
    configure_litellm()
    return LitellmModel(model=name, api_key=api_key, base_url=base_url)


@dataclass(slots=True)
class UpdateAgentContext:
    executor: ProfileExecutor
    request: UpdateRequest
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
                "target_verifications": [
                    verification.model_dump(mode="json")
                    for verification in successful.target_verifications
                ],
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
            self.executor.execute,
            self.request,
            plan,
            attempt_number=number,
        )
        self.attempts.append(attempt)
        if attempt.success:
            return {
                "accepted": True,
                "success": True,
                "phase": attempt.phase,
                "target_verifications": [
                    verification.model_dump(mode="json")
                    for verification in attempt.target_verifications
                ],
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
async def try_dependency_update(
    wrapper: RunContextWrapper[UpdateAgentContext],
    rationale: str,
    files: list[FileReplacement],
) -> str:
    """Try complete replacement contents for dependency manifests in a fresh sandbox.

    Args:
        rationale: Why these manifest replacements should satisfy the requested policies.
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


class AgentUpdateRunner:
    def __init__(
        self,
        executor: ProfileExecutor,
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
        self.executor = executor
        self.events = events
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.max_attempts = max_attempts
        self.max_turns = max_turns

    async def run(self, request: UpdateRequest) -> UpdateResult:
        project = self.executor.inspect(request)
        if self.executor.request_is_satisfied(request, project.target_locked_versions):
            packages = ", ".join(target.package for target in request.targets)
            self.events.emit(
                "finish", f"Requested targets already satisfy their policies: {packages}"
            )
            return UpdateResult(status="already_satisfied", request=request, attempts=[])

        context = UpdateAgentContext(
            executor=self.executor,
            request=request,
            project=project,
            events=self.events,
            max_attempts=self.max_attempts,
        )
        agent = Agent[UpdateAgentContext](
            name="Profile-driven dependency update planner",
            model=build_model(
                self.model_name,
                api_key=self.api_key,
                base_url=self.base_url,
            ),
            instructions=self._instructions(project),
            tools=[try_dependency_update],
        )
        set_tracing_disabled(True)
        runner_error: str | None = None
        try:
            await Runner.run(
                agent,
                self._input(request, project),
                context=context,
                max_turns=self.max_turns,
            )
        except MaxTurnsExceeded:
            runner_error = f"agent turn budget exhausted ({self.max_turns})"
        except Exception as exc:  # provider SDKs expose different error hierarchies
            runner_error = f"agent runtime failed: {exc}"

        successful = context.successful_attempt
        if successful is not None:
            summary = ", ".join(
                f"{verification.target.package} "
                f"{self._display_versions(verification.versions_before)} → "
                f"{self._display_versions(verification.versions_after)}"
                for verification in successful.target_verifications
            )
            self.events.emit(
                "finish",
                f"Verified dependency targets: {summary}",
            )
            return UpdateResult(
                status="updated",
                request=request,
                attempts=context.attempts,
                diff=successful.diff,
                changed_files=successful.changed_files,
            )
        if runner_error is not None:
            status = "exhausted" if context.attempts else "failed"
            return UpdateResult(
                status=status,
                request=request,
                attempts=context.attempts,
                error=runner_error,
            )
        return UpdateResult(
            status="exhausted",
            request=request,
            attempts=context.attempts,
            error="agent stopped without producing a deterministically verified fix",
        )

    @staticmethod
    def _instructions(project: ProjectState) -> str:
        profile_instructions = project.profile_instructions or ""
        return f"""
You are a dependency update planner operating through one constrained tool.
You MUST call try_dependency_update before claiming success. The input contains every manifest
you may replace. Pass the complete new content of only the manifest files that need a
change; preserve unrelated content and formatting exactly when practical. Never return
a partial snippet. Never edit a lockfile directly: the trusted resolver regenerates it.

Treat every requested target as one atomic update batch. Start with the smallest coherent
dependency-manifest change that can satisfy every target's explicit version policy. If
resolution or deterministic verification fails, inspect the returned evidence and try a
materially different edit. Do not add, remove, or modify source code, scripts, registry
sources, runtime configuration, or unrelated dependencies. Do not remove a requested
target unless its policy explicitly permits removal.

Package-manager profile guidance:
{profile_instructions}

Only success=true from try_dependency_update proves completion. Resolver success proves
only that the dependency graph resolves, every requested target satisfies its declared
policy, and changed files pass the allowlist. Never claim API, behavioral, or runtime
compatibility because application tests run later in the destination repository's CI.
""".strip()

    @staticmethod
    def _input(request: UpdateRequest, project: ProjectState) -> str:
        return json.dumps(
            {
                "task": "produce and verify one atomic dependency update",
                "request": request.model_dump(mode="json"),
                "project": {
                    "ecosystem": project.ecosystem,
                    "package_manager_profile": project.package_manager,
                    "lockfile_path": project.lockfile_path,
                    "target_locked_versions": project.target_locked_versions,
                    "allowed_manifest_documents": project.manifest_documents,
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _display_versions(versions: list[str]) -> str:
        return ",".join(versions) if versions else "removed"
