"""Verification plans and shared, bounded command execution without providers."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Config, ConfigError, VerificationVariant
from .diagnostics import (
    DiagnosticContext,
    compare_diagnostics,
    parse_diagnostics,
    summarize_diagnostics,
)
from .process import ProcessInterrupted, ProcessResult, run_process
from .verification_metadata import MetadataCapture, MetadataError
from .workspace import (
    WorkspaceError,
    manifest,
    project_lock,
    snapshot_environment,
    snapshot_id,
    write_json,
)


@dataclass(frozen=True)
class PlannedCheck:
    id: str
    profile: str
    variant: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class VerificationPlan:
    profile: str
    variants: tuple[str, ...]
    package: str | None
    test_filter: str | None
    checks: tuple[PlannedCheck, ...]


def _cargo_command(
    command: list[str] | tuple[str, ...],
    variant: VerificationVariant,
    package: str | None,
    test_filter: str | None,
) -> tuple[str, ...]:
    transformed = package is not None or test_filter is not None or variant != VerificationVariant()
    if not transformed:
        return tuple(command)
    if (
        len(command) < 2
        or command[0] != "cargo"
        or command[1] not in {"fmt", "check", "test", "clippy"}
    ):
        raise ConfigError(f"Cannot transform custom command unambiguously: {list(command)!r}")
    subcommand = command[1]
    args = list(command[2:])
    separator = args.index("--") if "--" in args else len(args)
    flags, trailing = args[:separator], args[separator:]
    # Only the simple, flag-only Cargo grammar used by the built-in checks is transformed.
    allowed = (
        {"--all", "--check"}
        if subcommand == "fmt"
        else {
            "--workspace",
            "--all-targets",
            "--locked",
            "--doc",
            "--offline",
            "--release",
            "--quiet",
        }
    )
    if any(flag not in allowed for flag in flags):
        raise ConfigError(
            f"Cannot transform conflicting or ambiguous Cargo arguments: {list(command)!r}"
        )
    if (
        subcommand == "test"
        and test_filter is not None
        and any(
            flag not in {"--nocapture", "--show-output", "--exact", "--quiet"}
            for flag in trailing[1:]
        )
    ):
        raise ConfigError(
            f"Cannot combine test-filter with existing harness filters: {list(command)!r}"
        )
    if package is not None:
        flags = [
            flag for flag in flags if flag != ("--all" if subcommand == "fmt" else "--workspace")
        ]
        flags += ["--package", package]
    if subcommand != "fmt":
        if variant.features:
            flags += ["--features", ",".join(variant.features)]
        if variant.all_features:
            flags += ["--all-features"]
        if variant.no_default_features:
            flags += ["--no-default-features"]
        if variant.target:
            flags += ["--target", variant.target]
        if test_filter is not None and subcommand == "test":
            flags.append(test_filter)
    toolchain = [f"+{variant.toolchain}"] if variant.toolchain else []
    return tuple(["cargo", *toolchain, subcommand, *flags, *trailing])


def plan_verification(
    config: Config,
    profile: str = "gate",
    *,
    package: str | None = None,
    test_filter: str | None = None,
) -> VerificationPlan:
    """Resolve commands now, retaining Config.checks as the sole gate command source."""
    if profile not in config.verification.profiles:
        raise ConfigError(f"Unknown verification profile: {profile}")
    if profile == "gate" and (package is not None or test_filter is not None):
        raise ConfigError("gate does not permit package or test-filter narrowing")
    if test_filter is not None and profile != "focused":
        raise ConfigError("test-filter is only supported by focused")
    for value, name in ((package, "package"), (test_filter, "test-filter")):
        if value is not None and (
            not isinstance(value, str) or not value.strip() or value.startswith("-")
        ):
            raise ConfigError(f"{name} must be non-empty and cannot start with '-'")
    settings = config.verification.profiles[profile]
    if not settings.variants or len(set(settings.variants)) != len(settings.variants):
        raise ConfigError(f"{profile}.variants must be non-empty and unique")
    if profile == "gate" and settings.checks is not None:
        raise ConfigError("gate cannot override Config.checks")
    commands = config.checks if profile == "gate" else settings.checks
    if not commands:
        raise ConfigError(f"{profile} requires non-empty checks")
    checks = []
    for name in settings.variants:
        if name not in config.verification.variants:
            raise ConfigError(f"Undefined verification variant: {name}")
        variant = config.verification.variants[name]
        for command in commands:
            argv = _cargo_command(command, variant, package, test_filter)
            checks.append(PlannedCheck(str(len(checks)), profile, name, argv))
    if test_filter is not None and not any(
        c.argv[1 + bool(config.verification.variants[c.variant].toolchain)] == "test"
        for c in checks
    ):
        raise ConfigError("focused test-filter requires a Cargo test command")
    return VerificationPlan(profile, settings.variants, package, test_filter, tuple(checks))


class VerificationStop(RuntimeError):
    """Fatal execution failure with a status shared by both callers."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status


def execute_checks(
    checks: tuple[PlannedCheck, ...],
    *,
    cwd: Path,
    artifact_root: Path,
    config: Config,
    timeout: Callable[[], float],
    command_root: Path | None = None,
    record: Callable[[dict], None] | None = None,
    expected: dict[str, str] | None = None,
    preparing: bool = False,
    process_fn: Callable = run_process,
    baseline_records: list[dict] | None = None,
    scope: dict | None = None,
    on_start: Callable[[PlannedCheck], None] | None = None,
) -> list[dict]:
    """Execute a precomputed plan, retaining full logs and one frozen source manifest.

    Ordinary command failures retain the remaining evidence. Launch errors, output
    overflow and source changes abort; interruption is recorded before propagation.
    Preparation deliberately permits mutations. The caller owns its global clock.
    """
    frozen = expected if expected is not None else manifest(cwd, config.exclude)
    results = []
    command_root = command_root or artifact_root / "checks"
    capture = MetadataCapture(
        cwd=cwd,
        artifact_root=artifact_root,
        command_root=command_root,
        config=config,
        timeout=timeout,
        process_fn=process_fn,
        frozen=frozen,
    )
    previous = {(item.get("id"), item.get("variant")): item for item in baseline_records or []}
    for index, check in enumerate(checks):
        before = manifest(cwd, config.exclude)
        if not preparing and before != frozen:
            raise VerificationStop("failed", "Candidate changed between validation commands")
        timeout()
        directory = command_root / str(index)
        directory.mkdir(parents=True)
        argv = list(check.argv)
        write_json(directory / "command.json", argv)
        started = time.monotonic()
        fatal: BaseException | None = None
        result = None
        command_started = False
        metadata = {"tool_versions": {}, "digest": None}
        environment = snapshot_environment()
        if on_start is not None:
            on_start(check)
        try:
            if not preparing:
                metadata = capture.capture(check.argv, environment, directory)
            command_started = True
            result = process_fn(
                argv,
                cwd=cwd,
                timeout=timeout(),
                env=environment,
                max_output_bytes=config.max_output_bytes,
            )
        except MetadataError as exc:
            fatal = VerificationStop("failed", str(exc))
        except VerificationStop as exc:
            fatal = exc
        except FileNotFoundError:
            fatal = VerificationStop("blocked", f"Validation executable unavailable: {argv[0]}")
        except OSError as exc:
            fatal = VerificationStop("failed", f"Cannot execute validation command: {exc}")
        except ProcessInterrupted as exc:
            fatal = exc
            # Probe output belongs to its metadata artifacts, not an unstarted check.
            if command_started:
                result = exc.result
        except KeyboardInterrupt as exc:
            fatal = exc
        if result is None:
            result = ProcessResult(-1, "", "", time.monotonic() - started)
        (directory / "stdout.txt").write_text(result.stdout, encoding="utf-8", newline="")
        (directory / "stderr.txt").write_text(result.stderr, encoding="utf-8", newline="")
        interrupted = isinstance(fatal, KeyboardInterrupt)
        after = None
        if not interrupted:
            try:
                after = manifest(cwd, config.exclude)
            except (OSError, WorkspaceError) as exc:
                fatal = VerificationStop("failed", f"Cannot inspect source after validation: {exc}")
        unchanged = None if interrupted else before == after
        if result.output_limit_exceeded and not interrupted:
            fatal = VerificationStop(
                "failed",
                f"Command output limit exceeded ({config.max_output_bytes} bytes): "
                + " ".join(argv),
            )
        if not preparing and after is not None and (not unchanged or after != frozen):
            # Keep interruption visible, but never mark its mutated result as valid.
            if not isinstance(fatal, KeyboardInterrupt):
                fatal = VerificationStop(
                    "failed",
                    "Validation command changed source files; results invalidated. "
                    "Move generators/formatters to prepare: " + " ".join(argv),
                )
        tail_chars = max(12000, config.context.failed_check_tail_chars)
        item = {
            "id": check.id,
            "profile": check.profile,
            "variant": check.variant,
            "kind": "preparation" if preparing else "validation",
            "snapshot_id": snapshot_id(before),
            "result_snapshot_id": snapshot_id(after) if after is not None else None,
            "source_unchanged": unchanged,
            "command": argv,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "output_limit_exceeded": result.output_limit_exceeded,
            "duration": result.duration,
            "interrupted": interrupted,
            "passed": fatal is None
            and result.returncode == 0
            and not result.timed_out
            and not result.output_limit_exceeded
            and (preparing or unchanged),
            "stdout_tail": result.stdout[-tail_chars:],
            "stderr_tail": result.stderr[-tail_chars:],
            "stdout_chars": len(result.stdout),
            "stderr_chars": len(result.stderr),
            "artifacts": str(directory.relative_to(artifact_root)),
        }
        if fatal is not None:
            item["error"] = (
                "Interrupted by user" if isinstance(fatal, KeyboardInterrupt) else str(fatal)
            )
        elif result.timed_out:
            item["error"] = "Command timed out"
        elif result.returncode:
            item["error"] = f"Command exited with status {result.returncode}"
        if not preparing:
            if "reasons" in metadata and not interrupted:
                capture.recheck(metadata, environment, directory)
            context = DiagnosticContext(
                check.profile,
                check.variant,
                check.argv,
                scope if scope is not None else {"package": None, "test_filter": None},
                metadata["tool_versions"],
                metadata["digest"],
            )
            diagnostics = parse_diagnostics(
                result.stdout, result.stderr, project_root=cwd, context=context, execution=item
            )
            baseline = load_diagnostics(previous.get((check.id, check.variant)), artifact_root)
            diagnostics["comparison"] = compare_diagnostics(diagnostics, baseline)
            write_json(directory / "diagnostics.json", diagnostics)
            item.update(
                diagnostics_file=str((directory / "diagnostics.json").relative_to(artifact_root)),
                metadata_file=str((directory / "metadata.json").relative_to(artifact_root)),
                diagnostics=summarize_diagnostics(diagnostics),
            )
        results.append(item)
        if record is not None:
            record(item)
        if fatal is not None:
            raise fatal
    if not preparing and manifest(cwd, config.exclude) != frozen:
        raise VerificationStop("failed", "Candidate changed after verification")
    timeout()
    return results


def load_diagnostics(item: dict | None, artifact_root: Path) -> dict | None:
    """Load retained parser evidence; older audit records have no comparable evidence."""
    if not item or not item.get("diagnostics_file"):
        return None
    try:
        value = json.loads((artifact_root / item["diagnostics_file"]).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def aggregate_diagnostics(
    results: list[dict],
    baseline_records: list[dict],
    *,
    checks: tuple[PlannedCheck, ...],
    artifact_root: Path,
    project_root: Path,
) -> dict:
    """Retain every check's comparison, including baseline checks never reached."""
    kinds = ("new", "persistent", "resolved", "unclassified", "unobserved_previous")
    aggregate = {
        "schema_version": 1,
        "comparable": True,
        "reasons": [],
        **{kind: [] for kind in kinds},
    }
    current = {(item.get("id"), item.get("variant")): item for item in results}
    previous = {(item.get("id"), item.get("variant")): item for item in baseline_records}
    planned = {(check.id, check.variant): check for check in checks}
    for key in sorted(current.keys() | previous.keys(), key=repr):
        item = current.get(key)
        report = load_diagnostics(item, artifact_root)
        if report is not None:
            comparison = report["comparison"]
        else:
            check = planned.get(key)
            old = load_diagnostics(previous.get(key), artifact_root)
            context = DiagnosticContext(
                check.profile if check else "unknown",
                key[1] or "unknown",
                check.argv if check else (),
                {"package": None, "test_filter": None},
                {},
                None,
            )
            empty = parse_diagnostics(
                "", "", project_root=project_root, context=context, execution={}
            )
            comparison = compare_diagnostics(empty, old)
        aggregate["comparable"] &= comparison["comparable"]
        aggregate["reasons"].extend(comparison["reasons"])
        for kind in kinds:
            aggregate[kind].extend(
                dict(finding, check_id=key[0], variant=key[1]) for finding in comparison[kind]
            )
    aggregate["reasons"] = sorted(set(aggregate["reasons"]))
    aggregate["counts"] = {kind: len(aggregate[kind]) for kind in kinds}
    return aggregate


def verify_repository(
    repo: Path,
    config: Config,
    profile: str,
    *,
    package: str | None = None,
    test_filter: str | None = None,
    process_fn: Callable = run_process,
) -> tuple[Path, dict]:
    """Verify the actual project under its lock, without preparation or apply authority."""
    plan = plan_verification(config, profile, package=package, test_filter=test_filter)
    repo = repo.resolve()
    if not repo.is_dir():
        raise ConfigError("A project directory is required")
    started = time.monotonic()
    directory = repo / ".bb-workflow" / "verifications" / uuid.uuid4().hex
    report = {
        "version": 1,
        "status": "running",
        "repo": str(repo),
        "run_dir": str(directory),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "plan": asdict(plan),
        "apply_eligible": False,
        "results": [],
        "reason": "",
        "exclude": list(config.exclude),
        "limits": {
            "timeout_seconds": config.timeout_seconds,
            "max_seconds": config.max_seconds,
            "max_output_bytes": config.max_output_bytes,
        },
    }

    def timeout() -> float:
        remaining = config.max_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise VerificationStop("budget_exhausted", "Total time budget exhausted")
        return min(config.timeout_seconds, remaining)

    def save_record(item: dict) -> None:
        report["results"].append(item)
        write_json(directory / "report.json", report)

    with project_lock(repo):
        directory.mkdir(parents=True)
        frozen = None
        try:
            frozen = manifest(repo, config.exclude)
            report["snapshot_id"] = snapshot_id(frozen)
            execute_checks(
                plan.checks,
                cwd=repo,
                artifact_root=directory,
                config=config,
                timeout=timeout,
                record=save_record,
                expected=frozen,
                process_fn=process_fn,
                scope={"package": package, "test_filter": test_filter},
            )
            passed = all(item["passed"] for item in report["results"])
            report.update(
                status="passed" if passed else "failed",
                reason="Checks passed" if passed else "Verification commands failed",
            )
        except VerificationStop as exc:
            report.update(status=exc.status, reason=str(exc))
        except KeyboardInterrupt:
            report.update(status="interrupted", reason="Interrupted by user")
        except (OSError, WorkspaceError) as exc:
            report.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        finally:
            if report["status"] == "interrupted":
                report.update(result_snapshot_id=None, source_unchanged=None)
            else:
                try:
                    current = manifest(repo, config.exclude)
                    report["result_snapshot_id"] = snapshot_id(current)
                    report["source_unchanged"] = frozen is not None and current == frozen
                except (OSError, WorkspaceError) as exc:
                    report.update(source_unchanged=False, source_error=str(exc))
            if not report["source_unchanged"] and report["status"] == "passed":
                report.update(status="failed", reason="Source changed after verification")
            if report["status"] == "passed":
                try:
                    timeout()
                except VerificationStop as exc:
                    report.update(status=exc.status, reason=str(exc))
            aggregate = aggregate_diagnostics(
                report["results"],
                [],
                checks=plan.checks,
                artifact_root=directory,
                project_root=repo,
            )
            write_json(directory / "diagnostics.json", aggregate)
            report["diagnostics"] = {
                **summarize_diagnostics({"comparison": aggregate}),
                "file": "diagnostics.json",
            }
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            write_json(directory / "report.json", report)
            _write_report(directory, report)
    return directory, report


def _write_report(directory: Path, report: dict) -> None:
    lines = [
        "# Verification",
        "",
        f"Status: {report['status']}",
        f"Reason: {report['reason']}",
        f"Profile: {report['plan']['profile']}",
        f"Package: {report['plan']['package']!r}",
        f"Test filter: {report['plan']['test_filter']!r}",
        f"Variants: {', '.join(report['plan']['variants'])}",
        f"Source unchanged: {report['source_unchanged']}",
        "Apply eligible: false",
        "",
        "Full scope, limits, snapshots and results: report.json",
        "",
    ]
    for item in report["results"]:
        lines.append(
            f"- {item['id']} [{item['variant']}]: {'passed' if item['passed'] else 'failed'} — {json.dumps(item['command'])}"
        )
        if item.get("error"):
            lines.append(f"  Error: {item['error']}")
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
