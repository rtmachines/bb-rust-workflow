"""Small CLI adapters. Raw backend output is retained before it is parsed."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .process import ProcessInterrupted, run_process
from .workspace import snapshot_environment


class SelectionLike(Protocol):
    backend: str
    model: str
    effort: str | None
    skills: tuple[str, ...]
    tools: tuple[str, ...]
    mcp_config: str | None


class BackendError(RuntimeError):
    """An invocation failed or did not return usable structured output."""

    def __init__(
        self,
        message: str,
        *,
        requested_model: str | None = None,
        observed_model: str | None = None,
        retryable: bool = True,
        cost_usd: float | None = None,
        usage: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.requested_model = requested_model
        self.observed_model = observed_model
        self.retryable = retryable
        self.cost_usd = cost_usd
        self.usage = dict(usage) if usage is not None else {}


class BackendInterrupted(ProcessInterrupted):
    """Provider cancellation retaining measured invocation evidence."""

    def __init__(self, interrupted: ProcessInterrupted, audit: dict) -> None:
        super().__init__(interrupted.result, *interrupted.args)
        self.requested_model = audit["requested_model"]
        self.observed_model = audit["observed_models"][-1] if audit["observed_models"] else None
        self.retryable = False
        self.cost_usd = audit["cost_usd"]
        self.usage = dict(audit["usage"])


@dataclass(frozen=True)
class BackendResult:
    payload: dict
    cost_usd: float | None
    usage: dict
    model: str | None


_READ_ROLES = {"reviewer_a", "reviewer_b", "triage"}
_WRITE_ROLES = {"implementation", "fixer"}
_DEFAULT_OUTPUT_BYTES = 16 * 1024 * 1024
_REROUTE = re.compile(r"^model rerouted:\s*(\S+)\s*->\s*(\S+)(?:\s+\((.*?)\))?$")
_CLAUDE_FLAGS = (
    "--model",
    "--effort",
    "--output-format",
    "--json-schema",
    "--no-session-persistence",
    "--permission-mode",
    "--tools",
    "--allowedTools",
    "--strict-mcp-config",
    "--mcp-config",
    "--setting-sources",
)
_CODEX_FLAGS = (
    "--model",
    "--sandbox",
    "--skip-git-repo-check",
    "--ephemeral",
    "--json",
    "--output-schema",
    "--output-last-message",
    "--config",
    "--ignore-user-config",
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _json_object(text: str, source: str) -> dict:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BackendError(f"Invalid JSON in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise BackendError(f"{source} must contain a JSON object")
    return value


def _reported_model(data: dict, expected: str) -> str | None:
    models = []
    if data.get("model") is not None:
        models.append(data["model"])
    model_usage = data.get("modelUsage")
    if isinstance(model_usage, dict):
        models.extend(model_usage)
    for model in models:
        if model != expected:
            raise BackendError(
                f"Backend model drift: requested {expected!r}, reported {model!r}",
                requested_model=expected,
                observed_model=model if isinstance(model, str) else None,
                retryable=False,
            )
    return models[0] if models else None


def _usage(data: dict) -> dict:
    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        raise BackendError("Backend usage must be an object")
    return usage


def _cost(value: object) -> float | None:
    """Accept finite nonnegative provider measurements, including measured zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        cost = float(value)
    except OverflowError:
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


def _parse_claude(stdout: str, model: str) -> BackendResult:
    data = _json_object(stdout, "Claude stdout")
    if data.get("is_error") or data.get("type") == "error":
        raise BackendError("Claude reported an error; see stdout.txt")
    if data.get("subtype") not in (None, "success"):
        raise BackendError(f"Claude did not complete successfully: {data['subtype']}")
    payload = data.get("structured_output")
    if not isinstance(payload, dict):
        raise BackendError("Claude response has no structured_output object")
    cost = _cost(data.get("total_cost_usd"))
    if data.get("total_cost_usd") is not None and cost is None:
        raise BackendError("Claude returned an invalid cost")
    return BackendResult(
        payload,
        cost,
        _usage(data),
        _reported_model(data, model),
    )


def _reroute_message(event: dict) -> str | None:
    item = event.get("item")
    if event.get("type") != "item.completed" or not isinstance(item, dict):
        return None
    message = item.get("message")
    if item.get("type") == "error" and isinstance(message, str):
        if message.startswith("model rerouted:"):
            return message
    return None


def _invocation_evidence(stdout: str, backend: str) -> dict:
    """Retain reported measurements and models even when the invocation fails.

    Only complete JSON envelopes/events supply measurements. Missing or malformed
    telemetry remains unknown; a failed invocation need not report all its usage.
    """
    observed: list[str] = []
    reroutes: list[dict] = []
    usage: dict = {}
    cost = None
    for text in [stdout] if backend == "claude" else stdout.splitlines():
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if backend == "claude":
            cost = _cost(data.get("total_cost_usd"))
        if backend == "claude" or data.get("type") == "turn.completed":
            if isinstance(data.get("usage"), dict):
                usage = data["usage"]
        models = [data.get("model")]
        if isinstance(data.get("modelUsage"), dict):
            models.extend(data["modelUsage"])
        message = _reroute_message(data) if backend == "codex" else None
        if message:
            match = _REROUTE.fullmatch(message)
            reroutes.append(
                {
                    "message": message,
                    "from_model": match[1],
                    "to_model": match[2],
                    "reason": match[3],
                }
                if match
                else {"message": message}
            )
            if match:
                models.append(match[2])
        for value in models:
            if isinstance(value, str) and value not in observed:
                observed.append(value)
    return {"observed_models": observed, "reroutes": reroutes, "usage": usage, "cost_usd": cost}


def _parse_codex(
    stdout: str, output: Path, model: str, max_output_bytes: int = _DEFAULT_OUTPUT_BYTES
) -> BackendResult:
    usage: dict = {}
    reported = None
    completed = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = _json_object(line, "Codex JSONL stdout")
        message = _reroute_message(event)
        if message:
            match = _REROUTE.fullmatch(message)
            raise BackendError(
                f"Codex model drift: {message}",
                requested_model=model,
                observed_model=match[2] if match else None,
                retryable=False,
            )
        if event.get("type") in ("turn.failed", "error"):
            raise BackendError("Codex reported an error; see stdout.txt")
        reported = _reported_model(event, model) or reported
        if event.get("type") == "turn.completed":
            completed = True
            usage = _usage(event)
    if not completed:
        raise BackendError("Codex did not emit turn.completed")
    try:
        with output.open("rb") as stream:
            raw = stream.read(max_output_bytes + 1)
        if len(raw) > max_output_bytes:
            raise BackendError(
                "Codex final response exceeded the output byte limit", retryable=False
            )
        payload = _json_object(raw.decode("utf-8"), "Codex last-message.json")
    except (OSError, UnicodeError) as exc:
        raise BackendError("Codex did not write a readable final structured response") from exc
    return BackendResult(payload, None, usage, reported)


def _command(
    selection: SelectionLike,
    role: str,
    artifact_dir: Path,
    schema: dict,
    executable: str,
) -> list[str]:
    if role not in _READ_ROLES | _WRITE_ROLES:
        raise BackendError(f"Unknown workflow role: {role}", retryable=False)
    if any(
        tool != "LSP" and not re.fullmatch(r"mcp__[\w-]+__[\w-]+", tool) for tool in selection.tools
    ):
        raise BackendError("Only LSP or exact MCP tool names may be configured", retryable=False)
    mcp_tools = [tool for tool in selection.tools if tool.startswith("mcp__")]
    if mcp_tools and not selection.mcp_config:
        raise BackendError("Explicit MCP tools require mcp_config", retryable=False)
    if selection.backend == "claude":
        tools = ["Read", "Glob", "Grep"]
        if role in _WRITE_ROLES:
            tools.extend(["Edit", "Write"])
        tools.extend(tool for tool in selection.tools if tool == "LSP" and tool not in tools)
        tool_list = ",".join(tools)
        allowed_list = ",".join([*tools, *mcp_tools])
        argv = [
            executable,
            "-p",
            "--model",
            selection.model,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--tools",
            tool_list,
            "--allowedTools",
            allowed_list,
            "--strict-mcp-config",
            "--setting-sources",
            "",
        ]
        if selection.effort:
            argv.extend(["--effort", selection.effort])
        if selection.mcp_config:
            argv.extend(["--mcp-config", selection.mcp_config])
        return argv
    if selection.backend == "codex":
        if selection.tools or selection.mcp_config:
            raise BackendError(
                "Configured tools and mcp_config are supported only for Claude in this version",
                retryable=False,
            )
        argv = [
            executable,
            "exec",
            "--model",
            selection.model,
            "-c",
            'approval_policy="never"',
            "--sandbox",
            "read-only" if role in _READ_ROLES else "workspace-write",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--json",
            "--output-schema",
            str(artifact_dir / "schema.json"),
            "-o",
            str(artifact_dir / "last-message.json"),
        ]
        if selection.effort:
            argv.extend(["-c", f"model_reasoning_effort={json.dumps(selection.effort)}"])
        return argv + ["-"]
    raise BackendError(f"Unsupported backend: {selection.backend}", retryable=False)


def invoke(
    selection: SelectionLike,
    role: str,
    prompt: str,
    cwd: Path,
    artifact_dir: Path,
    timeout: float,
    schema: dict,
    executable: str | None = None,
    *,
    max_output_bytes: int = _DEFAULT_OUTPUT_BYTES,
) -> BackendResult:
    """Run exactly one fresh CLI session and retain its audit artifacts."""
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("stdout.txt", "stderr.txt"):
        (artifact_dir / filename).write_text("", encoding="utf-8")
    for filename in ("response.json", "last-message.json"):
        (artifact_dir / filename).unlink(missing_ok=True)
    _write_json(artifact_dir / "schema.json", schema)
    audit = {
        "requested_model": selection.model,
        "observed_models": [],
        "reroutes": [],
        "usage": {},
        "cost_usd": None,
        "status": "failed",
    }
    try:
        argv = _command(selection, role, artifact_dir, schema, executable or selection.backend)
        _write_json(
            artifact_dir / "command.json",
            {
                "argv": argv,
                "cwd": str(cwd.resolve()),
                "role": role,
                "backend": selection.backend,
                "model": selection.model,
                "timeout": timeout,
                "max_output_bytes": max_output_bytes,
            },
        )
        interrupted = None
        try:
            result = run_process(
                argv,
                cwd=cwd,
                timeout=timeout,
                input_text=prompt,
                env=snapshot_environment(),
                max_output_bytes=max_output_bytes,
            )
        except ProcessInterrupted as exc:
            interrupted = exc
            result = exc.result
        except OSError as exc:
            raise BackendError(f"Cannot start {selection.backend}: {exc}", retryable=False) from exc
        (artifact_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8", newline="")
        (artifact_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8", newline="")
        audit.update(_invocation_evidence(result.stdout, selection.backend))
        if interrupted is not None:
            audit.update(status="interrupted", error="Interrupted by user", retryable=False)
            _write_json(artifact_dir / "invocation.json", audit)
            raise BackendInterrupted(interrupted, audit) from interrupted
        if result.output_limit_exceeded:
            raise BackendError(
                f"{selection.backend} exceeded the output byte limit", retryable=False
            )
        if result.timed_out:
            raise BackendError(f"{selection.backend} timed out after {timeout:g} seconds")
        if result.returncode:
            raise BackendError(
                f"{selection.backend} exited with status {result.returncode}; see stderr.txt"
            )
        parsed = (
            _parse_claude(result.stdout, selection.model)
            if selection.backend == "claude"
            else _parse_codex(
                result.stdout,
                artifact_dir / "last-message.json",
                selection.model,
                max(
                    0,
                    max_output_bytes
                    - len(result.stdout.encode("utf-8"))
                    - len(result.stderr.encode("utf-8")),
                ),
            )
        )
        _write_json(artifact_dir / "response.json", parsed.payload)
        audit["status"] = "completed"
        _write_json(artifact_dir / "invocation.json", audit)
        return parsed
    except BackendError as exc:
        exc.requested_model = selection.model
        observed = audit["observed_models"]
        if exc.observed_model is None and observed:
            exc.observed_model = observed[-1]
        if audit["reroutes"] or any(model != selection.model for model in observed):
            exc.retryable = False
        exc.usage = audit["usage"]
        exc.cost_usd = audit["cost_usd"]
        audit["error"] = str(exc)
        audit["retryable"] = exc.retryable
        _write_json(artifact_dir / "invocation.json", audit)
        _write_json(
            artifact_dir / "response.json",
            {
                "error": str(exc),
                "requested_model": selection.model,
                "observed_model": exc.observed_model,
            },
        )
        raise


def probe(
    executable: str,
    backend: str,
    cwd: Path | None = None,
    *,
    max_output_bytes: int = _DEFAULT_OUTPUT_BYTES,
) -> dict:
    """Inspect version/help without starting a model session or printing auth."""
    if backend not in ("claude", "codex"):
        raise ValueError(f"Unsupported backend: {backend}")
    report = {
        "backend": backend,
        "executable": executable,
        "available": False,
        "version": None,
        "supported": False,
        "missing_flags": [],
        "required_flags": {},
        "error": None,
    }
    cwd = (cwd if cwd is not None else Path.cwd()).resolve()
    environment = snapshot_environment()
    try:
        version = run_process(
            [executable, "--version"],
            cwd=cwd,
            timeout=10,
            env=environment,
            max_output_bytes=max_output_bytes,
        )
        if version.output_limit_exceeded:
            report["error"] = "Version check exceeded the output byte limit"
            return report
        if version.returncode or version.timed_out:
            report["error"] = "Version check failed or timed out"
            return report
        report["available"] = True
        report["version"] = version.stdout.strip()
        help_command = (
            [executable, "exec", "--help"] if backend == "codex" else [executable, "--help"]
        )
        help_result = run_process(
            help_command,
            cwd=cwd,
            timeout=10,
            env=environment,
            max_output_bytes=max_output_bytes,
        )
        if help_result.output_limit_exceeded:
            report["error"] = "Capability check exceeded the output byte limit"
            return report
        if help_result.returncode or help_result.timed_out:
            report["error"] = "Capability check failed or timed out"
            return report
        required = _CODEX_FLAGS if backend == "codex" else _CLAUDE_FLAGS
        report["required_flags"] = {flag: flag in help_result.stdout for flag in required}
        report["missing_flags"] = [flag for flag in required if flag not in help_result.stdout]
        report["supported"] = not report["missing_flags"]
    except OSError as exc:
        report["error"] = str(exc)
    return report
