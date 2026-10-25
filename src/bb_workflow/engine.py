"""A bounded, auditable state machine; backend output never controls stage order."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .backends import BackendError, BackendInterrupted, invoke
from .config import Config, Selection, route
from .process import ProcessInterrupted, run_process
from .protocol import RESPONSE_SCHEMA, merge_findings, resolve_triage, validate_response
from .usage import normalize_usage, summarize_usage
from .workspace import (
    copy_snapshot,
    initialize_git_snapshot,
    manifest,
    project_lock,
    snapshot_environment,
    snapshot_id,
    write_json,
)


class WorkflowStop(RuntimeError):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status


class Workflow:
    def __init__(
        self,
        repo: Path,
        task: str,
        config: Config,
        complexity: str = "auto",
        *,
        invoke_fn: Callable = invoke,
        process_fn: Callable = run_process,
        progress: Callable[[str], None] = print,
    ):
        self.repo = repo.resolve()
        self.task = task
        self.config = config
        self.level, self.selections = route(config, task, complexity)
        self.invoke = invoke_fn
        self.process = process_fn
        self.progress = progress
        self.started = time.monotonic()
        self.run_dir = self.repo / ".bb-workflow" / "runs" / uuid.uuid4().hex
        self.work = self.run_dir / "workspace"
        self.state = {
            "version": 1,
            "status": "running",
            "phase": "preparing",
            "repo": str(self.repo),
            "run_dir": str(self.run_dir),
            "task": task,
            "complexity": self.level,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "exclude": list(config.exclude),
            "config": asdict(config),
            "routing": {k: asdict(v) for k, v in self.selections.items()},
            "cycles": 0,
            "agent_calls": 0,
            "agents": [],
            "checks": [],
            "reviews": [],
            "known_cost_usd": 0.0,
            "unknown_cost_calls": 0,
            "reason": "",
        }

    def _save(self, phase: str | None = None) -> None:
        if phase:
            self.state["phase"] = phase
            self.progress(phase)
        self.state["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.state["known_cost_usd"] = sum(
            (item["cost_usd"] for item in self.state["agents"] if item.get("cost_usd") is not None),
            0.0,
        )
        self.state["unknown_cost_calls"] = sum(
            item.get("cost_usd") is None for item in self.state["agents"]
        )
        self.state["token_usage"] = summarize_usage(self.state["agents"])
        write_json(self.run_dir / "state.json", self.state)

    def _timeout(self) -> float:
        remaining = self.config.max_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise WorkflowStop("budget_exhausted", "Total time budget exhausted")
        return min(self.config.timeout_seconds, remaining)

    def run(self) -> Path:
        if not self.repo.is_dir() or not self.task.strip():
            raise ValueError("A project directory and non-empty task are required")
        with project_lock(self.repo):
            self.run_dir.mkdir(parents=True)
            self._save()
            try:
                self._execute()
            except WorkflowStop as exc:
                self.state.update(status=exc.status, reason=str(exc))
            except KeyboardInterrupt:
                self.state.update(status="interrupted", reason="Interrupted by user")
            except Exception as exc:
                self.state.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
            finally:
                for record in self.state["agents"]:
                    if record["status"] in {"running", "returned"}:
                        record.update(status=self.state["status"], error=self.state["reason"])
                self.state["finished_at"] = datetime.now(timezone.utc).isoformat()
                self._save("finished")
                self._report()
        return self.run_dir

    def _execute(self) -> None:
        self._timeout()
        self.baseline = copy_snapshot(self.repo, self.work, self.config.exclude)
        write_json(self.run_dir / "baseline_manifest.json", self.baseline)
        self.state["baseline_id"] = snapshot_id(self.baseline)
        # Fail before any provider call, including assets needed by later roles.
        # Per-call checks still catch assets removed by implementation or a fixer.
        for role in self.selections:
            self._selection(role)
            self._skill_paths(role, self.work)
        copy_snapshot(self.work, self.run_dir / "baseline", self.config.exclude)
        initialize_git_snapshot(self.work, timeout=self._timeout())
        initialize_git_snapshot(self.run_dir / "baseline", timeout=self._timeout())
        self._timeout()
        # Baseline failures remain visible, but never waive the final checks.
        self.state["baseline_preparation"] = self._checks("baseline-prepare", preparing=True)
        self.state["baseline_checks"] = self._checks("baseline")
        self._agent(
            "implementation",
            self.work,
            {
                "baseline_checks": self.state["baseline_checks"],
                "baseline_preparation": self.state["baseline_preparation"],
            },
        )
        while True:
            self._timeout()
            preparation = self._checks(f"candidate-{self.state['cycles']}-prepare", preparing=True)
            if not all(item["passed"] for item in preparation):
                self._fix(
                    {"failed_preparation": [item for item in preparation if not item["passed"]]}
                )
                continue
            candidate = manifest(self.work, self.config.exclude)
            candidate_id = snapshot_id(candidate)
            checks = self._checks(f"candidate-{self.state['cycles']}", expected=candidate)
            if not all(item["passed"] for item in checks):
                self._fix({"failed_checks": [item for item in checks if not item["passed"]]})
                continue
            changed = sorted(
                k
                for k in self.baseline.keys() | candidate.keys()
                if self.baseline.get(k) != candidate.get(k)
            )
            shared = {
                "checks": checks,
                "snapshot_id": candidate_id,
                "baseline_directory": str(self.run_dir / "baseline"),
                "changed_paths": changed,
                "diff": self._diff(changed),
            }
            reviews = []
            # Separate processes and separate copies. No preceding review is in shared context.
            for role in ("reviewer_a", "reviewer_b"):
                directory = self.run_dir / f"round-{self.state['cycles']}-{role}"
                frozen = copy_snapshot(self.work, directory, self.config.exclude)
                if frozen != candidate:
                    raise WorkflowStop("failed", "Candidate changed while preparing reviews")
                initialize_git_snapshot(directory, timeout=self._timeout())
                reviews.append(self._agent(role, directory, shared, frozen))
            if manifest(self.work, self.config.exclude) != candidate:
                raise WorkflowStop("failed", "Candidate changed during review")
            findings = merge_findings(reviews)
            record = {"snapshot_id": candidate_id, "reviews": reviews, "findings": findings}
            self.state["reviews"].append(record)
            confirmed, unresolved = [], []
            if findings:
                directory = self.run_dir / f"round-{self.state['cycles']}-triage"
                frozen = copy_snapshot(self.work, directory, self.config.exclude)
                if frozen != candidate:
                    raise WorkflowStop("failed", "Candidate changed while preparing triage")
                initialize_git_snapshot(directory, timeout=self._timeout())
                triage = self._agent("triage", directory, {**shared, "findings": findings}, frozen)
                confirmed, unresolved = resolve_triage(findings, triage)
                record["triage"] = triage
            record.update(confirmed=confirmed, unresolved=unresolved)
            self._save()
            if unresolved:
                raise WorkflowStop("blocked", "Triage left unresolved findings")
            blocking = [
                item for item in confirmed if item["severity"] in self.config.blocking_severities
            ]
            if blocking:
                self._fix({"confirmed_findings": blocking})
                continue
            if manifest(self.work, self.config.exclude) != candidate:
                raise WorkflowStop("failed", "Candidate changed after verification")
            self._timeout()
            write_json(self.run_dir / "candidate_manifest.json", candidate)
            self.state.update(
                status="succeeded",
                reason="Checks passed; no confirmed blocking findings",
                candidate_id=candidate_id,
                changed_paths=changed,
                remaining_findings=confirmed,
            )
            return

    def _fix(self, context: dict) -> None:
        if self.state["cycles"] >= self.config.max_cycles:
            raise WorkflowStop("limit_exhausted", "Maximum number of Fixer invocations reached")
        before = manifest(self.work, self.config.exclude)
        history = self.state.setdefault("fix_snapshots", [snapshot_id(before)])
        self._agent(
            "fixer",
            self.work,
            {**context, "baseline_directory": str(self.run_dir / "baseline")},
        )
        after = manifest(self.work, self.config.exclude)
        if before == after:
            raise WorkflowStop("no_progress", "Fixer made no source changes")
        signature = snapshot_id(after)
        if signature in history:
            raise WorkflowStop("no_progress", "Fixer returned to an earlier candidate")
        history.append(signature)
        self._save()

    def _checks(
        self, label: str, *, preparing: bool = False, expected: dict[str, str] | None = None
    ) -> list[dict]:
        commands = self.config.prepare if preparing else self.config.checks
        if not commands:
            return []
        self._save(f"checks:{label}")
        results = []
        frozen = expected if expected is not None else manifest(self.work, self.config.exclude)
        for index, argv in enumerate(commands):
            before = manifest(self.work, self.config.exclude)
            if not preparing and before != frozen:
                raise WorkflowStop("failed", "Candidate changed between validation commands")
            directory = self.run_dir / "checks" / label / str(index)
            directory.mkdir(parents=True)
            write_json(directory / "command.json", argv)
            interrupted = None
            try:
                result = self.process(
                    argv,
                    cwd=self.work,
                    timeout=self._timeout(),
                    env=snapshot_environment(),
                    max_output_bytes=self.config.max_output_bytes,
                )
            except ProcessInterrupted as exc:
                interrupted = exc
                result = exc.result
            except FileNotFoundError as exc:
                raise WorkflowStop(
                    "blocked", f"Validation executable unavailable: {argv[0]}"
                ) from exc
            (directory / "stdout.txt").write_text(result.stdout, encoding="utf-8")
            (directory / "stderr.txt").write_text(result.stderr, encoding="utf-8")
            after = manifest(self.work, self.config.exclude) if interrupted is None else None
            unchanged = before == after if after is not None else None
            record = {
                "kind": "preparation" if preparing else "validation",
                "snapshot_id": snapshot_id(before),
                "result_snapshot_id": snapshot_id(after) if after is not None else None,
                "source_unchanged": unchanged,
                "command": argv,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "output_limit_exceeded": result.output_limit_exceeded,
                "duration": result.duration,
                "interrupted": interrupted is not None,
                "passed": interrupted is None
                and result.returncode == 0
                and not result.timed_out
                and not result.output_limit_exceeded
                and (preparing or unchanged),
                "stdout_tail": result.stdout[
                    -max(12000, self.config.context.failed_check_tail_chars) :
                ],
                "stderr_tail": result.stderr[
                    -max(12000, self.config.context.failed_check_tail_chars) :
                ],
                "stdout_chars": len(result.stdout),
                "stderr_chars": len(result.stderr),
                "artifacts": str(directory.relative_to(self.run_dir)),
            }
            results.append(record)
            self.state["checks"].append({"phase": label, **record})
            self._save()
            if interrupted is not None:
                raise interrupted
            if result.output_limit_exceeded:
                raise WorkflowStop(
                    "failed",
                    f"Command output limit exceeded ({self.config.max_output_bytes} bytes): "
                    + " ".join(argv),
                )
            if not preparing and not unchanged:
                raise WorkflowStop(
                    "failed",
                    "Validation command changed source files; results invalidated. "
                    "Move generators/formatters to prepare: " + " ".join(argv),
                )
        return results

    def _selection(self, role: str) -> Selection:
        """Resolve and check external MCP assets before dispatching a role."""
        selection = self.selections[role]
        if selection.mcp_config:
            mcp_path = Path(selection.mcp_config)
            if not mcp_path.is_absolute():
                mcp_path = self.repo / mcp_path
            if not mcp_path.is_file():
                raise WorkflowStop("blocked", f"{role}: MCP configuration missing: {mcp_path}")
            selection = replace(selection, mcp_config=str(mcp_path.resolve()))
        return selection

    def _skill_paths(self, role: str, directory: Path) -> list[tuple[str, Path]]:
        paths = []
        for name in self.selections[role].skills:
            path = (directory / name).resolve()
            if not path.is_relative_to(directory.resolve()) or not path.is_file():
                raise WorkflowStop("blocked", f"{role}: Skill must exist inside project: {name}")
            paths.append((name, path))
        return paths

    def _agent(self, role: str, directory: Path, context: dict, frozen: dict | None = None) -> dict:
        selection = self._selection(role)
        prompt = self._prompt(role, directory, context)
        attempts = 1 + (self.config.max_retries if frozen is not None else 0)
        for attempt in range(attempts):
            if self.state["agent_calls"] >= self.config.max_agent_calls:
                raise WorkflowStop("budget_exhausted", "Agent invocation budget exhausted")
            self._timeout()
            self.state["agent_calls"] += 1
            if role == "fixer":
                self.state["cycles"] += 1
            artifact = self.run_dir / "agents" / f"{self.state['agent_calls']:03d}-{role}"
            artifact.mkdir(parents=True)
            (artifact / "prompt.txt").write_text(prompt, encoding="utf-8")
            record = {
                "role": role,
                "requested_model": selection.model,
                "selection": asdict(selection),
                "attempt": attempt + 1,
                "artifacts": str(artifact.relative_to(self.run_dir)),
                "status": "running",
                "prompt_chars": len(prompt),
                "prompt_bytes": len(prompt.encode("utf-8")),
            }
            self.state["agents"].append(record)
            self._save(role)
            try:
                result = self.invoke(
                    selection=selection,
                    role=role,
                    prompt=prompt,
                    cwd=directory,
                    artifact_dir=artifact,
                    timeout=self._timeout(),
                    schema=RESPONSE_SCHEMA,
                    executable=self.config.executables[selection.backend],
                    max_output_bytes=self.config.max_output_bytes,
                )
            except (BackendError, BackendInterrupted) as exc:
                record.update(
                    status="interrupted" if isinstance(exc, BackendInterrupted) else "failed",
                    error="Interrupted by user"
                    if isinstance(exc, BackendInterrupted)
                    else str(exc),
                    observed_model=exc.observed_model,
                    retryable=exc.retryable,
                    cost_usd=exc.cost_usd,
                    usage=exc.usage,
                    token_usage=normalize_usage(selection.backend, exc.usage),
                )
                self._save()
                if isinstance(exc, BackendInterrupted):
                    raise
                if frozen is not None and manifest(directory, self.config.exclude) != frozen:
                    raise WorkflowStop(
                        "failed", f"Read-only agent {role} changed its snapshot"
                    ) from exc
                if not exc.retryable or attempt + 1 == attempts:
                    raise
                continue
            record.update(
                status="returned",
                cost_usd=result.cost_usd,
                usage=result.usage,
                token_usage=normalize_usage(selection.backend, result.usage),
                observed_model=result.model,
            )
            self._save()
            if frozen is not None and manifest(directory, self.config.exclude) != frozen:
                raise WorkflowStop("failed", f"Read-only agent {role} changed its snapshot")
            response = validate_response(
                result.payload, role, directory, baseline_root=self.run_dir / "baseline"
            )
            record.update(status="completed", summary=response["summary"])
            self._save()
            if response["status"] == "blocked":
                raise WorkflowStop("blocked", f"{role}: {response['summary']}")
            return response
        raise AssertionError("Unreachable retry state")

    def _prompt(self, role: str, directory: Path, context: dict) -> str:
        prompts = Path(__file__).with_name("prompts")
        skill_context = []
        for name, path in self._skill_paths(role, directory):
            skill = {"path": name}
            if self.config.context.skill_mode == "inline":
                skill["content"] = path.read_text(encoding="utf-8")
            skill_context.append(skill)
        instructions = sorted(
            path
            for path in manifest(directory, self.config.exclude)
            if Path(path).name in {"AGENTS.md", "CLAUDE.md"}
        )
        payload = {
            "working_directory": str(directory),
            "task": self.task,
            "project_rules": instructions,
            "skills": skill_context,
            "evidence": self._prompt_context(context),
        }
        return (
            prompts.joinpath("common.md").read_text(encoding="utf-8")
            + "\n"
            + prompts.joinpath(f"{role}.md").read_text(encoding="utf-8")
            + "\nTask and evidence (JSON):\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    def _prompt_context(self, context: dict) -> dict:
        """Project check evidence for agents while retaining full records and raw logs."""
        projected = dict(context)
        for key in (
            "checks",
            "baseline_checks",
            "baseline_preparation",
            "failed_checks",
            "failed_preparation",
        ):
            if key not in context:
                continue
            checks = []
            for item in context[key]:
                check = {
                    "command": item["command"],
                    "passed": item["passed"],
                    "logs": str(self.run_dir / item["artifacts"]),
                }
                if not item["passed"]:
                    for field in (
                        "returncode",
                        "timed_out",
                        "output_limit_exceeded",
                        "source_unchanged",
                    ):
                        check[field] = item[field]
                    for stream in ("stdout", "stderr"):
                        tail = item[f"{stream}_tail"]
                        limit = self.config.context.failed_check_tail_chars
                        excerpt = tail[-limit:] if limit else ""
                        check[f"{stream}_tail"] = excerpt
                        check[f"{stream}_omitted_chars"] = item.get(
                            f"{stream}_chars", len(tail)
                        ) - len(excerpt)
                checks.append(check)
            projected[key] = checks
        return projected

    def _diff(self, changed: list[str]) -> str:
        import difflib

        sections = []
        remaining = self.config.context.max_diff_chars
        omitted = "\nDiff truncated; inspect changed_paths in candidate and baseline_directory.\n"
        for name in changed:
            self._timeout()
            if remaining <= 0:
                sections.append(omitted)
                break
            original, candidate = self.run_dir / "baseline" / name, self.work / name
            if any(
                path.is_file() and path.stat().st_size > 256000 for path in (original, candidate)
            ):
                text = f"Large file diff omitted: {name}; inspect source directly.\n"
            else:
                try:
                    before = (
                        original.read_text(encoding="utf-8").splitlines(keepends=True)
                        if original.is_file()
                        else []
                    )
                    after = (
                        candidate.read_text(encoding="utf-8").splitlines(keepends=True)
                        if candidate.is_file()
                        else []
                    )
                    text = "".join(
                        difflib.unified_diff(
                            before, after, fromfile=f"before/{name}", tofile=f"after/{name}"
                        )
                    )
                except UnicodeDecodeError:
                    text = f"Binary change: {name}; inspect source directly.\n"
            sections.append(text[:remaining])
            remaining -= len(text)
            if remaining < 0:
                sections.append(omitted)
                break
        return "".join(sections)

    def _report(self) -> None:
        cost = f"{self.state['known_cost_usd']:.4f} USD reported"
        if self.state["unknown_cost_calls"]:
            cost += f"; {self.state['unknown_cost_calls']} call(s) without cost data; total unknown"
        lines = [
            "# Workflow report",
            "",
            f"Status: **{self.state['status']}**",
            "",
            self.state["reason"],
            "",
            f"Complexity: {self.level}",
            f"Fixer invocations: {self.state['cycles']}/{self.config.max_cycles}",
            f"Agent calls: {self.state['agent_calls']}/{self.config.max_agent_calls}",
            f"Cost: {cost}",
            "",
            "Original project files were not automatically updated.",
            "",
        ]
        lines.extend(["## Tokens and prompt size", ""])
        lines.append(
            "Prompt characters/UTF-8 bytes measure submitted text only, not provider tokens "
            "or the CLI's instructions, tools and later reads."
        )
        for backend, usage in self.state["token_usage"]["by_backend"].items():
            counts = usage["known_totals"]
            measured = ", ".join(
                f"{name}={value if value is not None else 'unknown'}"
                for name, value in counts.items()
            )
            lines.append(
                f"- {backend}: measured subtotals {measured}; "
                f"{usage['complete_calls']}/{usage['calls']} calls with known total, "
                f"{usage['partial_calls']} partial, {usage['unknown_calls']} unknown. "
                "Cache counters are part of input, not additional tokens."
            )
        lines.extend(["", "## Checks", ""])
        for item in self.state["checks"]:
            lines.append(
                f"- {item['phase']}: {'PASS' if item['passed'] else 'FAIL'} — "
                + " ".join(item["command"])
            )
        if self.state["reviews"]:
            latest = self.state["reviews"][-1]
            remaining = [*latest.get("confirmed", []), *latest.get("unresolved", [])]
            lines.extend(["", "## Latest review findings", ""])
            if not remaining:
                lines.append("No confirmed or unresolved findings in the last completed review.")
            for item in remaining:
                lines.append(
                    f"- {item['severity']}: {item['path']}:{item['line']} — {item['title']}"
                )
        lines.extend(["", "## Agent summaries", ""])
        for item in self.state["agents"]:
            lines.append(
                f"- {item['role']} ({item['selection']['model']}): "
                + item.get("summary", item.get("error", item["status"]))
                + f" — prompt {item['prompt_chars']} chars / {item['prompt_bytes']} bytes"
            )
        lines.extend(
            [
                "",
                "Full evidence, routing, findings, triage and raw log paths: state.json.",
                "",
                "A succeeded result means configured checks and reviews passed. "
                "It is not a proof that all requirements or defects were covered.",
            ]
        )
        self.run_dir.joinpath("report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
