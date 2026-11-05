"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from depfix.adapters import create_adapter
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
    preview.add_argument(
        "--ecosystem",
        choices=("auto", "bundler", "npm"),
        default="auto",
    )
    preview.add_argument(
        "--sandbox",
        choices=("docker", "local"),
        default=os.environ.get("DEPFIX_SANDBOX", "docker"),
        help="resolver execution backend (default: docker)",
    )
    preview.add_argument("--package", required=True)
    preview.add_argument("--safe-version", required=True)
    preview.add_argument("--advisory-id")
    preview.add_argument(
        "--planner",
        choices=("heuristic", "agent", "adapter-agent", "ai"),
        default="heuristic",
    )
    preview.add_argument("--model", default=os.environ.get("DEPFIX_MODEL", ""))
    preview.add_argument("--api-base", default=os.environ.get("DEPFIX_API_BASE"))
    preview.add_argument(
        "--api-key-env",
        help="read an explicit LiteLLM API key from this environment variable",
    )
    preview.add_argument("--max-attempts", type=int, default=5)
    preview.add_argument("--max-turns", type=int, default=12)
    preview.add_argument("--output", type=Path)
    preview.add_argument("--json", action="store_true")
    return parser


async def _preview(args: argparse.Namespace) -> int:
    events = EventLogger(json_lines=args.json)
    repo = args.repo.expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"--repo must be a Git repository: {repo}")

    if args.planner == "agent":
        from depfix.docker_runtime import DockerRuntime
        from depfix.profile import detect_profile
        from depfix.profile_adapter import ProfileAdapter
        from depfix.profile_agent_runtime import ProfileAgentAutofixRunner

        if args.sandbox != "docker":
            raise ValueError("profile-driven agent execution requires --sandbox docker")
        profile = detect_profile(repo, args.workspace, args.ecosystem)
        DockerRuntime.ensure_available()
        adapter = ProfileAdapter(repo, args.workspace, events, profile)
        issue = DependencyIssue(
            ecosystem=profile.ecosystem,
            package=args.package,
            safe_version=args.safe_version,
            advisory_id=args.advisory_id,
        )
        result = await ProfileAgentAutofixRunner(
            adapter,
            events,
            model_name=args.model,
            max_attempts=args.max_attempts,
            max_turns=args.max_turns,
            api_key=_api_key(args),
            base_url=args.api_base,
        ).run(issue)
    else:
        adapter = create_adapter(
            repo,
            args.workspace,
            events,
            args.ecosystem,
            args.sandbox,
        )
        issue = DependencyIssue(
            ecosystem=adapter.ecosystem,
            package=args.package,
            safe_version=args.safe_version,
            advisory_id=args.advisory_id,
        )
        if args.planner == "adapter-agent":
            from depfix.agent_runtime import AgentAutofixRunner

            result = await AgentAutofixRunner(
                adapter,
                events,
                model_name=args.model,
                max_attempts=args.max_attempts,
                max_turns=args.max_turns,
                api_key=_api_key(args),
                base_url=args.api_base,
            ).run(issue)
        else:
            planner = AIPlanner(args.model) if args.planner == "ai" else HeuristicPlanner()
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
    if result.status == "fixed":
        return 0
    return 1 if result.status == "failed" else 2


def _api_key(args: argparse.Namespace) -> str | None:
    if not args.api_key_env:
        return None
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise ValueError(f"environment variable is not set: {args.api_key_env}")
    return api_key


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
