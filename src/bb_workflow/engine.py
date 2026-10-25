"""A bounded, auditable state machine; backend output never controls stage order."""

from __future__ import annotations

import json
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .acceptance import REPORT_CONTRACT, build_acceptance, canonical_digest, validate_acceptance
from .backends import BackendError, invoke
from .config import Config, Selection, route
from .diagnostics import summarize_diagnostics
from .process import run_process
from .protocol import merge_findings, resolve_triage, response_schema, validate_response
from .reporting import render_report
from .requirements import (
    Requirements,
    bind_requirements,
    evaluate_mechanical,
    verification_plan_id,
)
from .usage import normalize_usage, summarize_usage
from .verification import (
    PlannedCheck,
    VerificationStop,
    aggregate_diagnostics,
    execute_checks,
    plan_verification,
)
from .workspace import (
    copy_snapshot,
    initialize_git_snapshot,
    manifest,
    project_lock,
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
        requirements: Requirements | None = None,
        invoke_fn: Callable = invoke,
        process_fn: Callable = run_process,
        progress: Callable[[str], None] = print,
    ):
        self.repo = repo.resolve()
        self.task = task
        self.config = config = deepcopy(config)
        self.gate_plan = plan_verification(config, "gate")
        self.requirements = requirements
        self.bound = None
        self.response_version = 2 if requirements is not None else 1
        self.level, self.selections = route(config, task, complexity)
        self.invoke = invoke_fn
        self.process = process_fn
        self.progress = progress
        self.started = time.monotonic()
        self.run_dir = self.repo / ".bb-workflow" / "runs" / uuid.uuid4().hex
        self.work = self.run_dir / "workspace"
        self.state = {
            "version": 2,
            "report_contract": REPORT_CONTRACT,
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
            "acceptance": {
                "mode": "criteria"
                if requirements is not None
                else "required"
                if config.config_version >= 3
                else "legacy",
                "compatibility_mode": requirements is None and config.config_version < 3,
                "requirements_id": None,
                "verification_plan_id": verification_plan_id(self.gate_plan),
                "rounds": [],
            },
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
                # Keep durable state non-successful until both report artifacts exist.
                try:
                    self._report()
                except KeyboardInterrupt:
                    self.state.update(
                        status="interrupted", reason="Interrupted while writing report"
                    )
                except Exception as exc:
                    self.state.update(
                        status="failed",
                        reason=f"Report persistence failed: {type(exc).__name__}: {exc}",
                    )
                self._save("finished")
        return self.run_dir

    def _execute(self) -> None:
        self._timeout()
        if self.requirements is None and self.config.config_version >= 3:
            raise WorkflowStop("blocked", "Configuration v3 requires nonempty requirements")
        if self.requirements is not None:
            self.bound = bind_requirements(self.requirements, self.gate_plan)
            self.state["acceptance"]["requirements_id"] = self.bound.requirements_id
            write_json(self.run_dir / "requirements.json", asdict(self.bound.requirements))
        write_json(self.run_dir / "verification_plan.json", asdict(self.gate_plan))
        self._save()
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
                "diagnostics": self.state["diagnostics"]["baseline"],
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
            round_record = None
            if self.bound is not None:
                round_record = {
                    "cycle": self.state["cycles"],
                    "binding": self._binding(candidate_id),
                    "mechanical": [],
                    "semantic": [],
                }
                self.state["acceptance"]["rounds"].append(round_record)
            label = f"candidate-{self.state['cycles']}"
            try:
                checks = self._checks(label, expected=candidate)
            except BaseException:
                if round_record is not None:
                    # Retain partial proof without replacing a fatal source/budget/interrupt status.
                    partial = [item for item in self.state["checks"] if item["phase"] == label]
                    try:
                        self._mechanical(round_record, partial)
                    except Exception as exc:
                        round_record["audit_error"] = f"{type(exc).__name__}: {exc}"
                raise
            mechanical = self._mechanical(round_record, checks)
            if any(item["verdict"] == "unresolved" for item in mechanical):
                raise WorkflowStop("blocked", "Mechanical acceptance proof is unresolved")
            if not all(item["passed"] for item in checks):
                self._fix(
                    {
                        "failed_checks": [item for item in checks if not item["passed"]],
                        "mechanical_criteria": mechanical,
                        "diagnostics": self.state["diagnostics"][label],
                    }
                )
                continue
            if any(item["verdict"] == "unmet" for item in mechanical):
                self._fix(
                    {
                        "mechanical_criteria": mechanical,
                        "diagnostics": self.state["diagnostics"][label],
                    }
                )
                continue
            changed = sorted(
                k
                for k in self.baseline.keys() | candidate.keys()
                if self.baseline.get(k) != candidate.get(k)
            )
            shared = {
                "checks": checks,
                "diagnostics": self.state["diagnostics"][f"candidate-{self.state['cycles']}"],
                "snapshot_id": candidate_id,
                "baseline_directory": str(self.run_dir / "baseline"),
                "changed_paths": changed,
                "diff": self._diff(changed),
            }
            if self.bound is not None:
                shared["mechanical_criteria"] = mechanical
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
            record = {
                "cycle": self.state["cycles"],
                "snapshot_id": candidate_id,
                "reviews": reviews,
                "findings": findings,
            }
            self.state["reviews"].append(record)
            confirmed, unresolved = [], []
            semantic, disputed = self._semantic(reviews, candidate_id)
            if round_record is not None:
                round_record["reviews"] = reviews
                round_record["semantic"] = semantic
            if findings or disputed:
                directory = self.run_dir / f"round-{self.state['cycles']}-triage"
                frozen = copy_snapshot(self.work, directory, self.config.exclude)
                if frozen != candidate:
                    raise WorkflowStop("failed", "Candidate changed while preparing triage")
                initialize_git_snapshot(directory, timeout=self._timeout())
                triage_context = {**shared, "findings": findings}
                if self.bound is not None:
                    triage_context.update(
                        disputed_criteria=disputed,
                        criterion_assessments=[
                            {
                                "role": role,
                                "binding": review["binding"],
                                "assessments": review["criterion_assessments"],
                            }
                            for role, review in zip(("reviewer_a", "reviewer_b"), reviews)
                        ],
                    )
                triage = self._agent("triage", directory, triage_context, frozen)
                if manifest(self.work, self.config.exclude) != candidate:
                    raise WorkflowStop("failed", "Candidate changed during triage")
                confirmed, unresolved = resolve_triage(findings, triage)
                record["triage"] = triage
                if round_record is not None:
                    round_record["triage"] = triage
                    by_id = {item["criterion_id"]: item for item in triage["criterion_decisions"]}
                    semantic = [
                        {**item, **by_id.get(item["criterion_id"], {})} for item in semantic
                    ]
                    round_record["semantic"] = semantic
            record.update(confirmed=confirmed, unresolved=unresolved)
            self._save()
            if unresolved or any(item["verdict"] == "unresolved" for item in semantic):
                raise WorkflowStop("blocked", "Triage left unresolved findings or criteria")
            blocking = [
                item for item in confirmed if item["severity"] in self.config.blocking_severities
            ]
            unmet = [item for item in semantic if item["verdict"] == "unmet"]
            if blocking or unmet:
                self._fix({"confirmed_findings": blocking, "unmet_criteria": unmet})
                continue
            if manifest(self.work, self.config.exclude) != candidate:
                raise WorkflowStop("failed", "Candidate changed after verification")
            self._timeout()
            write_json(self.run_dir / "candidate_manifest.json", candidate)
            self.state.update(candidate_id=candidate_id)
            approval = build_acceptance(self.run_dir, self.state)
            write_json(self.run_dir / "acceptance.json", approval)
            self.state["acceptance_report"] = {
                "version": REPORT_CONTRACT,
                "file": "acceptance.json",
                "digest": canonical_digest(approval),
                "binding": approval["binding"],
            }
            validate_acceptance(self.run_dir, self.state)
            self.state.update(
                status="succeeded",
                reason=(
                    "Checks and all acceptance criteria passed; no confirmed blocking findings"
                    if self.bound is not None
                    else "Checks passed; no confirmed blocking findings (legacy compatibility)"
                ),
                candidate_id=candidate_id,
                changed_paths=changed,
                remaining_findings=confirmed,
            )
            return

    def _binding(self, candidate_id: str) -> dict:
        return {
            "candidate_id": candidate_id,
            "requirements_id": self.bound.requirements_id,
            "verification_plan_id": self.bound.verification_plan_id,
        }

    def _mechanical(self, round_record: dict | None, checks: list[dict]) -> list[dict]:
        if round_record is None:
            return []
        results = evaluate_mechanical(self.bound, checks, round_record["binding"], self.run_dir)
        round_record["mechanical"] = results
        self._save()
        return results

    def _semantic(self, reviews: list[dict], candidate_id: str) -> tuple[list[dict], list[str]]:
        if self.bound is None:
            return [], []
        assessments = [
            {item["criterion_id"]: item for item in review["criterion_assessments"]}
            for review in reviews
        ]
        results, disputed = [], []
        for criterion in self.bound.requirements.criteria:
            if criterion.kind != "semantic":
                continue
            satisfied = all(items[criterion.id]["verdict"] == "satisfied" for items in assessments)
            if not satisfied:
                disputed.append(criterion.id)
            results.append(
                {
                    "criterion_id": criterion.id,
                    "verdict": "satisfied" if satisfied else "unresolved",
                    "evidence": [items[criterion.id] for items in assessments],
                    "binding": self._binding(candidate_id),
                }
            )
        return results, disputed

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
        if preparing:
            checks = tuple(
                PlannedCheck(str(index), "preparation", "default", tuple(argv))
                for index, argv in enumerate(self.config.prepare)
            )
        else:
            checks = self.gate_plan.checks
        if not checks:
            return []
        self._save(f"checks:{label}")

        phase_records = []

        def record(item: dict) -> None:
            phase_records.append(item)
            self.state["checks"].append({"phase": label, **item})
            self._save()

        try:
            return execute_checks(
                checks,
                cwd=self.work,
                artifact_root=self.run_dir,
                command_root=self.run_dir / "checks" / label,
                config=self.config,
                timeout=self._timeout,
                record=record,
                expected=expected,
                preparing=preparing,
                process_fn=self.process,
                baseline_records=self.state.get("baseline_checks", [])
                if label != "baseline"
                else [],
            )
        except VerificationStop as exc:
            raise WorkflowStop(exc.status, str(exc)) from exc
        finally:
            if not preparing:
                aggregate = aggregate_diagnostics(
                    phase_records,
                    self.state.get("baseline_checks", []) if label != "baseline" else [],
                    checks=checks,
                    artifact_root=self.run_dir,
                    project_root=self.work,
                )
                path = self.run_dir / "checks" / label / "diagnostics.json"
                write_json(path, aggregate)
                self.state.setdefault("diagnostics", {})[label] = {
                    **summarize_diagnostics({"comparison": aggregate}),
                    "file": str(path.relative_to(self.run_dir)),
                }
                self._save()

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
        binding = None
        if self.bound is not None:
            binding = self._binding(
                snapshot_id(
                    frozen if frozen is not None else manifest(directory, self.config.exclude)
                )
            )
        prompt = self._prompt(role, directory, context, binding)
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
                "cycle": self.state["cycles"],
                "requested_model": selection.model,
                "selection": asdict(selection),
                "attempt": attempt + 1,
                "artifacts": str(artifact.relative_to(self.run_dir)),
                "status": "running",
                "prompt_chars": len(prompt),
                "prompt_bytes": len(prompt.encode("utf-8")),
            }
            if binding is not None:
                record["binding"] = dict(binding)
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
                    schema=response_schema(self.response_version),
                    executable=self.config.executables[selection.backend],
                    max_output_bytes=self.config.max_output_bytes,
                )
            except BackendError as exc:
                record.update(
                    status="failed",
                    error=str(exc),
                    observed_model=exc.observed_model,
                    retryable=exc.retryable,
                    cost_usd=exc.cost_usd,
                    usage=exc.usage,
                    token_usage=normalize_usage(selection.backend, exc.usage),
                )
                self._save()
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
                result.payload,
                role,
                directory,
                baseline_root=self.run_dir / "baseline",
                version=self.response_version,
                expected_binding=binding,
                criteria=self.bound.requirements.criteria if self.bound is not None else (),
                disputed_criteria=tuple(context.get("disputed_criteria", ())),
            )
            if binding is not None:
                record["criterion_evidence"] = deepcopy(response["criterion_evidence"])
            write_json(artifact / "validated_response.json", response)
            record.update(
                status="completed", summary=response["summary"], response=deepcopy(response)
            )
            self._save()
            if response["status"] == "blocked":
                raise WorkflowStop("blocked", f"{role}: {response['summary']}")
            return response
        raise AssertionError("Unreachable retry state")

    def _prompt(
        self, role: str, directory: Path, context: dict, binding: dict | None = None
    ) -> str:
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
        if self.bound is not None:
            payload["acceptance"] = {
                "schema_version": 2,
                "requirements": asdict(self.bound.requirements),
                "verification_plan": asdict(self.gate_plan),
                "binding": binding,
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
        if "diagnostics" in context:
            projected["diagnostics"] = {
                **context["diagnostics"],
                "file": str(self.run_dir / context["diagnostics"]["file"]),
            }
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
                if item.get("diagnostics"):
                    check["diagnostics"] = {
                        **item["diagnostics"],
                        "file": str(self.run_dir / item["diagnostics_file"]),
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
        self.run_dir.joinpath("report.md").write_text(
            render_report(
                self.state,
                asdict(self.gate_plan),
                asdict(self.bound.requirements) if self.bound else None,
            ),
            encoding="utf-8",
        )
