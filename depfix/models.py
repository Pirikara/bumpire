"""Structured state exchanged by planners, executors, and verification gates."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class DependencyIssue(BaseModel):
    ecosystem: Literal["bundler"] = "bundler"
    package: str
    safe_version: str
    advisory_id: str | None = None

    @field_validator("package", "safe_version")
    @classmethod
    def non_empty_single_line(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("must be a non-empty single-line value")
        return value


class DependencyEdit(BaseModel):
    package: str
    requirement: str
    operation: Literal["set", "add"] = "set"

    @field_validator("package", "requirement")
    @classmethod
    def safe_gemfile_value(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char in value for char in ("\n", "\r", '"', "'")):
            raise ValueError("must be a non-empty quote-free single-line value")
        return value


class FixPlan(BaseModel):
    rationale: str
    edits: list[DependencyEdit] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_packages(self) -> FixPlan:
        packages = [edit.package for edit in self.edits]
        if len(packages) != len(set(packages)):
            raise ValueError("a plan may edit each package at most once")
        return self

    def fingerprint(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            sorted((edit.package, edit.requirement, edit.operation) for edit in self.edits)
        )


class ProjectState(BaseModel):
    ecosystem: Literal["bundler"] = "bundler"
    manifest_path: str
    lockfile_path: str
    manifest_text: str
    direct_dependencies: dict[str, str]
    locked_versions: dict[str, str]
    paths_to_target: list[list[str]]
    target_locked_version: str | None


class AttemptResult(BaseModel):
    number: int
    plan: FixPlan
    success: bool
    phase: Literal["manifest", "resolution", "verification", "review", "completed"]
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    target_before: str | None = None
    target_after: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    diff: str = ""


class FixResult(BaseModel):
    status: Literal["fixed", "exhausted", "failed"]
    issue: DependencyIssue
    attempts: list[AttemptResult]
    diff: str = ""
    changed_files: list[str] = Field(default_factory=list)
    error: str | None = None


class PlanningContext(BaseModel):
    issue: DependencyIssue
    project: ProjectState
    attempts: list[AttemptResult]
