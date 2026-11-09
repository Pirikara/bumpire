"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from bumpire.agent_runtime import AgentAutofixRunner
from bumpire.docker_runtime import DockerRuntime
from bumpire.events import EventLogger
from bumpire.models import DependencyTarget, RemediationRequest
from bumpire.profile import detect_profile
from bumpire.profile_executor import ProfileExecutor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bumpire")
    subcommands = parser.add_subparsers(dest="command", required=True)
    preview = subcommands.add_parser("preview", help="generate a verified dependency-fix diff")
    preview.add_argument("--repo", type=Path, required=True)
    preview.add_argument("--workspace", default=".")
    preview.add_argument(
        "--ecosystem",
        default="auto",
        help="profile ecosystem to select when auto-detection is ambiguous",
    )
    preview.add_argument(
        "--target",
        action="append",
        nargs=2,
        metavar=("PACKAGE", "MINIMUM_VERSION"),
        help="add a package and minimum safe version to the atomic remediation batch",
    )
    preview.add_argument("--package", help="single-target compatibility option")
    preview.add_argument("--safe-version", help="single-target compatibility option")
    preview.add_argument(
        "--advisory-id",
        action="append",
        help="advisory ID for the package supplied through the compatibility options",
    )
    preview.add_argument("--model", default=os.environ.get("BUMPIRE_MODEL", ""))
    preview.add_argument("--api-base", default=os.environ.get("BUMPIRE_API_BASE"))
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

    profile = detect_profile(repo, args.workspace, args.ecosystem)
    DockerRuntime.ensure_available()
    executor = ProfileExecutor(repo, args.workspace, events, profile)
    request = _request(args, profile.ecosystem)
    result = await AgentAutofixRunner(
        executor,
        events,
        model_name=args.model,
        max_attempts=args.max_attempts,
        max_turns=args.max_turns,
        api_key=_api_key(args),
        base_url=args.api_base,
    ).run(request)

    if args.output and result.diff:
        args.output.write_text(result.diff, encoding="utf-8")

    if args.json:
        print(result.model_dump_json(indent=2))
    elif result.status == "fixed":
        print(result.diff)
        if args.output:
            print(f"\nPreview written to {args.output}", file=sys.stderr)
    elif result.status == "already_safe":
        packages = ", ".join(target.package for target in result.request.targets)
        print(f"All requested targets are already safe: {packages}")
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
                        "target_resolutions": [
                            resolution.model_dump(mode="json")
                            for resolution in attempt.target_resolutions
                        ],
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
    if result.status in {"fixed", "already_safe"}:
        return 0
    return 1 if result.status == "failed" else 2


def _request(args: argparse.Namespace, ecosystem: str) -> RemediationRequest:
    batch_targets = args.target or []
    uses_compatibility_options = bool(args.package or args.safe_version or args.advisory_id)
    if batch_targets and uses_compatibility_options:
        raise ValueError(
            "--target cannot be combined with --package, --safe-version, or --advisory-id"
        )
    if batch_targets:
        return RemediationRequest(
            ecosystem=ecosystem,
            targets=[
                DependencyTarget(package=package, minimum_version=minimum_version)
                for package, minimum_version in batch_targets
            ],
        )
    if bool(args.package) != bool(args.safe_version):
        raise ValueError("--package and --safe-version must be supplied together")
    if not args.package:
        raise ValueError("supply at least one --target, or --package with --safe-version")
    return RemediationRequest(
        ecosystem=ecosystem,
        targets=[
            DependencyTarget(
                package=args.package,
                minimum_version=args.safe_version,
                advisory_ids=args.advisory_id or [],
            )
        ],
    )


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
        print(f"bumpire: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
