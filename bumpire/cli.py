"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from bumpire.agent_runtime import AgentUpdateRunner
from bumpire.docker_runtime import DockerRuntime
from bumpire.events import EventLogger
from bumpire.models import (
    DependencyTarget,
    UpdateRequest,
    UpdateResult,
    VerifiedUpdateArtifact,
)
from bumpire.profile import detect_profile
from bumpire.profile_executor import ProfileExecutor
from bumpire.publisher import GitHubPublisher


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bumpire")
    subcommands = parser.add_subparsers(dest="command", required=True)
    preview = subcommands.add_parser("preview", help="generate a verified dependency-update diff")
    publish = subcommands.add_parser("publish", help="open a verified GitHub draft pull request")
    for command in (preview, publish):
        _add_update_arguments(command)
    publish.add_argument("--remote", default="origin", help="Git remote used for publication")
    publish.add_argument(
        "--artifact-input",
        type=Path,
        help="publish a previously verified artifact without running the agent again",
    )
    publish.add_argument(
        "--base",
        help="remote base branch; defaults to the branch recorded in the artifact",
    )
    return parser


def _add_update_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--repo", type=Path, required=True)
    command.add_argument("--workspace", default=".")
    command.add_argument(
        "--ecosystem",
        default="auto",
        help="profile ecosystem to select when auto-detection is ambiguous",
    )
    command.add_argument(
        "--target",
        action="append",
        nargs=2,
        metavar=("PACKAGE", "VERSION"),
        help="add an exact package version to the atomic update",
    )
    command.add_argument("--model", default=os.environ.get("BUMPIRE_MODEL", ""))
    command.add_argument("--api-base", default=os.environ.get("BUMPIRE_API_BASE"))
    command.add_argument(
        "--api-key-env",
        help="read an explicit LiteLLM API key from this environment variable",
    )
    command.add_argument("--max-attempts", type=int, default=5)
    command.add_argument("--max-turns", type=int, default=12)
    command.add_argument("--output", type=Path)
    command.add_argument(
        "--artifact-output",
        type=Path,
        help="write the verified update artifact as JSON",
    )
    command.add_argument("--json", action="store_true")


async def _preview(args: argparse.Namespace) -> int:
    events = EventLogger(json_lines=args.json)
    _, result = await _generate_update(args, events)
    _write_outputs(args, result)

    if args.json:
        print(result.model_dump_json(indent=2))
    elif result.status == "updated":
        print(result.diff)
        if args.output:
            print(f"\nPreview written to {args.output}", file=sys.stderr)
        if args.artifact_output:
            print(f"Artifact written to {args.artifact_output}", file=sys.stderr)
    elif result.status == "already_satisfied":
        _print_already_satisfied(result)
    else:
        _print_failed_update(result)
    return _update_exit_code(result)


async def _publish(args: argparse.Namespace) -> int:
    events = EventLogger(json_lines=args.json)
    repo = _repository(args)
    if args.artifact_input:
        _validate_artifact_publish_args(args)
        artifact = VerifiedUpdateArtifact.model_validate_json(
            args.artifact_input.read_text(encoding="utf-8")
        )
        if args.output:
            args.output.write_text(artifact.diff, encoding="utf-8")
        if args.artifact_output:
            args.artifact_output.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    else:
        _, result = await _generate_update(args, events, repo=repo)
        _write_outputs(args, result)
        if result.status == "already_satisfied":
            if args.json:
                print(result.model_dump_json(indent=2))
            else:
                _print_already_satisfied(result)
            return 0
        if result.status != "updated" or result.artifact is None:
            if args.json:
                print(result.model_dump_json(indent=2))
            else:
                _print_failed_update(result)
            return _update_exit_code(result)
        artifact = result.artifact

    publication = await asyncio.to_thread(
        GitHubPublisher(repo, events, remote=args.remote).publish,
        artifact,
        base_ref=args.base,
    )
    if args.json:
        print(json.dumps(asdict(publication), indent=2))
    else:
        print(publication.pull_request_url)
    return 0


async def _generate_update(
    args: argparse.Namespace,
    events: EventLogger,
    *,
    repo: Path | None = None,
) -> tuple[Path, UpdateResult]:
    repo = repo or _repository(args)

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
    return repo, result


def _repository(args: argparse.Namespace) -> Path:
    repo = args.repo.expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"--repo must be a Git repository: {repo}")
    return repo


def _validate_artifact_publish_args(args: argparse.Namespace) -> None:
    if args.target:
        raise ValueError("--artifact-input cannot be combined with update target options")


def _write_outputs(args: argparse.Namespace, result: UpdateResult) -> None:
    if args.output and result.diff:
        args.output.write_text(result.diff, encoding="utf-8")
    if args.artifact_output:
        if result.artifact is None:
            raise ValueError("no verified update artifact was produced")
        args.artifact_output.write_text(result.artifact.model_dump_json(indent=2), encoding="utf-8")


def _print_already_satisfied(result: UpdateResult) -> None:
    packages = ", ".join(target.package for target in result.request.targets)
    print(f"All requested targets already resolve to their exact versions: {packages}")


def _print_failed_update(result: UpdateResult) -> None:
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


def _update_exit_code(result: UpdateResult) -> int:
    if result.status in {"updated", "already_satisfied"}:
        return 0
    return 1 if result.status == "failed" else 2


def _request(args: argparse.Namespace, ecosystem: str) -> UpdateRequest:
    if not args.target:
        raise ValueError("supply at least one --target PACKAGE VERSION")
    return UpdateRequest(
        ecosystem=ecosystem,
        targets=[
            DependencyTarget(package=package, version=_exact_version(version))
            for package, version in args.target
        ],
    )


def _exact_version(value: str) -> str:
    value = value.strip()
    if value.startswith("=="):
        value = value[2:].strip()
    if value.startswith(("=", ">", "<", "!", "~", "^")):
        raise ValueError("--target accepts only an exact version (optionally prefixed by ==)")
    if not value:
        raise ValueError("--target version must not be empty")
    return value


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
        operation = _publish if args.command == "publish" else _preview
        code = asyncio.run(operation(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"bumpire: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
