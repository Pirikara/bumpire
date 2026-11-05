"""Small, dependency-free parser for the portions of Gemfile.lock needed by the planner."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from depfix.models import ProjectState

_SPEC = re.compile(r"^    ([A-Za-z0-9_.-]+) \(([^)]+)\)$")
_CHILD = re.compile(r"^      ([A-Za-z0-9_.-]+)(?: \(([^)]+)\))?$")
_DIRECT = re.compile(r"^  ([A-Za-z0-9_.-]+)(?: \(([^)]+)\))?(!)?$")


@dataclass(slots=True)
class GemSpec:
    name: str
    version: str
    dependencies: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class BundlerLock:
    specs: dict[str, GemSpec]
    direct_dependencies: dict[str, str]
    bundler_version: str | None

    @classmethod
    def parse(cls, text: str) -> BundlerLock:
        specs: dict[str, GemSpec] = {}
        direct: dict[str, str] = {}
        section = ""
        current: GemSpec | None = None
        lines = text.splitlines()

        for line in lines:
            if line and not line.startswith(" "):
                section = line
                current = None
                continue
            if section == "GEM" and line == "  specs:":
                section = "SPECS"
                continue
            if section == "SPECS":
                if match := _SPEC.match(line):
                    current = GemSpec(match.group(1), match.group(2))
                    specs.setdefault(current.name, current)
                    continue
                if current is not None and (match := _CHILD.match(line)):
                    current.dependencies[match.group(1)] = match.group(2) or ""
                    continue
            if section == "DEPENDENCIES" and (match := _DIRECT.match(line)):
                direct[match.group(1)] = match.group(2) or ""

        bundler_version = None
        for index, line in enumerate(lines):
            if line == "BUNDLED WITH" and index + 1 < len(lines):
                bundler_version = lines[index + 1].strip() or None
                break
        return cls(specs=specs, direct_dependencies=direct, bundler_version=bundler_version)

    def paths_to(self, target: str) -> list[list[str]]:
        paths: list[list[str]] = []

        def walk(name: str, path: list[str]) -> None:
            if name == target:
                paths.append(path)
                return
            if name in path[:-1]:
                return
            spec = self.specs.get(name)
            if spec is None:
                return
            for child in spec.dependencies:
                walk(child, [*path, child])

        for direct in self.direct_dependencies:
            walk(direct, [direct])
        paths.sort(key=lambda path: (len(path), path))
        return paths

    def project_state(
        self,
        *,
        manifest_path: str,
        lockfile_path: str,
        manifest_text: str,
        target: str,
    ) -> ProjectState:
        return ProjectState(
            ecosystem="bundler",
            package_manager="bundler",
            manifest_path=manifest_path,
            lockfile_path=lockfile_path,
            manifest_text=manifest_text,
            direct_dependencies=self.direct_dependencies,
            locked_versions={name: spec.version for name, spec in self.specs.items()},
            paths_to_target=self.paths_to(target),
            target_locked_version=self.specs.get(target).version if target in self.specs else None,
        )
