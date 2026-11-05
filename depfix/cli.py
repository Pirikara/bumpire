"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from depfix.bundler import BundlerAdapter
from depfix.engine import AutofixEngine
from depfix.events import EventLogger
from depfix.models import DependencyIssue
from depfix.planner import AIPlanner, HeuristicPlanner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="depfix")
    subcommands = parser.add_subparsers(dest="command", required=True)
    preview = subcommands.add_parser("preview", help="generate a verified dependency-fix diff")
    preview.add_argument("--repo", type=Path, required=True)
    preview.add_argument("--workspace", default=".")
    preview.add_argument("--package", required=True)
    preview.add_argument("--safe-version", required=True)
    preview.add_argument("--advisory-id")
    preview.add_argument("--planner", choices=("heuristic", "ai"), default="heuristic")
    preview.add_argument("--model", default=os.environ.get("DEPFIX_MODEL", ""))
    preview.add_argument("--max-attempts", type=int, default=5)
    preview.add_argument("--output", type=Path)
    preview.add_argument("--json", action="store_true")
    return parser


async def _preview(args: argparse.Namespace) -> int:
    events = EventLogger(json_lines=args.json)
    repo = args.repo.expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"--repo must be a Git repository: {repo}")

    planner = AIPlanner(args.model) if args.planner == "ai" else HeuristicPlanner()
    issue = DependencyIssue(
        package=args.package,
        safe_version=args.safe_version,
        advisory_id=args.advisory_id,
    )
    adapter = BundlerAdapter(repo, args.workspace, events)
    result = await AutofixEngine(
        adapter,
        planner,
        events,
        max_attempts=args.max_attempts,
    ).run(issue)

    if args.output and result.diff:
        args.output.write_text(result.diff, encoding="utf-8")

    if args.json:
        print(result.model_dump_json(indent=2))
    elif result.status == "fixed":
        print(result.diff)
        if args.output:
            print(f"\nPreview written to {args.output}", file=sys.stderr)
    else:
        print(f"AutoFix {result.status}: {result.error}", file=sys.stderr)
        for attempt in result.attempts:
            print(
                json.dumps(
                    {
                        "attempt": attempt.number,
                        "phase": attempt.phase,
                        "error": attempt.error,
                        "stderr": attempt.stderr,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
    return 0 if result.status == "fixed" else 2


def main() -> None:
    args = _parser().parse_args()
    try:
        code = asyncio.run(_preview(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"depfix: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
