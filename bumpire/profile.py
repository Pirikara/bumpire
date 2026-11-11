"""Declarative package-manager profiles used by the generic agent path."""

from __future__ import annotations

import difflib
import json
import re
import tomllib
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class VersionSource(BaseModel):
    path: str
    pattern: str


class RejectedContent(BaseModel):
    path: str
    pattern: str
    reason: str

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError(f"unsafe rejected-content path: {value!r}")
        return value

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, value: str) -> str:
        re.compile(value)
        return value


class ManifestPolicy(BaseModel):
    kind: Literal[
        "json-sections",
        "changed-line-patterns",
        "toml-paths",
        "yaml-dependency-sections",
    ]
    allowed_sections: list[str] = Field(default_factory=list)
    forbid_new_json_section_keys: bool = False
    protected_json_key_pattern: str | None = None
    allowed_line_patterns: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    preserved_toml_table_key_paths: list[str] = Field(default_factory=list)
    mutable_toml_table_value_keys: list[str] = Field(default_factory=list)
    forbid_new_toml_table_keys: bool = False
    preserved_declaration_pattern: str | None = None
    forbid_new_declarations: bool = False
    preserved_toml_item_pattern: str | None = None
    protected_toml_item_pattern: str | None = None

    @model_validator(mode="after")
    def required_rules(self) -> ManifestPolicy:
        if self.kind in {"json-sections", "yaml-dependency-sections"} and not self.allowed_sections:
            raise ValueError(f"{self.kind} policy requires allowed_sections")
        if self.kind == "changed-line-patterns" and not self.allowed_line_patterns:
            raise ValueError("changed-line-patterns policy requires allowed_line_patterns")
        if self.kind == "toml-paths" and not self.allowed_paths:
            raise ValueError("toml-paths policy requires allowed_paths")
        for pattern in self.allowed_line_patterns:
            re.compile(pattern)
        if self.protected_json_key_pattern is not None:
            re.compile(self.protected_json_key_pattern)
        for path in [
            *self.allowed_paths,
            *self.protected_paths,
            *self.preserved_toml_table_key_paths,
        ]:
            if not path or any(
                not re.fullmatch(r"(?:[A-Za-z0-9_-]+|\*)", component)
                for component in path.split(".")
            ):
                raise ValueError(f"unsafe TOML policy path: {path!r}")
        if self.preserved_declaration_pattern is not None:
            compiled = re.compile(self.preserved_declaration_pattern)
            if "name" not in compiled.groupindex:
                raise ValueError("preserved_declaration_pattern requires a named 'name' group")
        elif self.forbid_new_declarations:
            raise ValueError(
                "forbid_new_declarations requires preserved_declaration_pattern"
            )
        if self.forbid_new_toml_table_keys and not self.preserved_toml_table_key_paths:
            raise ValueError(
                "forbid_new_toml_table_keys requires preserved_toml_table_key_paths"
            )
        if self.preserved_toml_item_pattern is not None:
            compiled = re.compile(self.preserved_toml_item_pattern)
            if "name" not in compiled.groupindex:
                raise ValueError("preserved_toml_item_pattern requires a named 'name' group")
        if self.protected_toml_item_pattern is not None:
            re.compile(self.protected_toml_item_pattern)
        for key in self.mutable_toml_table_value_keys:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                raise ValueError(f"unsafe mutable TOML table key: {key!r}")
        return self


class ResolvedGraph(BaseModel):
    """Package-manager-neutral output produced by a trusted driver extractor."""

    schema_version: Literal[1] = 1
    packages: dict[str, list[str]]

    @field_validator("packages")
    @classmethod
    def valid_packages(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        for package, versions in value.items():
            name = package.strip()
            if not name or any(character in name for character in ("\n", "\r", "\x00")):
                raise ValueError("resolved package names must be non-empty single-line values")
            clean_versions: list[str] = []
            for version in versions:
                clean = version.strip()
                if not clean or any(
                    character in clean for character in ("\n", "\r", "\x00")
                ):
                    raise ValueError(
                        "resolved versions must be non-empty single-line values"
                    )
                clean_versions.append(clean)
            normalized[name] = sorted(set(clean_versions))
        return normalized

    def for_targets(self, targets: list[str]) -> dict[str, list[str]]:
        expected = set(targets)
        actual = set(self.packages)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise ValueError(
                "resolved graph does not match requested targets: "
                f"missing={missing}, unexpected={unexpected}"
            )
        return {target: self.packages[target] for target in targets}


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
    rejected_content: list[RejectedContent] = Field(default_factory=list)
    resolve_script: str
    resolved_graph_script: str
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
    def version_placeholder_required(cls, value: str) -> str:
        if not any(
            placeholder in value
            for placeholder in ("{runtime_version}", "{manager_version}")
        ):
            raise ValueError(
                "image_template must contain {runtime_version} or {manager_version}"
            )
        return value

    def matches(self, workdir: Path) -> bool:
        return all((workdir / path).is_file() for path in self.detect_all)

    def manifest_paths(self, workdir: Path) -> list[Path]:
        return self._matching_files(workdir, self.manifest_globs)

    def lockfile_paths(self, workdir: Path) -> list[Path]:
        return self._matching_files(workdir, self.lockfiles)

    @staticmethod
    def _matching_files(workdir: Path, patterns: list[str]) -> list[Path]:
        paths: set[Path] = set()
        for pattern in patterns:
            for path in workdir.glob(pattern):
                if not path.is_file():
                    continue
                relative = path.relative_to(workdir)
                if any(part in {".git", "node_modules", "vendor"} for part in relative.parts):
                    continue
                paths.add(path)
        return sorted(paths)

    def validate_workspace(self, workdir: Path) -> None:
        for rule in self.rejected_content:
            path = workdir / rule.path
            if path.is_file() and re.search(
                rule.pattern,
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            ):
                raise ValueError(
                    f"{self.name} driver rejects {rule.path}: {rule.reason}"
                )

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
        runtime_version = self._safe_image_version(
            self.runtime_version(workdir).removeprefix("ruby-"),
            "runtime",
        )
        manager_version = self.manager_version(workdir)
        if "{manager_version}" in self.image_template:
            manager_version = self._safe_image_version(manager_version, "package-manager")
        return self.image_template.format(
            runtime_version=runtime_version,
            manager_version=manager_version,
        )

    def parse_resolved_graph(self, output: str, targets: list[str]) -> dict[str, list[str]]:
        try:
            graph = ResolvedGraph.model_validate_json(output)
        except ValueError as exc:
            raise ValueError(f"driver returned an invalid resolved graph: {exc}") from exc
        return graph.for_targets(targets)

    def validate_manifest_replacement(self, original: str, replacement: str) -> None:
        if self.manifest_policy.kind == "json-sections":
            self._validate_json_sections(original, replacement)
            return
        if self.manifest_policy.kind == "toml-paths":
            self._validate_toml_paths(original, replacement)
            return
        if self.manifest_policy.kind == "yaml-dependency-sections":
            self._validate_yaml_dependency_sections(original, replacement)
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
        self._validate_preserved_declarations(original, replacement)

    def _validate_preserved_declarations(self, original: str, replacement: str) -> None:
        pattern = self.manifest_policy.preserved_declaration_pattern
        if pattern is None:
            return
        before = {match.group("name") for match in re.finditer(pattern, original, re.MULTILINE)}
        after = {match.group("name") for match in re.finditer(pattern, replacement, re.MULTILINE)}
        removed = sorted(before - after)
        if removed:
            raise ValueError(f"dependency declarations may not be removed: {removed}")
        if self.manifest_policy.forbid_new_declarations:
            added = sorted(after - before)
            if added:
                raise ValueError(f"dependency declarations may not be added: {added}")

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
            if (
                self.manifest_policy.forbid_new_json_section_keys
                and section not in before
                and after[section]
            ):
                raise ValueError(
                    f"dependencies may not be added to {section}: {sorted(after[section])}"
                )
        for section in allowed & set(before):
            if not isinstance(before[section], dict):
                raise ValueError(f"dependency section must be an object: {section}")
            if section not in after:
                raise ValueError(f"dependency section may not be removed: {section}")
            removed = sorted(set(before[section]) - set(after[section]))
            if removed:
                raise ValueError(f"dependencies may not be removed from {section}: {removed}")
            added = sorted(set(after[section]) - set(before[section]))
            if self.manifest_policy.forbid_new_json_section_keys and added:
                raise ValueError(f"dependencies may not be added to {section}: {added}")
            protected_pattern = self.manifest_policy.protected_json_key_pattern
            if protected_pattern is not None:
                protected = {
                    key
                    for key in set(before[section]) | set(after[section])
                    if re.search(protected_pattern, key)
                }
                changed_protected = sorted(
                    key
                    for key in protected
                    if key not in before[section]
                    or key not in after[section]
                    or before[section][key] != after[section][key]
                )
                if changed_protected:
                    raise ValueError(
                        "manifest replacement changes protected JSON dependencies in "
                        f"{section}: {changed_protected}"
                    )

    def _validate_yaml_dependency_sections(self, original: str, replacement: str) -> None:
        before_projection, before_sections = self._yaml_dependency_document(original)
        after_projection, after_sections = self._yaml_dependency_document(replacement)
        if before_projection != after_projection:
            raise ValueError("manifest replacement changes protected YAML fields")

        for section in self.manifest_policy.allowed_sections:
            before = before_sections.get(section)
            after = after_sections.get(section)
            if before is None or after is None:
                if before != after:
                    raise ValueError(
                        f"YAML dependency section may not be added or removed: {section}"
                    )
                continue
            self._validate_yaml_dependency_section(section, before, after)

    def _yaml_dependency_document(
        self,
        document: str,
    ) -> tuple[str, dict[str, list[str]]]:
        lines = document.splitlines(keepends=True)
        top_level: list[tuple[int, str, str]] = []
        for index, line in enumerate(lines):
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$", line)
            if match is not None:
                top_level.append((index, match.group(1), match.group(2)))

        allowed = set(self.manifest_policy.allowed_sections)
        sections: dict[str, list[str]] = {}
        projection: list[str] = []
        cursor = 0
        for position, (start, name, remainder) in enumerate(top_level):
            end = top_level[position + 1][0] if position + 1 < len(top_level) else len(lines)
            projection.extend(lines[cursor:start])
            block = lines[start:end]
            is_block_section = re.fullmatch(r"[ \t]*(?:#.*)?(?:\r?\n)?", remainder)
            if name in allowed and is_block_section:
                if name in sections:
                    raise ValueError(f"duplicate YAML dependency section: {name}")
                sections[name] = block
                projection.append(lines[start])
                projection.append(f"\0bumpire-yaml-section:{name}\0\n")
            else:
                projection.extend(block)
            cursor = end
        projection.extend(lines[cursor:])
        return "".join(projection), sections

    def _validate_yaml_dependency_section(
        self,
        section: str,
        before: list[str],
        after: list[str],
    ) -> None:
        before_prefix, before_order, before_entries = self._yaml_dependency_entries(before)
        after_prefix, after_order, after_entries = self._yaml_dependency_entries(after)
        removed = sorted(set(before_entries) - set(after_entries))
        if removed:
            raise ValueError(f"dependencies may not be removed from {section}: {removed}")
        added = sorted(set(after_entries) - set(before_entries))
        if added:
            raise ValueError(f"dependencies may not be added to {section}: {added}")
        if before_prefix != after_prefix or before_order != after_order:
            raise ValueError(f"dependency declaration layout may not change in {section}")

        for dependency in before_order:
            previous = before_entries[dependency]
            proposed = after_entries[dependency]
            if previous == proposed:
                continue
            before_scalar = self._yaml_version_declaration(previous)
            after_scalar = self._yaml_version_declaration(proposed)
            if before_scalar is None or after_scalar is None:
                raise ValueError(
                    "dependency source or selector fields may not change for "
                    f"{dependency} in {section}"
                )
            before_shape, _ = before_scalar
            after_shape, _ = after_scalar
            if before_shape != after_shape or previous[1:] != proposed[1:]:
                raise ValueError(
                    "dependency declaration formatting may not change for "
                    f"{dependency} in {section}"
                )

    @staticmethod
    def _yaml_dependency_entries(
        block: list[str],
    ) -> tuple[list[str], list[str], dict[str, list[str]]]:
        body = block[1:]
        candidates: list[tuple[int, int, str]] = []
        for index, line in enumerate(body):
            match = re.match(r"^( +)([a-z][a-z0-9_]*):(.*)$", line)
            if match is not None:
                candidates.append((index, len(match.group(1)), match.group(2)))
        if not candidates:
            return body, [], {}
        indentation = min(candidate[1] for candidate in candidates)
        declarations = [candidate for candidate in candidates if candidate[1] == indentation]
        prefix = body[: declarations[0][0]]
        order: list[str] = []
        entries: dict[str, list[str]] = {}
        for position, (start, _, name) in enumerate(declarations):
            if name in entries:
                raise ValueError(f"duplicate YAML dependency declaration: {name}")
            end = declarations[position + 1][0] if position + 1 < len(declarations) else len(body)
            order.append(name)
            entries[name] = body[start:end]
        return prefix, order, entries

    @staticmethod
    def _yaml_version_declaration(
        block: list[str],
    ) -> tuple[tuple[str, str, str, str, str], str] | None:
        if not block or any(
            re.fullmatch(r"[ \t]*(?:#.*)?(?:\r?\n)?", line) is None
            for line in block[1:]
        ):
            return None
        match = re.fullmatch(
            r"(?P<indent> +)(?P<name>[a-z][a-z0-9_]*):"
            r"(?P<spacing>[ \t]+)(?P<value>[^#\r\n]*?)"
            r"(?P<comment>[ \t]+#.*)?(?P<newline>\r?\n)?",
            block[0],
        )
        if match is None:
            return None
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        elif '"' in value or "'" in value:
            return None
        if re.fullmatch(r"[0-9A-Za-z.*^<>=+ -]+", value) is None:
            return None
        shape = (
            match.group("indent"),
            match.group("name"),
            match.group("spacing"),
            match.group("comment") or "",
            match.group("newline") or "",
        )
        return shape, value

    def _validate_toml_paths(self, original: str, replacement: str) -> None:
        try:
            before = tomllib.loads(original)
            after = tomllib.loads(replacement)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"manifest replacement is not valid TOML: {exc}") from exc
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError("TOML dependency manifest must contain a table")

        missing = object()
        pattern = self.manifest_policy.preserved_toml_item_pattern
        protected_item_pattern = self.manifest_policy.protected_toml_item_pattern
        table_key_patterns = [
            path.split(".")
            for path in self.manifest_policy.preserved_toml_table_key_paths
        ]
        for dotted_pattern in self.manifest_policy.allowed_paths:
            path_pattern = dotted_pattern.split(".")
            before_paths = self._matching_toml_paths(before, path_pattern)
            after_paths = self._matching_toml_paths(after, path_pattern)
            for path in before_paths:
                previous = self._nested_value(before, path, missing)
                proposed = self._nested_value(after, path, missing)
                if proposed is missing:
                    raise ValueError(
                        f"TOML dependency path may not be removed: {'.'.join(path)}"
                    )
                preserves_table_keys = any(
                    len(path) == len(table_pattern)
                    and self._toml_path_within_pattern(path, table_pattern)
                    for table_pattern in table_key_patterns
                )
                if pattern is not None and not preserves_table_keys:
                    before_items = self._toml_dependency_items(previous, pattern)
                    after_items = self._toml_dependency_items(proposed, pattern)
                    removed = list((before_items - after_items).elements())
                    if removed:
                        raise ValueError(
                            "dependency declarations may not be removed from "
                            f"{'.'.join(path)}: {removed[:3]}"
                        )
                if protected_item_pattern is not None and not preserves_table_keys:
                    before_items = self._matching_toml_strings(
                        previous, protected_item_pattern
                    )
                    after_items = self._matching_toml_strings(
                        proposed, protected_item_pattern
                    )
                    if before_items != after_items:
                        raise ValueError(
                            "dependency source declarations may not change in "
                            f"{'.'.join(path)}"
                        )
            for path in before_paths | after_paths:
                proposed = self._nested_value(after, path, missing)
                if proposed is not missing and not isinstance(proposed, (dict, list)):
                    raise ValueError(
                        "TOML dependency path must remain a table or array: "
                        f"{'.'.join(path)}"
                    )

        for dotted_pattern in self.manifest_policy.preserved_toml_table_key_paths:
            path_pattern = dotted_pattern.split(".")
            table_paths = self._matching_toml_paths(before, path_pattern)
            if self.manifest_policy.forbid_new_toml_table_keys:
                table_paths.update(self._matching_toml_paths(after, path_pattern))
            for path in table_paths:
                previous = self._nested_value(before, path, missing)
                proposed = self._nested_value(after, path, missing)
                if previous is missing:
                    if isinstance(proposed, dict) and proposed:
                        raise ValueError(
                            "dependency declarations may not be added to "
                            f"{'.'.join(path)}: {sorted(proposed)[:3]}"
                        )
                    continue
                if not isinstance(previous, dict) or not isinstance(proposed, dict):
                    raise ValueError(
                        f"TOML dependency table may not be removed: {'.'.join(path)}"
                    )
                removed = sorted(set(previous) - set(proposed))
                if removed:
                    raise ValueError(
                        "dependency declarations may not be removed from "
                        f"{'.'.join(path)}: {removed[:3]}"
                    )
                added = sorted(set(proposed) - set(previous))
                if self.manifest_policy.forbid_new_toml_table_keys and added:
                    raise ValueError(
                        "dependency declarations may not be added to "
                        f"{'.'.join(path)}: {added[:3]}"
                    )
                mutable_keys = set(self.manifest_policy.mutable_toml_table_value_keys)
                for dependency in sorted(set(previous) & set(proposed)):
                    before_shape = self._protected_toml_value(
                        previous[dependency], mutable_keys
                    )
                    after_shape = self._protected_toml_value(
                        proposed[dependency], mutable_keys
                    )
                    if before_shape != after_shape:
                        raise ValueError(
                            "dependency source or selector fields may not change for "
                            f"{dependency} in {'.'.join(path)}"
                        )
                for dependency in added:
                    if self._protected_toml_value(proposed[dependency], mutable_keys) is not None:
                        raise ValueError(
                            "new dependency must use a registry version constraint: "
                            f"{dependency} in {'.'.join(path)}"
                        )

        for dotted_pattern in self.manifest_policy.protected_paths:
            path_pattern = dotted_pattern.split(".")
            candidate_paths = self._matching_toml_paths(before, path_pattern)
            candidate_paths.update(self._matching_toml_paths(after, path_pattern))
            if "*" not in path_pattern:
                candidate_paths.add(tuple(path_pattern))
            for path in candidate_paths:
                if self._nested_value(before, path, missing) != self._nested_value(
                    after, path, missing
                ):
                    raise ValueError(
                        f"manifest replacement changes protected TOML field: {'.'.join(path)}"
                    )

        changed = self._changed_toml_paths(
            before,
            after,
            allowed=[path.split(".") for path in self.manifest_policy.allowed_paths],
        )
        if changed:
            rendered = [".".join(path) or "<root>" for path in changed[:3]]
            raise ValueError(f"manifest replacement changes protected TOML fields: {rendered}")

    @staticmethod
    def _nested_value(
        document: dict[str, object], path: list[str] | tuple[str, ...], missing: object
    ) -> object:
        current: object = document
        for component in path:
            if not isinstance(current, dict) or component not in current:
                return missing
            current = current[component]
        return current

    @classmethod
    def _matching_toml_paths(
        cls,
        document: dict[str, object],
        pattern: list[str],
    ) -> set[tuple[str, ...]]:
        matches: set[tuple[str, ...]] = set()

        def walk(current: object, index: int, path: tuple[str, ...]) -> None:
            if index == len(pattern):
                matches.add(path)
                return
            if not isinstance(current, dict):
                return
            component = pattern[index]
            if component == "*":
                for key, value in current.items():
                    walk(value, index + 1, (*path, key))
            elif component in current:
                walk(current[component], index + 1, (*path, component))

        walk(document, 0, ())
        return matches

    @classmethod
    def _changed_toml_paths(
        cls,
        before: object,
        after: object,
        *,
        allowed: list[list[str]],
        path: tuple[str, ...] = (),
    ) -> list[tuple[str, ...]]:
        if before == after:
            return []
        if any(cls._toml_path_within_pattern(path, pattern) for pattern in allowed):
            return []
        if isinstance(before, dict) or isinstance(after, dict):
            if before is not None and not isinstance(before, dict):
                return [path]
            if after is not None and not isinstance(after, dict):
                return [path]
            before_table = before if isinstance(before, dict) else {}
            after_table = after if isinstance(after, dict) else {}
            changes: list[tuple[str, ...]] = []
            for key in sorted(set(before_table) | set(after_table)):
                changes.extend(
                    cls._changed_toml_paths(
                        before_table.get(key),
                        after_table.get(key),
                        allowed=allowed,
                        path=(*path, key),
                    )
                )
            return changes
        return [path]

    @staticmethod
    def _toml_path_within_pattern(path: tuple[str, ...], pattern: list[str]) -> bool:
        return len(path) >= len(pattern) and all(
            expected == "*" or expected == actual
            for actual, expected in zip(path, pattern, strict=False)
        )

    @staticmethod
    def _toml_dependency_items(value: object, pattern: str) -> Counter[str]:
        items: Counter[str] = Counter()
        compiled = re.compile(pattern)

        def collect(candidate: object, *, list_item: bool = False) -> None:
            if isinstance(candidate, str):
                match = compiled.search(candidate)
                if match is not None:
                    items[f"package:{match.group('name')}"] += 1
                return
            if isinstance(candidate, list):
                for item in candidate:
                    collect(item, list_item=True)
                return
            if isinstance(candidate, dict):
                if list_item:
                    serialized = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
                    items[f"entry:{serialized}"] += 1
                    return
                for item in candidate.values():
                    collect(item)

        collect(value)
        return items

    @staticmethod
    def _matching_toml_strings(value: object, pattern: str) -> Counter[str]:
        matches: Counter[str] = Counter()
        compiled = re.compile(pattern)

        def collect(candidate: object) -> None:
            if isinstance(candidate, str):
                if compiled.search(candidate):
                    matches[candidate] += 1
                return
            if isinstance(candidate, list):
                for item in candidate:
                    collect(item)
                return
            if isinstance(candidate, dict):
                for item in candidate.values():
                    collect(item)

        collect(value)
        return matches

    @classmethod
    def _protected_toml_value(
        cls,
        value: object,
        mutable_keys: set[str],
    ) -> object:
        if isinstance(value, str):
            return None
        if isinstance(value, list):
            return tuple(cls._protected_toml_value(item, mutable_keys) for item in value)
        if isinstance(value, dict):
            protected = {
                key: item
                for key, item in value.items()
                if key not in mutable_keys
            }
            return protected or None
        return value

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

    @staticmethod
    def _safe_image_version(value: str, kind: str) -> str:
        if not re.fullmatch(r"\d+(?:\.\d+){0,2}", value):
            raise ValueError(f"unsafe {kind} version in profile input: {value!r}")
        return value


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
