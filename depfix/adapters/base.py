"""Contract implemented by each package-manager adapter."""

from __future__ import annotations

from typing import Protocol

from depfix.models import AttemptResult, DependencyIssue, FixPlan, ProjectState


class DependencyAdapter(Protocol):
    ecosystem: str

    def inspect(self, issue: DependencyIssue) -> ProjectState: ...

    def execute(
        self,
        issue: DependencyIssue,
        plan: FixPlan,
        *,
        attempt_number: int,
    ) -> AttemptResult: ...
