"""Structured state exchanged by planners, executors, and verification gates."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class VersionRequirement(BaseModel):
    kind: Literal["exact", "at_least"] = "exact"
    version: str

    @field_validator("version")
    @classmethod
    def non_empty_single_line(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("must be a non-empty single-line value")
        return value


class DependencyTarget(BaseModel):
    package: str
    requirement: VersionRequirement
    allow_removal: bool = False

    @field_validator("package")
    @classmethod
    def non_empty_package(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("must be a non-empty single-line value")
        return value


class UpdateRequest(BaseModel):
    ecosystem: str
    targets: list[DependencyTarget] = Field(min_length=1, max_length=20)

    @field_validator("ecosystem")
    @classmethod
    def non_empty_ecosystem(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("ecosystem must be a non-empty single-line value")
        return value

    @model_validator(mode="after")
    def unique_packages(self) -> UpdateRequest:
        packages = [target.package for target in self.targets]
        if len(packages) != len(set(packages)):
            raise ValueError("each package may appear only once per update request")
        return self


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
    base_commit: str
    base_ref: str | None = None
    workspace: str
    ecosystem: str
    package_manager: str
    lockfile_path: str
    target_locked_versions: dict[str, list[str]]
    manifest_documents: dict[str, str] = Field(default_factory=dict)
    profile_instructions: str | None = None

    @field_validator("base_commit")
    @classmethod
    def valid_base_commit(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("base_commit must be a full hexadecimal Git object ID")
        return normalized

    @field_validator("base_ref")
    @classmethod
    def valid_base_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or "\n" in normalized or "\r" in normalized or "\x00" in normalized:
            raise ValueError("base_ref must be a non-empty single-line value")
        return normalized

    @field_validator("workspace")
    @classmethod
    def valid_workspace(cls, value: str) -> str:
        normalized = value.strip() or "."
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or "\x00" in normalized:
            raise ValueError("workspace must be a safe relative path")
        return normalized


class TargetVerification(BaseModel):
    target: DependencyTarget
    versions_before: list[str]
    versions_after: list[str]
    satisfied: bool
    outcome: Literal[
        "satisfied",
        "removed",
        "removal_disallowed",
        "constraint_mismatch",
        "missing",
        "invalid_version",
    ]
    error: str | None = None

    @model_validator(mode="after")
    def internally_consistent(self) -> TargetVerification:
        successful_outcome = self.outcome in {"satisfied", "removed"}
        if self.satisfied != successful_outcome:
            raise ValueError("target verification outcome contradicts satisfied state")
        if self.outcome == "removed":
            if not self.target.allow_removal:
                raise ValueError("target removal was not permitted")
            if not self.versions_before or self.versions_after:
                raise ValueError("removed outcome requires versions only before the update")
        elif self.outcome == "removal_disallowed":
            if self.target.allow_removal:
                raise ValueError("removal_disallowed contradicts the target policy")
            if not self.versions_before or self.versions_after:
                raise ValueError("removal_disallowed requires versions only before the update")
        elif self.outcome in {"satisfied", "constraint_mismatch", "invalid_version"}:
            if not self.versions_before or not self.versions_after:
                raise ValueError(f"{self.outcome} requires versions before and after the update")
        if self.outcome == "invalid_version" and not self.error:
            raise ValueError("invalid_version requires comparison error evidence")
        if self.outcome != "invalid_version" and self.error is not None:
            raise ValueError("comparison error evidence is only valid for invalid_version")
        return self


class AttemptResult(BaseModel):
    number: int
    plan: FileFixPlan
    success: bool
    phase: Literal["manifest", "resolution", "verification", "review", "completed"]
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    target_verifications: list[TargetVerification] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    diff: str = ""


class VerifiedUpdateArtifact(BaseModel):
    schema_version: Literal[1] = 1
    base_commit: str
    base_ref: str | None = None
    workspace: str
    ecosystem: str
    package_manager: str
    request: UpdateRequest
    target_verifications: list[TargetVerification] = Field(min_length=1, max_length=20)
    changed_files: list[str] = Field(min_length=1)
    diff: str = Field(min_length=1, max_length=5_000_000)
    diff_sha256: str

    @field_validator("base_commit")
    @classmethod
    def valid_base_commit(cls, value: str) -> str:
        return ProjectState.valid_base_commit(value)

    @field_validator("base_ref")
    @classmethod
    def valid_base_ref(cls, value: str | None) -> str | None:
        return ProjectState.valid_base_ref(value)

    @field_validator("workspace")
    @classmethod
    def valid_workspace(cls, value: str) -> str:
        return ProjectState.valid_workspace(value)

    @field_validator("changed_files")
    @classmethod
    def safe_changed_files(cls, values: list[str]) -> list[str]:
        for value in values:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or "\x00" in value:
                raise ValueError(f"artifact contains an unsafe changed file: {value!r}")
        return values

    @field_validator("diff_sha256")
    @classmethod
    def valid_diff_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("diff_sha256 must be a hexadecimal SHA-256 digest")
        return normalized

    @model_validator(mode="after")
    def internally_consistent(self) -> VerifiedUpdateArtifact:
        expected = hashlib.sha256(self.diff.encode()).hexdigest()
        if self.diff_sha256 != expected:
            raise ValueError("diff_sha256 does not match diff content")
        if self.ecosystem != self.request.ecosystem:
            raise ValueError("artifact ecosystem does not match its update request")
        if any(not verification.satisfied for verification in self.target_verifications):
            raise ValueError("artifact may contain only satisfied target verifications")
        verified_targets = [verification.target for verification in self.target_verifications]
        if verified_targets != self.request.targets:
            raise ValueError("artifact verifications do not match the requested targets")
        if self.changed_files != sorted(set(self.changed_files)):
            raise ValueError("artifact changed_files must be sorted and unique")
        return self

    @classmethod
    def from_attempt(
        cls,
        *,
        project: ProjectState,
        request: UpdateRequest,
        attempt: AttemptResult,
    ) -> VerifiedUpdateArtifact:
        if not attempt.success or not attempt.diff:
            raise ValueError("verified artifact requires a successful attempt with a diff")
        return cls(
            base_commit=project.base_commit,
            base_ref=project.base_ref,
            workspace=project.workspace,
            ecosystem=project.ecosystem,
            package_manager=project.package_manager,
            request=request,
            target_verifications=attempt.target_verifications,
            changed_files=attempt.changed_files,
            diff=attempt.diff,
            diff_sha256=hashlib.sha256(attempt.diff.encode()).hexdigest(),
        )


class UpdateResult(BaseModel):
    status: Literal["updated", "already_satisfied", "exhausted", "failed"]
    request: UpdateRequest
    attempts: list[AttemptResult]
    diff: str = ""
    changed_files: list[str] = Field(default_factory=list)
    error: str | None = None
    artifact: VerifiedUpdateArtifact | None = None

    @model_validator(mode="after")
    def verified_artifact_for_updated_result(self) -> UpdateResult:
        if self.status == "updated":
            if self.artifact is None:
                raise ValueError("updated result requires a verified update artifact")
            if self.request != self.artifact.request:
                raise ValueError("result request does not match verified artifact")
            if self.diff != self.artifact.diff:
                raise ValueError("result diff does not match verified artifact")
            if self.changed_files != self.artifact.changed_files:
                raise ValueError("result changed_files do not match verified artifact")
        elif self.artifact is not None:
            raise ValueError("only an updated result may contain a verified artifact")
        return self
