"""Constrained package.json dependency editing."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from depfix.models import DependencyEdit

DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def apply_dependency_edits(
    workspace: Path,
    edits: list[DependencyEdit],
    *,
    default_manifest: str = "package.json",
) -> list[str]:
    documents: dict[str, dict[str, Any]] = {}

    for edit in edits:
        relative = PurePosixPath(edit.manifest_path or default_manifest)
        if relative.is_absolute() or ".." in relative.parts or relative.name != "package.json":
            raise ValueError(f"unsafe package manifest path: {relative}")
        relative_text = relative.as_posix()
        path = (workspace / Path(*relative.parts)).resolve()
        if not path.is_relative_to(workspace.resolve()) or not path.is_file():
            raise ValueError(f"package manifest does not exist: {relative}")

        document = documents.setdefault(relative_text, load_json_object(path))
        section_name = edit.section or _find_section(document, edit.package) or "dependencies"
        if section_name not in DEPENDENCY_SECTIONS:
            raise ValueError(f"unsupported dependency section: {section_name}")
        section = document.setdefault(section_name, {})
        if not isinstance(section, dict):
            raise ValueError(f"{relative}:{section_name} must be an object")
        section[edit.package] = edit.requirement

    for relative_text, document in documents.items():
        path = workspace / Path(*PurePosixPath(relative_text).parts)
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return sorted(documents)


def _find_section(document: dict[str, Any], package: str) -> str | None:
    for section_name in DEPENDENCY_SECTIONS:
        section = document.get(section_name)
        if isinstance(section, dict) and package in section:
            return section_name
    return None
