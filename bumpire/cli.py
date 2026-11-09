"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from bumpire.agent_runtime import AgentUpdateRunner
from bumpire.docker_runtime import DockerRuntime
from bumpire.events import EventLogger
from bumpire.models import DependencyTarget, UpdateRequest, VersionRequirement
from bumpire.profile import detect_profile
from bumpire.profile_executor import ProfileExecutor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bumpire")
    subcommands = parser.add_subparsers(dest="command", required=True)
    preview = subcommands.add_parser("preview", help="generate a verified dependency-update diff")
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
        metavar=("PACKAGE", "REQUIREMENT"),
        help="add an exact version or an explicit >= version policy to the atomic update",
    )
    preview.add_argument(
        "--allow-removal",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="allow a requested package to disappear from the resolved dependency graph",
    )
    preview.add_argument("--package", help="single-target compatibility option")
    preview.add_argument("--safe-version", help="single-target compatibility option")
    preview.add_argument("--model", default=os.environ.get("BUMPIRE_MODEL", ""))
    preview.add_argument("--api-base", default=os.environ.get("BUMPIRE_API_BASE"))
    preview.add_argument(
        "--api-key-env",
        help="read an explicit LiteLLM API key from this environment variable",
    )
    preview.add_argument("--max-attempts", type=int, default=5)
    preview.add_argument("--max-turns", type=int, default=12)
    preview.add_argument("--output", type=Path)
    preview.add_argument(
        "--artifact-output",
        type=Path,
        help="write the verified update artifact as JSON",
    )
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
    result = await AgentUpdateRunner(
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
    if args.artifact_output:
        if result.artifact is None:
            raise ValueError("no verified update artifact was produced")
        args.artifact_output.write_text(result.artifact.model_dump_json(indent=2), encoding="utf-8")

    if args.json:
        print(result.model_dump_json(indent=2))
    elif result.status == "updated":
        print(result.diff)
        if args.output:
            print(f"\nPreview written to {args.output}", file=sys.stderr)
        if args.artifact_output:
            print(f"Artifact written to {args.artifact_output}", file=sys.stderr)
    elif result.status == "already_satisfied":
        packages = ", ".join(target.package for target in result.request.targets)
        print(f"All requested targets already satisfy their policies: {packages}")
    else:
        print(f"Update {result.status}: {result.error}", file=sys.stderr)
        for attempt in result.attempts:
            print(
                json.dumps(
                    {
                        "attempt": attempt.number,
                        "phase": attempt.phase,
                        "error": attempt.error,
                        "stderr": attempt.stderr,
                        "target_verifications": [
                            verification.model_dump(mode="json")
                            for verification in attempt.target_verifications
                        ],
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
    if result.status in {"updated", "already_satisfied"}:
        return 0
    return 1 if result.status == "failed" else 2


def _request(args: argparse.Namespace, ecosystem: str) -> UpdateRequest:
    batch_targets = args.target or []
    uses_compatibility_options = bool(args.package or args.safe_version)
    if batch_targets and uses_compatibility_options:
        raise ValueError("--target cannot be combined with --package or --safe-version")
    if batch_targets:
        targets = [
            DependencyTarget(
                package=package,
                requirement=_requirement(requirement),
                allow_removal=package in args.allow_removal,
            )
            for package, requirement in batch_targets
        ]
        _validate_removal_packages(args.allow_removal, targets)
        return UpdateRequest(
            ecosystem=ecosystem,
            targets=targets,
        )
    if bool(args.package) != bool(args.safe_version):
        raise ValueError("--package and --safe-version must be supplied together")
    if not args.package:
        raise ValueError("supply at least one --target, or --package with --safe-version")
    targets = [
        DependencyTarget(
            package=args.package,
            requirement=VersionRequirement(kind="at_least", version=args.safe_version),
            allow_removal=args.package in args.allow_removal,
        )
    ]
    _validate_removal_packages(args.allow_removal, targets)
    return UpdateRequest(
        ecosystem=ecosystem,
        targets=targets,
    )


def _requirement(value: str) -> VersionRequirement:
    value = value.strip()
    if value.startswith("=="):
        return VersionRequirement(kind="exact", version=value[2:])
    if value.startswith(">="):
        return VersionRequirement(kind="at_least", version=value[2:])
    if value.startswith(("=", ">", "<", "!", "~", "^")):
        raise ValueError("version requirements support only exact versions, ==, or >=")
    return VersionRequirement(kind="exact", version=value)


def _validate_removal_packages(
    packages: list[str],
    targets: list[DependencyTarget],
) -> None:
    unknown = sorted(set(packages) - {target.package for target in targets})
    if unknown:
        raise ValueError(f"--allow-removal names packages that are not targets: {unknown}")


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
