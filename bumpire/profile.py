"""Declarative package-manager profiles used by the generic agent path."""

from __future__ import annotations

import difflib
import json
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class VersionSource(BaseModel):
    path: str
    pattern: str


class ManifestPolicy(BaseModel):
    kind: Literal["json-sections", "changed-line-patterns"]
    allowed_sections: list[str] = Field(default_factory=list)
    allowed_line_patterns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def required_rules(self) -> ManifestPolicy:
        if self.kind == "json-sections" and not self.allowed_sections:
            raise ValueError("json-sections policy requires allowed_sections")
        if self.kind == "changed-line-patterns" and not self.allowed_line_patterns:
            raise ValueError("changed-line-patterns policy requires allowed_line_patterns")
        for pattern in self.allowed_line_patterns:
            re.compile(pattern)
        return self


class PackageManagerProfile(BaseModel):
    name: str
    ecosystem: str
    detect_all: list[str] = Field(min_length=1)
    manifest_globs: list[str] = Field(min_length=1)
    lockfiles: list[str] = Field(min_length=1)
    image_template: str
    default_runtime_version: str
    runtime_version_sources: list[VersionSource] = Field(default_factory=list)
    default_manager_version: str = ""
    manager_version_sources: list[VersionSource] = Field(default_factory=list)
    resolver_script: str
    locked_version_pattern: str
    instructions: str
    manifest_policy: ManifestPolicy

    @field_validator("detect_all", "manifest_globs", "lockfiles")
    @classmethod
    def safe_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or "\x00" in value:
                raise ValueError(f"unsafe profile path: {value!r}")
        return values

    @field_validator("image_template")
    @classmethod
    def runtime_placeholder_required(cls, value: str) -> str:
        if "{runtime_version}" not in value:
            raise ValueError("image_template must contain {runtime_version}")
        return value

    def matches(self, workdir: Path) -> bool:
        return all((workdir / path).is_file() for path in self.detect_all)

    def manifest_paths(self, workdir: Path) -> list[Path]:
        paths: set[Path] = set()
        for pattern in self.manifest_globs:
            for path in workdir.glob(pattern):
                if not path.is_file():
                    continue
                relative = path.relative_to(workdir)
                if any(part in {".git", "node_modules", "vendor"} for part in relative.parts):
                    continue
                paths.add(path)
        return sorted(paths)

    def allows_manifest(self, relative_path: str) -> bool:
        path = PurePosixPath(relative_path)
        return any(path.match(pattern) for pattern in self.manifest_globs)

    def runtime_version(self, workdir: Path) -> str:
        return self._extract_version(
            workdir,
            self.runtime_version_sources,
            self.default_runtime_version,
        )

    def manager_version(self, workdir: Path) -> str:
        return self._extract_version(
            workdir,
            self.manager_version_sources,
            self.default_manager_version,
        )

    def image(self, workdir: Path) -> str:
        version = self.runtime_version(workdir).removeprefix("ruby-")
        if not re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
            raise ValueError(f"unsafe runtime version in profile input: {version!r}")
        return self.image_template.format(runtime_version=version)

    def locked_version(self, lock_text: str, package: str) -> str | None:
        pattern = self.locked_version_pattern.replace("{package}", re.escape(package))
        match = re.search(pattern, lock_text, re.MULTILINE | re.DOTALL)
        return match.group("version") if match is not None else None

    def validate_manifest_replacement(self, original: str, replacement: str) -> None:
        if self.manifest_policy.kind == "json-sections":
            self._validate_json_sections(original, replacement)
            return
        changed_lines = [
            line[2:]
            for line in difflib.ndiff(original.splitlines(), replacement.splitlines())
            if line.startswith(("- ", "+ "))
        ]
        disallowed = [
            line
            for line in changed_lines
            if not any(
                re.search(pattern, line) for pattern in self.manifest_policy.allowed_line_patterns
            )
        ]
        if disallowed:
            raise ValueError(
                f"manifest replacement changes lines outside the profile policy: {disallowed[:3]}"
            )

    def _validate_json_sections(self, original: str, replacement: str) -> None:
        try:
            before = json.loads(original)
            after = json.loads(replacement)
        except json.JSONDecodeError as exc:
            raise ValueError(f"manifest replacement is not valid JSON: {exc}") from exc
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError("JSON dependency manifest must contain an object")
        allowed = set(self.manifest_policy.allowed_sections)
        protected_keys = (set(before) | set(after)) - allowed
        changed = sorted(
            key
            for key in protected_keys
            if key not in before or key not in after or before[key] != after[key]
        )
        if changed:
            raise ValueError(f"manifest replacement changes protected JSON fields: {changed}")
        for section in allowed & set(after):
            if not isinstance(after[section], dict):
                raise ValueError(f"dependency section must remain an object: {section}")
        for section in allowed & set(before):
            if not isinstance(before[section], dict):
                raise ValueError(f"dependency section must be an object: {section}")
            if section not in after:
                raise ValueError(f"dependency section may not be removed: {section}")
            removed = sorted(set(before[section]) - set(after[section]))
            if removed:
                raise ValueError(f"dependencies may not be removed from {section}: {removed}")

    @staticmethod
    def _extract_version(
        workdir: Path,
        sources: list[VersionSource],
        default: str,
    ) -> str:
        for source in sources:
            path = workdir / source.path
            if not path.is_file():
                continue
            match = re.search(source.pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
            if match is not None:
                return match.group("version").strip()
        return default


def load_builtin_profiles() -> list[PackageManagerProfile]:
    directory = Path(__file__).with_name("profiles")
    return [
        PackageManagerProfile.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.toml"))
    ]


def detect_profile(
    repo: Path,
    workspace: str,
    requested_ecosystem: str = "auto",
) -> PackageManagerProfile:
    workdir = (repo / workspace).resolve()
    if not workdir.is_relative_to(repo.resolve()) or not workdir.is_dir():
        raise ValueError(f"workspace does not exist: {workspace}")
    profiles = load_builtin_profiles()
    if requested_ecosystem != "auto":
        profiles = [profile for profile in profiles if profile.ecosystem == requested_ecosystem]
        if not profiles:
            raise ValueError(f"unsupported ecosystem: {requested_ecosystem}")
    matches = [profile for profile in profiles if profile.matches(workdir)]
    if not matches:
        raise ValueError("no declarative package-manager profile matches this workspace")
    if len(matches) > 1:
        names = ", ".join(profile.name for profile in matches)
        raise ValueError(f"multiple package-manager profiles match: {names}")
    return matches[0]
