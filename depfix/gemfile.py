"""Constrained Gemfile editing for dependency requirements."""

from __future__ import annotations

import re

from depfix.models import DependencyEdit


def apply_edit(text: str, edit: DependencyEdit) -> str:
    package = re.escape(edit.package)
    pattern = re.compile(
        rf"^(?P<indent>\s*)gem\s+(?P<quote>['\"]){package}(?P=quote)"
        rf"(?:\s*,\s*(?P<version_quote>['\"])[^'\"]*(?P=version_quote))?"
        rf"(?P<rest>.*)$",
        re.MULTILINE,
    )
    match = pattern.search(text)
    replacement = f'gem "{edit.package}", "{edit.requirement}"'
    if match is not None:
        replacement = f"{match.group('indent')}{replacement}{match.group('rest')}"
        return f"{text[: match.start()]}{replacement}{text[match.end() :]}"

    suffix = "" if text.endswith("\n") else "\n"
    return f"{text}{suffix}\n{replacement}\n"


def apply_edits(text: str, edits: list[DependencyEdit]) -> str:
    for edit in edits:
        text = apply_edit(text, edit)
    return text
