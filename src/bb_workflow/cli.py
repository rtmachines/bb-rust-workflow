"""Portable command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .backends import BackendError, probe
from .cli_output import CommandResult, emit_result, known_artifacts
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


class _ParserResponse(Exception):
    def __init__(self, message: str, command: str | None, *, help_requested: bool = False):
        super().__init__(message)
        self.command = command
        self.help_requested = help_requested


class _CommandParser(argparse.ArgumentParser):
    def __init__(self, *args, machine: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.machine = machine
        self.command: str | None = None
        self.root = self
        self.selected_command: str | None = None

    def parse_known_args(self, args=None, namespace=None):
        # Record only a parser actually selected by argparse, never arbitrary argv values.
        if self.command is not None:
            self.root.selected_command = self.command
        return super().parse_known_args(args, namespace)

    def error(self, message):
        if self.machine:
            raise _ParserResponse(message, self.root.selected_command)
        super().error(message)

    def print_help(self, file=None):
        if self.machine:
            raise _ParserResponse(
                self.format_help(), self.root.selected_command, help_requested=True
            )
        super().print_help(file)


class _InvalidState(ValueError):
    """Persisted status data is unreadable or has an unsupported shape."""


class _InvalidArguments(ValueError):
    """Command input rejected before creating a workflow."""


def _read_state(directory: Path) -> dict:
    try:
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise _InvalidState(f"Invalid state metadata: {directory / 'state.json'}") from exc
    if not isinstance(state, dict):
        raise _InvalidState("State metadata must be an object")
    for name in ("status", "phase", "reason"):
        if name in state and not isinstance(state[name], str):
            raise _InvalidState(f"State {name} must be a string")
    if "acceptance" in state and not isinstance(state["acceptance"], dict):
        raise _InvalidState("State acceptance must be an object")
    return state


def _parser(machine: bool = False) -> argparse.ArgumentParser:
    parser = _CommandParser(
        machine=machine, prog="bb-workflow", description="Bounded Rust agent workflow"
    )
    parser.add_argument("--json", action="store_true", help="Emit a versioned command envelope")
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
    for name, command in commands.choices.items():
        command.root = parser
        command.command = name
        command.machine = machine
        command.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Emit a versioned command envelope",
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


def _dispatch(args: argparse.Namespace) -> CommandResult:
    if args.command == "init":
        with args.config.open("x", encoding="utf-8") as stream:
            stream.write(default_text())
        return CommandResult(
            {"config": str(args.config.resolve())},
            0,
            text=f"Configuration: {args.config.resolve()}",
        )
    if args.command == "status":
        state = _read_state(args.run_dir)
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
        return CommandResult(state, 0)
    if args.command == "apply":
        changed = apply_run(args.run_dir.resolve())
        return CommandResult({"status": "applied", "changed_paths": changed}, 0)
    config = _config(args)
    if args.command == "verify":
        directory, report = verify_repository(
            args.repo, config, args.profile, package=args.package, test_filter=args.test_filter
        )
        return CommandResult(
            {
                "status": report["status"],
                "reason": report["reason"],
                "run_dir": str(directory),
                "report": str(directory / "report.md"),
                "apply_eligible": False,
            },
            0 if report["status"] == "passed" else 130 if report["status"] == "interrupted" else 1,
            code=report.get("error_code", report["status"]),
            message=report["reason"],
        )
    if args.command == "doctor":
        payload, ok = _doctor(config, args.repo)
        return CommandResult(
            payload, 0 if ok else 1, code="doctor_failed", message="Required metadata probes failed"
        )
    requirements = load_requirements(args.requirements) if args.requirements else None
    level, roles = route(config, args.task, args.complexity)
    if args.command == "plan":
        gate_plan = plan_verification(config, "gate")
        if requirements is not None:
            requirements = prepare_rust_review(requirements, config.rust_review, gate_plan)
        bound = bind_requirements(requirements, gate_plan) if requirements is not None else None
        return CommandResult(
            {
                "config_version": config.config_version,
                "compatibility_mode": bound is None
                and config.config_version < 3
                and (not config.rust_review.enabled),
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
                "verification": {**asdict(config.verification), "gate_plan": asdict(gate_plan)},
                "prepare": config.prepare,
                "max_cycles": config.max_cycles,
                "max_agent_calls": config.max_agent_calls,
                "max_seconds": config.max_seconds,
                "max_output_bytes": config.max_output_bytes,
                "context": asdict(config.context),
            },
            0,
        )
    if not args.repo.is_dir() or not args.task.strip():
        raise _InvalidArguments("A project directory and non-empty task are required")
    workflow = Workflow(
        args.repo,
        args.task,
        config,
        args.complexity,
        requirements=requirements,
        progress=(lambda text: None)
        if args.events_jsonl
        else lambda text: print(text, file=sys.stderr, flush=True),
        event=(
            lambda event: print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
        )
        if args.events_jsonl
        else None,
    )
    run_dir = workflow.run()
    return CommandResult(
        {
            "status": workflow.state["status"],
            "reason": workflow.state["reason"],
            "run_dir": str(run_dir),
            "report": str(run_dir / "report.md"),
        },
        0
        if workflow.state["status"] == "succeeded"
        else 130
        if workflow.state["status"] == "interrupted"
        else 1,
        code=workflow.state.get("error_code", workflow.state["status"]),
        message=workflow.state["reason"],
    )


def _exception_result(exc: Exception) -> CommandResult:
    if isinstance(exc, ConfigError):
        code = "invalid_config"
    elif isinstance(exc, RequirementsError):
        code = "invalid_requirements"
    elif isinstance(exc, WorkspaceError):
        code = exc.code
    elif isinstance(exc, BackendError):
        code = "provider_error"
    elif isinstance(exc, _InvalidState):
        code = "invalid_state"
    elif isinstance(exc, _InvalidArguments):
        code = "invalid_arguments"
    elif isinstance(exc, OSError):
        code = "io_error"
    else:
        code = "internal_error"
    return CommandResult(exit_code=2, code=code, message=str(exc) or type(exc).__name__)


def _emit_command_result(result, args, command, machine) -> int:
    directory = getattr(args, "run_dir", None)
    if result.data is not None and isinstance(result.data.get("run_dir"), str):
        directory = Path(result.data["run_dir"])
    artifacts = known_artifacts(directory) if result.exit_code else []
    return emit_result(result, command, machine=machine, artifacts=artifacts)


def _main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    options = argv[: argv.index("--")] if "--" in argv else argv
    machine = "--json" in options
    parser = _parser(machine)
    args = None
    try:
        args = parser.parse_args(argv)
        result = _dispatch(args)
        return _emit_command_result(result, args, parser.selected_command, machine)
    except _ParserResponse as response:
        result = (
            CommandResult({"help": str(response)})
            if response.help_requested
            else CommandResult(exit_code=2, code="invalid_arguments", message=str(response))
        )
    except KeyboardInterrupt:
        result = CommandResult(exit_code=130, code="interrupted", message="Interrupted by user")
        if not machine:
            print(result.message, file=sys.stderr)
    except Exception as exc:
        if not machine and not isinstance(
            exc, (ConfigError, RequirementsError, WorkspaceError, OSError, ValueError)
        ):
            raise
        result = _exception_result(exc)
        if not machine:
            print(f"Error: {exc}", file=sys.stderr)
    return _emit_command_result(result, args, parser.selected_command, machine)


def main(argv: list[str] | None = None) -> int:
    with termination_as_interrupt():
        return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
