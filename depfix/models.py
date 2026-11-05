"""Structured state exchanged by planners, executors, and verification gates."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class DependencyIssue(BaseModel):
    ecosystem: str
    package: str
    safe_version: str
    advisory_id: str | None = None

    @field_validator("ecosystem", "package", "safe_version")
    @classmethod
    def non_empty_single_line(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("must be a non-empty single-line value")
        return value


class FileReplacement(BaseModel):
    path: str
    content: str = Field(max_length=500_000)

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        value = value.strip()
        parts = value.replace("\\", "/").split("/")
        if not value or value.startswith("/") or ".." in parts or "\x00" in value:
            raise ValueError("must be a safe relative file path")
        return value

    @field_validator("content")
    @classmethod
    def text_without_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("file content may not contain NUL bytes")
        return value


class FileFixPlan(BaseModel):
    rationale: str
    files: list[FileReplacement] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_paths(self) -> FileFixPlan:
        paths = [replacement.path for replacement in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("a plan may replace each manifest at most once")
        return self

    def fingerprint(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                (
                    replacement.path,
                    hashlib.sha256(replacement.content.encode()).hexdigest(),
                )
                for replacement in self.files
            )
        )


class ProjectState(BaseModel):
    ecosystem: str
    package_manager: str
    lockfile_path: str
    target_locked_version: str | None
    manifest_documents: dict[str, str] = Field(default_factory=dict)
    profile_instructions: str | None = None


class AttemptResult(BaseModel):
    number: int
    plan: FileFixPlan
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
