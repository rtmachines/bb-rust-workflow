"""Portable command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .backends import probe
from .config import (
    DEFAULT_CHECKS,
    LEGACY_CHECKS,
    Config,
    ConfigError,
    default_text,
    load_config,
    route,
)
from .engine import Workflow
from .lifecycle import read_progress, termination_as_interrupt
from .process import run_process
from .requirements import (
    RequirementsError,
    bind_requirements,
    load_requirements,
    verification_plan_id,
)
from .rust_review import prepare_rust_review, rust_review_context
from .verification import plan_verification, verify_repository
from .workspace import WorkspaceError, apply_run, snapshot_environment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bb-workflow", description="Bounded Rust agent workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="Write a configuration template without overwriting files"
    )
    init.add_argument("--config", type=Path, default=Path("workflow.toml"))
    for name in ("plan", "run", "doctor"):
        command = commands.add_parser(name)
        command.add_argument("--repo", type=Path, default=Path.cwd())
        command.add_argument("--config", type=Path)
        if name == "run":
            command.add_argument(
                "--events-jsonl", action="store_true", help="Emit lifecycle JSONL on stderr"
            )
        if name != "doctor":
            command.add_argument(
                "--requirements", type=Path, help="Versioned acceptance criteria TOML"
            )
            command.add_argument("task", help="Task text (quote spaces in your shell)")
            command.add_argument(
                "--complexity", choices=("auto", "low", "medium", "high"), default="auto"
            )
    verify = commands.add_parser("verify", help="Run checks in the project without providers")
    verify.add_argument("profile", choices=("fast", "focused", "lint", "gate"))
    verify.add_argument("--repo", type=Path, default=Path.cwd())
    verify.add_argument("--config", type=Path)
    verify.add_argument("--package")
    verify.add_argument("--test-filter")
    for name in ("status", "apply"):
        command = commands.add_parser(name)
        command.add_argument("run_dir", type=Path)
        if name == "status":
            command.add_argument(
                "--summary",
                action="store_true",
                help="Show progress and usage without raw evidence",
            )
    return parser


def _config(args: argparse.Namespace):
    explicit = args.config
    discovered = args.repo.resolve() / "workflow.toml"
    return load_config(
        explicit if explicit is not None else discovered if discovered.is_file() else None
    )


def _checks_scope(config: Config) -> dict[str, str]:
    commands = tuple(tuple(command) for command in config.checks)
    if commands == DEFAULT_CHECKS:
        return {
            "kind": "workspace_all_targets_and_doctests",
            "description": (
                "Format the whole workspace; check, test and Clippy all workspace targets; "
                "run workspace doctests separately. Cargo builds use --locked; "
                "Clippy warnings are errors. Features and target use Cargo defaults."
            ),
        }
    if commands == LEGACY_CHECKS:
        return {
            "kind": "legacy_defaults",
            "description": (
                "Legacy commands preserved: Cargo selects packages from the current manifest "
                "and default-members; no separate doctest check or --locked guarantee."
            ),
        }
    return {
        "kind": "configured_commands",
        "description": (
            "Scope is defined by the exact configured commands shown in checks. "
            "Full workspace, all targets and doctest coverage are not inferred."
        ),
    }


def _doctor(config, repo: Path) -> tuple[dict, bool]:
    used_backends = {
        selection.backend
        for level in config.profiles
        for selection in route(config, "", level)[1].values()
    }
    providers = {
        backend: probe(
            executable, backend, cwd=repo.resolve(), max_output_bytes=config.max_output_bytes
        )
        for backend, executable in config.executables.items()
        if backend in used_backends
    }
    executables = dict.fromkeys(
        ["git", *(command[0] for command in [*config.prepare, *config.checks])]
    )
    checks = {}
    for executable in executables:
        try:
            result = run_process(
                [executable, "--version"],
                cwd=repo.resolve(),
                timeout=10,
                env=snapshot_environment(),
                max_output_bytes=config.max_output_bytes,
            )
            checks[executable] = {
                "available": (
                    result.returncode == 0
                    and not result.timed_out
                    and not result.output_limit_exceeded
                ),
                "version": result.stdout.strip() or result.stderr.strip(),
            }
            if result.output_limit_exceeded:
                checks[executable]["error"] = "Version check exceeded the output byte limit"
        except OSError as exc:
            checks[executable] = {"available": False, "error": str(exc)}
    payload = {
        "backends": providers,
        "git": checks["git"],
        "preparation_executables": {command[0]: checks[command[0]] for command in config.prepare},
        "validation_executables": {command[0]: checks[command[0]] for command in config.checks},
        "note": "Metadata probes only; authentication, model access and LSP are not verified.",
    }
    ok = all(item.get("supported", False) for item in providers.values())
    ok = ok and all(item["available"] for item in checks.values())
    return payload, ok


def main(argv: list[str] | None = None) -> int:
    with termination_as_interrupt():
        try:
            return _main(argv)
        except KeyboardInterrupt:
            print("Interrupted by user", file=sys.stderr)
            return 130


def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            with args.config.open("x", encoding="utf-8") as stream:
                stream.write(default_text())
            print(f"Configuration: {args.config.resolve()}")
            return 0
        if args.command == "status":
            state = json.loads((args.run_dir / "state.json").read_text(encoding="utf-8"))
            if args.summary:
                state = {
                    key: state[key]
                    for key in (
                        "status",
                        "phase",
                        "reason",
                        "complexity",
                        "cycles",
                        "agent_calls",
                        "elapsed_seconds",
                        "known_cost_usd",
                        "unknown_cost_calls",
                        "token_usage",
                        "acceptance",
                    )
                    if key in state
                }
                if "acceptance" in state:
                    state["acceptance"] = {
                        key: value for key, value in state["acceptance"].items() if key != "rounds"
                    }
                state["run_dir"] = str(args.run_dir.resolve())
                state["report"] = str(args.run_dir.resolve() / "report.md")
                if state.get("status") == "running":
                    progress = read_progress(args.run_dir)
                    if progress is not None:
                        state["progress"] = progress
            print(json.dumps(state, indent=2, ensure_ascii=False))
            return 0
        if args.command == "apply":
            changed = apply_run(args.run_dir.resolve())
            print(json.dumps({"status": "applied", "changed_paths": changed}, indent=2))
            return 0
        config = _config(args)
        if args.command == "verify":
            directory, report = verify_repository(
                args.repo,
                config,
                args.profile,
                package=args.package,
                test_filter=args.test_filter,
            )
            print(
                json.dumps(
                    {
                        "status": report["status"],
                        "reason": report["reason"],
                        "run_dir": str(directory),
                        "report": str(directory / "report.md"),
                        "apply_eligible": False,
                    },
                    indent=2,
                )
            )
            return (
                0
                if report["status"] == "passed"
                else 130
                if report["status"] == "interrupted"
                else 1
            )
        if args.command == "doctor":
            payload, ok = _doctor(config, args.repo)
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0 if ok else 1
        requirements = load_requirements(args.requirements) if args.requirements else None
        level, roles = route(config, args.task, args.complexity)
        if args.command == "plan":
            gate_plan = plan_verification(config, "gate")
            if requirements is not None:
                requirements = prepare_rust_review(requirements, config.rust_review, gate_plan)
            bound = bind_requirements(requirements, gate_plan) if requirements is not None else None
            print(
                json.dumps(
                    {
                        "config_version": config.config_version,
                        "compatibility_mode": bound is None
                        and config.config_version < 3
                        and not config.rust_review.enabled,
                        "acceptance_mode": "criteria"
                        if bound is not None
                        else "required"
                        if config.config_version >= 3 or config.rust_review.enabled
                        else "legacy",
                        "requirements_needed": bound is None
                        and (config.config_version >= 3 or config.rust_review.enabled),
                        "requirements_id": bound.requirements_id if bound else None,
                        "requirements": asdict(bound.requirements) if bound else None,
                        "rust_review": rust_review_context(config.rust_review),
                        "verification_plan_id": verification_plan_id(gate_plan),
                        "complexity": level,
                        "roles": {k: asdict(v) for k, v in roles.items()},
                        "checks": config.checks,
                        "checks_scope": _checks_scope(config),
                        "verification": {
                            **asdict(config.verification),
                            "gate_plan": asdict(gate_plan),
                        },
                        "prepare": config.prepare,
                        "max_cycles": config.max_cycles,
                        "max_agent_calls": config.max_agent_calls,
                        "max_seconds": config.max_seconds,
                        "max_output_bytes": config.max_output_bytes,
                        "context": asdict(config.context),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        workflow = Workflow(
            args.repo,
            args.task,
            config,
            args.complexity,
            requirements=requirements,
            progress=(lambda text: None)
            if args.events_jsonl
            else (lambda text: print(text, file=sys.stderr, flush=True)),
            event=(
                lambda event: print(
                    json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True
                )
            )
            if args.events_jsonl
            else None,
        )
        run_dir = workflow.run()
        print(
            json.dumps(
                {
                    "status": workflow.state["status"],
                    "reason": workflow.state["reason"],
                    "run_dir": str(run_dir),
                    "report": str(run_dir / "report.md"),
                },
                indent=2,
            )
        )
        return (
            0
            if workflow.state["status"] == "succeeded"
            else 130
            if workflow.state["status"] == "interrupted"
            else 1
        )
    except (ConfigError, RequirementsError, WorkspaceError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
