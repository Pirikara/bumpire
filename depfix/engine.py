"""Explicit plan → execute → observe → re-plan loop."""

from __future__ import annotations

from depfix.bundler import BundlerAdapter
from depfix.events import EventLogger
from depfix.models import DependencyIssue, FixResult, PlanningContext
from depfix.planner import Planner, PlanningExhaustedError


class AutofixEngine:
    def __init__(
        self,
        adapter: BundlerAdapter,
        planner: Planner,
        events: EventLogger,
        *,
        max_attempts: int = 5,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.adapter = adapter
        self.planner = planner
        self.events = events
        self.max_attempts = max_attempts

    async def run(self, issue: DependencyIssue) -> FixResult:
        project = self.adapter.inspect(issue)
        attempts = []
        seen_plans: set[tuple[tuple[str, str, str], ...]] = set()

        for number in range(1, self.max_attempts + 1):
            context = PlanningContext(issue=issue, project=project, attempts=attempts)
            try:
                plan = await self.planner.propose(context)
            except PlanningExhaustedError as exc:
                return FixResult(
                    status="exhausted",
                    issue=issue,
                    attempts=attempts,
                    error=str(exc),
                )

            fingerprint = plan.fingerprint()
            if fingerprint in seen_plans:
                return FixResult(
                    status="exhausted",
                    issue=issue,
                    attempts=attempts,
                    error="planner repeated an already attempted plan",
                )
            seen_plans.add(fingerprint)
            self.events.emit("plan", plan.rationale, attempt=number)

            attempt = self.adapter.execute(issue, plan, attempt_number=number)
            attempts.append(attempt)
            if attempt.success:
                self.events.emit(
                    "finish",
                    f"Verified {issue.package} {attempt.target_before} → {attempt.target_after}",
                )
                return FixResult(
                    status="fixed",
                    issue=issue,
                    attempts=attempts,
                    diff=attempt.diff,
                    changed_files=attempt.changed_files,
                )
            self.events.emit(
                "observe",
                attempt.error or "candidate failed verification",
                attempt=number,
                phase=attempt.phase,
            )

        return FixResult(
            status="exhausted",
            issue=issue,
            attempts=attempts,
            error=f"maximum attempts reached ({self.max_attempts})",
        )
