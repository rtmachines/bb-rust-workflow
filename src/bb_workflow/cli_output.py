"""Versioned command results, separate from progress and workflow approval."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommandResult:
    data: dict | None = None
    exit_code: int = 0
    code: str | None = None
    message: str = ""
    text: str | None = None


ACTIONS = {
    "invalid_arguments": "correct_arguments",
    "invalid_config": "correct_configuration",
    "invalid_requirements": "correct_requirements",
    "requirements_required": "provide_requirements",
    "project_locked": "inspect_project_lock",
    "source_conflict": "inspect_source_conflict",
    "candidate_conflict": "inspect_candidate_conflict",
    "provider_error": "inspect_provider_logs",
    "interrupted": "inspect_interrupted_operation",
    "io_error": "inspect_filesystem",
    "invalid_state": "inspect_state",
    "workspace_error": "inspect_workspace",
    "apply_recovery_required": "recover_apply_backup",
    "doctor_failed": "inspect_doctor_results",
    "internal_error": "inspect_internal_error",
}


def known_artifacts(directory: Path | None) -> list[str]:
    """Link only known files that actually exist, including partial-run evidence."""
    if directory is None:
        return []
    artifacts = []
    for name in ("state.json", "report.md", "report.json", "apply.json", "acceptance.json"):
        path = directory / name
        try:
            if path.is_file():
                artifacts.append(str(path.resolve()))
        except (OSError, ValueError, RuntimeError):
            # Evidence discovery must not replace the command's original error.
            continue
    return artifacts


def emit_result(
    result: CommandResult,
    command: str | None,
    *,
    machine: bool,
    artifacts: list[str] | None = None,
) -> int:
    if machine:
        error = None
        if result.exit_code:
            code = result.code or "internal_error"
            error = {
                "code": code,
                "message": result.message,
                # A role retry policy says nothing about safely repeating a CLI command.
                "retryable": False,
                "next_action": ACTIONS.get(code, "inspect_run_artifacts"),
                "artifacts": artifacts or [],
            }
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "command": command,
                    "ok": result.exit_code == 0,
                    "exit_code": result.exit_code,
                    "data": result.data,
                    "error": error,
                },
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    elif result.text is not None:
        print(result.text)
    elif result.data is not None:
        print(json.dumps(result.data, indent=2, ensure_ascii=command in {"run", "verify", "apply"}))
    return result.exit_code
