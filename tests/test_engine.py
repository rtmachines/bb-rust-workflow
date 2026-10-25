"""State-machine integration tests with real snapshots and fixture providers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from bb_workflow.backends import BackendError, BackendResult
from bb_workflow.config import (
    ContextConfig,
    Selection,
    VerificationConfig,
    VerificationProfile,
    VerificationVariant,
    load_config,
    route,
)
from bb_workflow.engine import Workflow
from bb_workflow.process import ProcessResult, run_process
from bb_workflow.protocol import RESPONSE_SCHEMA
from bb_workflow.workspace import WorkspaceError, apply_run, manifest, snapshot_id


def response(*, status="completed", summary="Fixture complete", findings=None, decisions=None):
    return {
        "status": status,
        "summary": summary,
        "findings": findings or [],
        "decisions": decisions or [],
    }


def finding(identifier="A1", severity="high", title="Incorrect result"):
    return {
        "id": identifier,
        "path": "src/lib.rs",
        "line": 1,
        "severity": severity,
        "title": title,
        "detail": "Concrete defect",
        "evidence": "src/lib.rs:1 shows the defect",
    }


def decision(identifier="F1", disposition="confirmed", duplicate_of=""):
    return {
        "finding_id": identifier,
        "disposition": disposition,
        "rationale": "Checked against the source",
        "duplicate_of": duplicate_of,
    }


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve() / "project"
        (self.repo / "src").mkdir(parents=True)
        self.original = "pub fn value() -> u32 {\n    0\n}\n"
        (self.repo / "src/lib.rs").write_text(self.original, encoding="utf-8")
        self.config = replace(
            load_config(),
            config_version=2,
            prepare=[],
            checks=[["fixture-check", "--strict"]],
            max_cycles=2,
            timeout_seconds=20,
            max_seconds=100,
        )
        self.calls = []
        self.check_calls = []
        self.events = []
        self.scripts = {}
        self.check_script = []
        self.fixer_count = 0

    def invoke(self, **kwargs):
        self.events.append(kwargs["role"])
        self.calls.append({**kwargs, "snapshot": manifest(kwargs["cwd"])})
        role = kwargs["role"]
        scripted = self.scripts.get(role, [])
        if scripted:
            action = scripted.pop(0)
            if isinstance(action, Exception):
                raise action
            payload = action(kwargs) if callable(action) else action
        else:
            if role == "implementation":
                (kwargs["cwd"] / "src/lib.rs").write_text("pub fn value() -> u32 { 1 }\n")
            if role == "fixer":
                self.fixer_count += 1
                (kwargs["cwd"] / "src/lib.rs").write_text(
                    f"pub fn value() -> u32 {{ {self.fixer_count + 1} }}\n"
                )
            payload = response()
        if isinstance(payload, BackendResult):
            return payload
        return BackendResult(payload, 0.1, {"input_tokens": 10}, kwargs["selection"].model)

    def process(self, argv, **kwargs):
        self.events.append("check")
        self.check_calls.append({"argv": argv, **kwargs})
        if self.check_script:
            action = self.check_script.pop(0)
            if isinstance(action, Exception):
                raise action
            return action(kwargs) if callable(action) else action
        return ProcessResult(0, "checks passed", "", 0.01)

    def workflow(self, **kwargs):
        return Workflow(
            self.repo,
            kwargs.pop("task", "implement a value function"),
            kwargs.pop("config", self.config),
            invoke_fn=self.invoke,
            process_fn=self.process,
            progress=lambda message: None,
            **kwargs,
        )

    def run_workflow(self, **kwargs):
        flow = self.workflow(**kwargs)
        directory = flow.run()
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state, json.loads(json.dumps(flow.state)))
        self.assertTrue((directory / "report.md").is_file())
        self.assertFalse((self.repo / ".bb-workflow/lock").exists())
        self.assertEqual((self.repo / "src/lib.rs").read_text(), self.original)
        return flow, state

    def test_old_config_without_requirements_retains_v1_schema_and_explicit_legacy_mode(self):
        for version in (1, 2):
            with self.subTest(version=version):
                flow, state = self.run_workflow(config=replace(self.config, config_version=version))
                self.assertEqual(state["status"], "succeeded", state["reason"])
                self.assertEqual(state["acceptance"]["mode"], "legacy")
                self.assertTrue(state["acceptance"]["compatibility_mode"])
                self.assertEqual(state["acceptance"]["rounds"], [])
                self.assertEqual(self.calls[-1]["schema"], RESPONSE_SCHEMA)
                self.assertIn("legacy", (flow.run_dir / "report.md").read_text())

    def test_every_gate_variant_precedes_independent_reviews(self):
        verification = VerificationConfig()
        verification.profiles["gate"] = VerificationProfile(variants=("default", "feature"))
        verification.variants["feature"] = VerificationVariant(features=("extra",))
        self.config = replace(
            self.config, checks=[["cargo", "check", "--workspace"]], verification=verification
        )
        flow, state = self.run_workflow()
        self.assertEqual(state["status"], "succeeded", state["reason"])
        self.assertEqual(
            self.events,
            ["check", "check", "implementation", "check", "check", "reviewer_a", "reviewer_b"],
        )
        candidate = [c for c in state["checks"] if c["phase"] == "candidate-0"]
        self.assertEqual([c["variant"] for c in candidate], ["default", "feature"])
        self.assertEqual(
            candidate[1]["command"], ["cargo", "check", "--workspace", "--features", "extra"]
        )
        self.assertEqual({c["snapshot_id"] for c in candidate}, {state["candidate_id"]})
        self.assertNotEqual(self.calls[1]["cwd"], self.calls[2]["cwd"])
        self.assertTrue((flow.run_dir / candidate[1]["artifacts"] / "stdout.txt").exists())

    def test_variant_failure_runs_fixer_and_repeats_full_gate_before_reviews(self):
        verification = VerificationConfig()
        verification.profiles["gate"] = VerificationProfile(variants=("default", "feature"))
        verification.variants["feature"] = VerificationVariant(all_features=True)
        self.config = replace(
            self.config, checks=[["cargo", "test", "--workspace"]], verification=verification
        )
        self.check_script = [ProcessResult(0, "", "", 0)] * 3 + [
            ProcessResult(1, "", "feature failed", 0)
        ]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "succeeded", state["reason"])
        self.assertEqual(
            self.events,
            [
                "check",
                "check",
                "implementation",
                "check",
                "check",
                "fixer",
                "check",
                "check",
                "reviewer_a",
                "reviewer_b",
            ],
        )
        failed = [c for c in state["checks"] if not c["passed"]]
        self.assertEqual([(c["phase"], c["variant"]) for c in failed], [("candidate-0", "feature")])
        self.assertEqual(len([c for c in state["checks"] if c["phase"] == "candidate-1"]), 2)

    def test_success_uses_identical_separate_review_snapshots_and_no_cross_review_prompt(self):
        self.scripts["reviewer_a"] = [response(summary="REVIEW_A_SECRET")]
        flow, state = self.run_workflow()
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(
            [call["role"] for call in self.calls], ["implementation", "reviewer_a", "reviewer_b"]
        )
        first, second = self.calls[1:]
        self.assertNotEqual(first["cwd"], second["cwd"])
        self.assertNotEqual(first["cwd"], flow.work)
        self.assertEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(first["snapshot"], manifest(flow.work))
        self.assertNotIn("REVIEW_A_SECRET", second["prompt"])
        evidence = []
        for call in (first, second):
            payload = json.loads(
                call["prompt"]
                .split("Task and evidence (JSON):\n")[1]
                .split("\nRequired response schema:\n")[0]
            )
            evidence.append(payload["evidence"])
        self.assertEqual(evidence[0], evidence[1])
        self.assertEqual(evidence[0]["snapshot_id"], state["candidate_id"])
        self.assertEqual(state["candidate_id"], snapshot_id(manifest(flow.work)))
        self.assertEqual(state["changed_paths"], ["src/lib.rs"])
        self.assertEqual(state["cycles"], 0)
        self.assertAlmostEqual(state["known_cost_usd"], 0.3)
        self.assertEqual(state["unknown_cost_calls"], 0)

    def test_candidate_gate_failure_runs_fixer_before_review_then_rechecks(self):
        self.check_script = [
            ProcessResult(0, "baseline", "", 0.01),
            ProcessResult(1, "", "candidate failed", 0.01),
            ProcessResult(0, "repaired", "", 0.01),
        ]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(
            self.events,
            ["check", "implementation", "check", "fixer", "check", "reviewer_a", "reviewer_b"],
        )
        self.assertEqual(state["cycles"], 1)
        fixer = next(call for call in self.calls if call["role"] == "fixer")
        self.assertIn("candidate failed", fixer["prompt"])
        self.assertEqual([check["passed"] for check in state["checks"]], [True, False, True])

    def test_successful_check_logs_stay_in_artifacts_without_filling_agent_prompts(self):
        stdout = "SUCCESS_STDOUT:" + "x" * 14000
        stderr = "SUCCESS_STDERR:" + "y" * 14000
        self.check_script = [ProcessResult(0, stdout, stderr, 0.01)] * 2
        flow, state = self.run_workflow(task="Implement a value function: zażółć")
        self.assertEqual(state["status"], "succeeded")
        for call, record in zip(self.calls, state["agents"], strict=True):
            prompt = call["prompt"]
            payload = json.loads(prompt.split("Task and evidence (JSON):\n", 1)[1])
            key = "baseline_checks" if call["role"] == "implementation" else "checks"
            check = payload["evidence"][key][0]
            self.assertTrue(check["passed"])
            self.assertEqual(check["command"], ["fixture-check", "--strict"])
            self.assertNotIn("stdout_tail", check)
            self.assertNotIn("stderr_tail", check)
            log_dir = Path(check["logs"])
            self.assertTrue(log_dir.is_absolute())
            self.assertEqual((log_dir / "stdout.txt").read_text(encoding="utf-8"), stdout)
            self.assertEqual((log_dir / "stderr.txt").read_text(encoding="utf-8"), stderr)
            self.assertNotIn("x" * 100, prompt)
            self.assertNotIn("y" * 100, prompt)
            self.assertNotIn("Required response schema", prompt)
            self.assertEqual(call["schema"], RESPONSE_SCHEMA)
            self.assertEqual(record["prompt_chars"], len(prompt))
            self.assertEqual(record["prompt_bytes"], len(prompt.encode("utf-8")))
        self.assertEqual(state["checks"][0]["stdout_tail"], stdout[-12000:])
        self.assertEqual(state["checks"][0]["stderr_tail"], stderr[-12000:])
        self.assertEqual(state["checks"][0]["stdout_chars"], len(stdout))
        self.assertEqual(state["checks"][0]["stderr_chars"], len(stderr))
        self.assertEqual(
            json.loads((flow.run_dir / "state.json").read_text())["checks"], state["checks"]
        )

    def test_failed_check_excerpts_count_all_omissions_and_link_full_logs(self):
        stdout = "start" + "ą" * 20000 + "OUT_END"
        stderr = "start" + "b" * 20000 + "ERR_END"
        for limit in (7, 0, 16000):
            with self.subTest(limit=limit):
                self.calls.clear()
                self.check_script = [
                    ProcessResult(0, "", "", 0.01),
                    ProcessResult(1, stdout, stderr, 0.01),
                    ProcessResult(0, "", "", 0.01),
                ]
                config = replace(self.config, context=ContextConfig(failed_check_tail_chars=limit))
                _, state = self.run_workflow(config=config)
                self.assertEqual(state["status"], "succeeded")
                fixer = next(call for call in self.calls if call["role"] == "fixer")
                payload = json.loads(fixer["prompt"].split("Task and evidence (JSON):\n", 1)[1])
                check = payload["evidence"]["failed_checks"][0]
                self.assertFalse(check["passed"])
                self.assertEqual(check["returncode"], 1)
                self.assertFalse(check["timed_out"])
                self.assertTrue(check["source_unchanged"])
                for name, text in (("stdout", stdout), ("stderr", stderr)):
                    self.assertEqual(check[f"{name}_tail"], text[-limit:] if limit else "")
                    self.assertEqual(check[f"{name}_omitted_chars"], len(text) - limit)
                    self.assertEqual(
                        (Path(check["logs"]) / f"{name}.txt").read_text(encoding="utf-8"), text
                    )

    def test_provider_token_totals_are_saved_and_reported_without_double_counting_cache(self):
        usages = {
            "implementation": {
                "input_tokens": 10,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 30,
                "output_tokens": 5,
            },
            "reviewer_a": {
                "input_tokens": 40,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 0,
                "output_tokens": 7,
            },
            "reviewer_b": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20},
        }
        for role, usage in usages.items():
            model = self.config.profiles["medium"][role].model
            self.scripts[role] = [BackendResult(response(), 0.1, usage, model)]
        flow, state = self.run_workflow()
        self.assertEqual(state["status"], "succeeded")
        groups = state["token_usage"]["by_backend"]
        self.assertEqual(groups["claude"]["complete_calls"], 2)
        self.assertEqual(groups["claude"]["known_totals"]["input_tokens"], 105)
        self.assertEqual(groups["claude"]["known_totals"]["total_tokens"], 117)
        self.assertEqual(groups["claude"]["known_totals"]["cached_input_tokens"], 25)
        self.assertEqual(groups["codex"]["complete_calls"], 1)
        self.assertEqual(groups["codex"]["known_totals"]["input_tokens"], 100)
        self.assertEqual(groups["codex"]["known_totals"]["total_tokens"], 120)
        self.assertEqual(groups["codex"]["known_totals"]["cached_input_tokens"], 60)
        for record in state["agents"]:
            self.assertEqual(record["usage"], usages[record["role"]])
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("total_tokens=117", report)
        self.assertIn("total_tokens=120", report)
        self.assertIn("2/2 calls with known total", report)
        self.assertIn("1/1 calls with known total", report)
        self.assertIn("0 partial, 0 unknown", report)

    def test_partial_token_measurements_and_failed_calls_remain_visible(self):
        usage = {
            "input_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        model = self.config.profiles["medium"]["implementation"].model
        self.scripts["implementation"] = [BackendResult(response(), 0.1, usage, model)]
        self.scripts["reviewer_a"] = [BackendError("Provider interrupted")]
        flow, state = self.run_workflow()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["agents"][-1]["status"], "failed")
        self.assertEqual(state["unknown_cost_calls"], 1)
        group = state["token_usage"]["by_backend"]["claude"]
        self.assertEqual(group["calls"], 2)
        self.assertEqual(group["complete_calls"], 0)
        self.assertEqual(group["partial_calls"], 1)
        self.assertEqual(group["unknown_calls"], 1)
        self.assertEqual(group["known_totals"]["input_tokens"], 10)
        self.assertIsNone(group["known_totals"]["total_tokens"])
        self.assertEqual(group["missing_calls"]["total_tokens"], 2)
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("total_tokens=unknown", report)
        self.assertIn("0/2 calls with known total, 1 partial, 1 unknown", report)
        self.assertIn("Provider interrupted", report)

    def test_selected_skills_are_referenced_by_default_and_inline_when_requested(self):
        name = "skills/manual.md"
        path = self.repo / name
        path.parent.mkdir()
        content = "Required domain rules: preserve the counter invariant.\n"
        path.write_text(content)
        selection = replace(self.config.profiles["medium"]["implementation"], skills=(name,))
        for mode in ("reference", "inline"):
            with self.subTest(mode=mode):
                config = replace(
                    self.config,
                    roles={"implementation": selection},
                    context=ContextConfig(skill_mode=mode),
                )
                prompt = self.workflow(config=config)._prompt("implementation", self.repo, {})
                payload = json.loads(prompt.split("Task and evidence (JSON):\n", 1)[1])
                expected = {"path": name}
                if mode == "inline":
                    expected["content"] = content
                else:
                    self.assertNotIn(content.strip(), prompt)
                self.assertEqual(payload["skills"], [expected])

    def test_missing_or_escaping_selected_skill_blocks_before_provider_call(self):
        outside = self.repo.parent / "outside.md"
        outside.write_text("Outside skill must not be loaded")
        for name in ("missing.md", "../outside.md"):
            for mode in ("reference", "inline"):
                with self.subTest(name=name, mode=mode):
                    selection = replace(
                        self.config.profiles["medium"]["implementation"], skills=(name,)
                    )
                    config = replace(
                        self.config,
                        roles={"implementation": selection},
                        context=ContextConfig(skill_mode=mode),
                    )
                    _, state = self.run_workflow(config=config)
                    self.assertEqual(state["status"], "blocked")
                    self.assertIn("Skill must exist inside project", state["reason"])
                    self.assertFalse(self.calls)

    def test_diff_budget_applies_to_text_large_and_binary_changes(self):
        flow = self.workflow()
        flow.work.mkdir(parents=True)
        baseline = flow.run_dir / "baseline"
        baseline.mkdir()
        (flow.work / "text.rs").write_text("pub const VALUE: u32 = 1;\n" * 20)
        (flow.work / "large.rs").write_bytes(b"x" * 256001)
        (flow.work / "binary.bin").write_bytes(b"\xff\xfe")
        notice = "\nDiff truncated; inspect changed_paths in candidate and baseline_directory.\n"
        for name, prefix in (
            ("text.rs", "--- before/text.rs"),
            ("large.rs", "Large file diff omitted:"),
            ("binary.bin", "Binary change:"),
        ):
            with self.subTest(name=name):
                flow.config.context = ContextConfig(max_diff_chars=24)
                excerpt = flow._diff([name])
                self.assertTrue(excerpt.startswith(prefix))
                self.assertTrue(excerpt.endswith(notice))
                self.assertEqual(len(excerpt), 24 + len(notice))
                self.assertTrue((flow.work / name).is_file())
        flow.config.context = ContextConfig(max_diff_chars=0)
        self.assertEqual(flow._diff(["text.rs"]), notice)
        self.assertEqual(flow._diff([]), "")

    def test_diff_at_exact_budget_reports_unseen_remaining_files(self):
        flow = self.workflow()
        flow.work.mkdir(parents=True)
        (flow.run_dir / "baseline").mkdir()
        (flow.work / "a.bin").write_bytes(b"\xff")
        (flow.work / "b.bin").write_bytes(b"\xfe")
        first = flow._diff(["a.bin"])
        flow.config.context = ContextConfig(max_diff_chars=len(first))
        self.assertEqual(flow._diff(["a.bin"]), first)
        combined = flow._diff(["a.bin", "b.bin"])
        self.assertTrue(combined.startswith(first))
        self.assertIn("Diff truncated", combined)
        self.assertIn("changed_paths", combined)

    def test_mutating_validation_cannot_certify_an_untested_candidate(self):
        candidate_text = "pub fn value() -> u32 { 1 }\n"
        read = "from pathlib import Path; p=Path('src/lib.rs'); "
        config = replace(
            self.config,
            checks=[
                [
                    sys.executable,
                    "-c",
                    read + f"assert p.read_text() in ({self.original!r}, {candidate_text!r})",
                ],
                [
                    sys.executable,
                    "-c",
                    read + f"p.write_text('BROKEN') if p.read_text()=={candidate_text!r} else None",
                ],
            ],
        )
        flow = Workflow(
            self.repo,
            "implement value",
            config,
            invoke_fn=self.invoke,
            process_fn=run_process,
            progress=lambda _: None,
        )
        flow.run()
        self.assertEqual(flow.state["status"], "failed")
        self.assertIn("results invalidated", flow.state["reason"])
        self.assertEqual([call["role"] for call in self.calls], ["implementation"])
        self.assertFalse((flow.run_dir / "candidate_manifest.json").exists())
        last = flow.state["checks"][-1]
        self.assertFalse(last["passed"])
        self.assertFalse(last["source_unchanged"])
        self.assertNotEqual(last["snapshot_id"], last["result_snapshot_id"])
        with self.assertRaisesRegex(WorkspaceError, "succeeded"):
            apply_run(flow.run_dir)

    def test_preparation_may_generate_files_before_validation_and_review(self):
        config = replace(
            self.config,
            prepare=[
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('generated.txt').write_text('ready')",
                ]
            ],
            checks=[
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('generated.txt').read_text()=='ready'",
                ]
            ],
        )
        flow = Workflow(
            self.repo,
            "implement value",
            config,
            invoke_fn=self.invoke,
            process_fn=run_process,
            progress=lambda _: None,
        )
        flow.run()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        self.assertFalse((self.repo / "generated.txt").exists())
        preparation = flow.state["baseline_preparation"][0]
        self.assertTrue(preparation["passed"])
        self.assertFalse(preparation["source_unchanged"])
        validations = [item for item in flow.state["checks"] if item["phase"] == "candidate-0"]
        self.assertTrue(validations)
        for item in validations:
            self.assertTrue(item["source_unchanged"])
            self.assertEqual(item["snapshot_id"], flow.state["candidate_id"])
            self.assertEqual(item["result_snapshot_id"], flow.state["candidate_id"])
        for call in self.calls:
            self.assertTrue((call["cwd"] / ".git").is_dir())

    def test_source_mutation_in_baseline_validation_stops_before_model_calls(self):
        def mutate(kwargs):
            (kwargs["cwd"] / "src/lib.rs").write_text("changed during validation")
            return ProcessResult(0, "", "", 0)

        self.check_script = [mutate]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "failed")
        self.assertFalse(state["checks"][0]["passed"])
        self.assertFalse(self.calls)

    def test_preparation_failure_uses_bounded_fixer_before_any_review(self):
        self.check_script = [
            ProcessResult(0, "baseline prepare", "", 0),
            ProcessResult(0, "baseline check", "", 0),
            ProcessResult(1, "", "generator failed", 0),
            ProcessResult(0, "candidate prepare", "", 0),
            ProcessResult(0, "candidate check", "", 0),
        ]
        _, state = self.run_workflow(config=replace(self.config, prepare=[["fixture-prepare"]]))
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["cycles"], 1)
        self.assertLess(self.events.index("fixer"), self.events.index("reviewer_a"))
        fixer = next(call for call in self.calls if call["role"] == "fixer")
        self.assertIn("generator failed", fixer["prompt"])

    def test_baseline_failure_never_waives_candidate_checks(self):
        self.check_script = [
            ProcessResult(1, "baseline failure", "", 0.01),
            ProcessResult(0, "candidate pass", "", 0.01),
        ]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "succeeded")
        self.assertFalse(state["baseline_checks"][0]["passed"])
        self.assertIn("baseline failure", self.calls[0]["prompt"])
        self.assertEqual(len(self.check_calls), 2)

    def test_max_cycles_zero_allows_clean_review_without_fixer(self):
        _, state = self.run_workflow(config=replace(self.config, max_cycles=0))
        self.assertEqual(state["status"], "succeeded")
        self.assertNotIn("fixer", self.events)

    def test_max_cycles_zero_failing_gate_stops_without_review_or_fixer(self):
        self.check_script = [ProcessResult(0, "", "", 0), ProcessResult(1, "", "bad", 0)]
        _, state = self.run_workflow(config=replace(self.config, max_cycles=0))
        self.assertEqual(state["status"], "limit_exhausted")
        self.assertEqual([call["role"] for call in self.calls], ["implementation"])

    def test_confirmed_blocking_findings_exhaust_fixer_limit(self):
        self.scripts["reviewer_a"] = [
            response(findings=[finding()]),
            response(findings=[finding()]),
        ]
        self.scripts["triage"] = [
            response(decisions=[decision()]),
            response(decisions=[decision()]),
        ]
        _, state = self.run_workflow(config=replace(self.config, max_cycles=1))
        self.assertEqual(state["status"], "limit_exhausted")
        self.assertEqual(state["cycles"], 1)
        self.assertEqual(self.events.count("fixer"), 1)
        self.assertEqual(len(state["reviews"]), 2)

    def test_fixer_without_changes_stops_as_no_progress(self):
        self.check_script = [ProcessResult(0, "", "", 0), ProcessResult(1, "", "bad", 0)]
        self.scripts["fixer"] = [response(summary="No source changes")]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "no_progress")
        self.assertEqual(state["cycles"], 1)

    def test_fixer_return_to_original_candidate_stops_as_no_progress(self):
        initial = "pub fn value() -> u32 { 1 }\n"

        def back_to_initial(call):
            (call["cwd"] / "src/lib.rs").write_text(initial)
            return response()

        def first_fix(call):
            (call["cwd"] / "src/lib.rs").write_text("pub fn value() -> u32 { 2 }\n")
            return response()

        self.scripts["fixer"] = [first_fix, back_to_initial]
        self.check_script = [ProcessResult(0, "", "", 0)] + [ProcessResult(1, "", "bad", 0)] * 3
        _, state = self.run_workflow(config=replace(self.config, max_cycles=3))
        self.assertEqual(state["status"], "no_progress")
        self.assertEqual(state["cycles"], 2)

    def test_unresolved_triage_blocks_without_fixer(self):
        self.scripts["reviewer_a"] = [response(findings=[finding()])]
        self.scripts["triage"] = [response(decisions=[decision(disposition="unresolved")])]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["reviews"][0]["unresolved"][0]["id"], "F1")
        self.assertNotIn("fixer", self.events)

    def test_invalid_duplicate_triage_fails_closed(self):
        self.scripts["reviewer_a"] = [response(findings=[finding()])]
        self.scripts["triage"] = [
            response(decisions=[decision(disposition="duplicate", duplicate_of="F1")])
        ]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "failed")
        self.assertIn("ProtocolError", state["reason"])
        self.assertNotIn("fixer", self.events)

    def test_rejected_findings_can_pass_but_low_confirmed_findings_remain_reported(self):
        for disposition, severity, expected in (("rejected", "high", 0), ("confirmed", "low", 1)):
            with self.subTest(disposition=disposition):
                self.calls.clear()
                self.scripts["reviewer_a"] = [response(findings=[finding(severity=severity)])]
                self.scripts["triage"] = [response(decisions=[decision(disposition=disposition)])]
                _, state = self.run_workflow()
                self.assertEqual(state["status"], "succeeded")
                self.assertEqual(len(state["remaining_findings"]), expected)

    def test_blocked_implementation_stops_before_candidate_validation(self):
        self.scripts["implementation"] = [
            response(status="blocked", summary="Need missing requirement")
        ]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "blocked")
        self.assertIn("Need missing requirement", state["reason"])
        self.assertEqual(self.events, ["check", "implementation"])

    def test_reviewer_snapshot_mutation_fails_and_never_retries(self):
        def mutate(call):
            (call["cwd"] / "src/lib.rs").write_text("modified by reviewer")
            raise BackendError("backend failed after modifying source")

        self.scripts["reviewer_a"] = [mutate, response()]
        _, state = self.run_workflow(config=replace(self.config, max_retries=1))
        self.assertEqual(state["status"], "failed")
        self.assertIn("changed its snapshot", state["reason"])
        self.assertEqual(self.events.count("reviewer_a"), 1)
        self.assertNotIn("reviewer_b", self.events)

    def test_successful_reviewer_mutation_also_fails(self):
        def mutate(call):
            (call["cwd"] / "src/lib.rs").write_text("modified by reviewer")
            return response()

        self.scripts["reviewer_a"] = [mutate]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "failed")
        self.assertIn("changed its snapshot", state["reason"])

    def test_agent_call_budget_stops_before_second_reviewer(self):
        _, state = self.run_workflow(config=replace(self.config, max_agent_calls=2))
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["agent_calls"], 2)
        self.assertNotIn("reviewer_b", self.events)

    def test_budget_prevents_fixer_without_counting_a_repair(self):
        self.check_script = [ProcessResult(0, "", "", 0), ProcessResult(1, "", "error", 0)]
        _, state = self.run_workflow(config=replace(self.config, max_agent_calls=1))
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["cycles"], 0)
        self.assertEqual(state["agent_calls"], 1)
        self.assertNotIn("fixer", self.events)

    def test_deleted_source_finding_reaches_triage(self):
        def delete_source(call):
            (call["cwd"] / "src/lib.rs").unlink()
            return response()

        self.scripts["implementation"] = [delete_source]
        self.scripts["reviewer_a"] = [response(findings=[finding()])]
        self.scripts["triage"] = [response(decisions=[decision()])]
        _, state = self.run_workflow(config=replace(self.config, max_cycles=0))
        self.assertEqual(state["status"], "limit_exhausted")
        self.assertIn("triage", self.events)

    def test_time_budget_is_checked_after_last_reviewer_before_success(self):
        current = [0.0]

        def slow_review(call):
            current[0] += 2
            return response()

        self.scripts["reviewer_b"] = [slow_review]
        with patch("bb_workflow.engine.time.monotonic", side_effect=lambda: current[0]):
            _, state = self.run_workflow(config=replace(self.config, max_seconds=1))
        self.assertEqual(state["status"], "budget_exhausted")

    def test_backend_timeouts_fail_and_gate_timeouts_do_not_pass(self):
        self.scripts["implementation"] = [BackendError("model timed out")]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "failed")
        self.assertIn("timed out", state["reason"])
        self.scripts.clear()
        self.check_script = [ProcessResult(0, "", "", 0), ProcessResult(0, "partial", "", 1, True)]
        _, state = self.run_workflow(config=replace(self.config, max_cycles=0))
        self.assertEqual(state["status"], "limit_exhausted")
        self.assertFalse(state["checks"][-1]["passed"])
        self.assertTrue(state["checks"][-1]["timed_out"])

    def test_output_limit_invalidates_zero_exit_without_spending_fixer_calls(self):
        self.check_script = [
            ProcessResult(0, "baseline", "", 0),
            ProcessResult(0, "truncated", "", 0, output_limit_exceeded=True),
        ]
        _, state = self.run_workflow(config=replace(self.config, max_output_bytes=1024))
        self.assertEqual(state["status"], "failed")
        self.assertIn("output limit exceeded (1024 bytes)", state["reason"])
        self.assertFalse(state["checks"][-1]["passed"])
        self.assertTrue(state["checks"][-1]["output_limit_exceeded"])
        self.assertEqual(state["cycles"], 0)
        self.assertEqual([call["role"] for call in self.calls], ["implementation"])
        self.assertTrue(all(call["max_output_bytes"] == 1024 for call in self.check_calls))
        self.assertEqual(self.calls[0]["max_output_bytes"], 1024)

    def test_real_output_overflow_stops_before_models(self):
        config = replace(
            self.config,
            max_output_bytes=1024,
            checks=[[sys.executable, "-c", "import sys; sys.stdout.write('x'*100000)"]],
        )
        flow = Workflow(
            self.repo,
            "implement value",
            config,
            invoke_fn=self.invoke,
            process_fn=run_process,
            progress=lambda _: None,
        )
        flow.run()
        self.assertEqual(flow.state["status"], "failed")
        self.assertIn("output limit exceeded", flow.state["reason"])
        self.assertFalse(self.calls)
        self.assertLessEqual((flow.run_dir / "checks/baseline/0/stdout.txt").stat().st_size, 1024)

    def test_read_only_backend_failure_retries_same_snapshot_once(self):
        self.scripts["reviewer_a"] = [BackendError("temporary backend failure"), response()]
        _, state = self.run_workflow(config=replace(self.config, max_retries=1))
        self.assertEqual(state["status"], "succeeded")
        calls = [call for call in self.calls if call["role"] == "reviewer_a"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["cwd"], calls[1]["cwd"])
        self.assertEqual(calls[0]["snapshot"], calls[1]["snapshot"])
        self.assertEqual(calls[0]["prompt"], calls[1]["prompt"])
        self.assertEqual(state["unknown_cost_calls"], 1)

    def test_writer_backend_failure_is_never_retried(self):
        self.scripts["implementation"] = [BackendError("writer failed"), response()]
        _, state = self.run_workflow(config=replace(self.config, max_retries=4))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.events.count("implementation"), 1)

    def test_fixer_backend_failure_is_never_retried(self):
        self.check_script = [ProcessResult(0, "", "", 0), ProcessResult(1, "", "bad", 0)]
        self.scripts["fixer"] = [BackendError("writer failed"), response()]
        _, state = self.run_workflow(config=replace(self.config, max_retries=4))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.events.count("fixer"), 1)
        self.assertEqual(state["cycles"], 1)

    def test_read_only_retry_limit_is_bounded(self):
        self.scripts["reviewer_a"] = [
            BackendError("first failure"),
            BackendError("second failure"),
            response(),
        ]
        _, state = self.run_workflow(config=replace(self.config, max_retries=1))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.events.count("reviewer_a"), 2)
        self.assertNotIn("reviewer_b", self.events)

    def test_triage_receives_both_review_findings_and_provenance_in_its_own_copy(self):
        self.scripts["reviewer_a"] = [response(findings=[finding("a", title="A finding")])]
        self.scripts["reviewer_b"] = [response(findings=[finding("b", title="B finding")])]
        self.scripts["triage"] = [
            response(decisions=[decision("F1", "rejected"), decision("F2", "rejected")])
        ]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "succeeded")
        first, second, triage = self.calls[1:]
        self.assertNotIn("A finding", second["prompt"])
        self.assertNotIn("B finding", first["prompt"])
        self.assertIn("reviewer_a:a", triage["prompt"])
        self.assertIn("reviewer_b:b", triage["prompt"])
        self.assertEqual(first["snapshot"], triage["snapshot"])
        self.assertEqual(len({first["cwd"], second["cwd"], triage["cwd"]}), 3)

    def test_invalid_review_contract_is_not_retried(self):
        self.scripts["reviewer_a"] = [{"malformed": True}, response()]
        _, state = self.run_workflow(config=replace(self.config, max_retries=4))
        self.assertEqual(state["status"], "failed")
        self.assertIn("ProtocolError", state["reason"])
        self.assertEqual(self.events.count("reviewer_a"), 1)
        self.assertEqual(state["agents"][-1]["status"], "failed")

    def test_keyboard_interrupt_records_agent_interruption_and_releases_lock(self):
        def interrupt(call):
            raise KeyboardInterrupt

        self.scripts["implementation"] = [interrupt]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["agents"][-1]["status"], "interrupted")
        self.assertEqual(state["unknown_cost_calls"], 1)
        self.assertEqual(state["token_usage"]["by_backend"]["claude"]["unknown_calls"], 1)

    def test_high_severity_duplicate_of_low_confirmed_finding_still_blocks(self):
        self.scripts["reviewer_a"] = [response(findings=[finding("a", severity="high")])]
        self.scripts["reviewer_b"] = [response(findings=[finding("b", severity="low")])]
        self.scripts["triage"] = [
            response(decisions=[decision("F1", "duplicate", "F2"), decision("F2", "confirmed")])
        ]
        _, state = self.run_workflow(config=replace(self.config, max_cycles=0))
        self.assertEqual(state["status"], "limit_exhausted")
        self.assertEqual(state["reviews"][0]["confirmed"][0]["severity"], "high")

    def test_missing_gate_executable_blocks_without_ai_calls(self):
        self.check_script = [FileNotFoundError("fixture not installed")]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "blocked")
        self.assertIn("Validation executable unavailable", state["reason"])
        self.assertFalse(self.calls)

    def test_model_routing_uses_complexity_profile_and_role_override(self):
        override = Selection("claude", "claude-opus-4-6", "high")
        config = replace(self.config, roles={"reviewer_b": override})
        task = "Fix unsafe memory safety and concurrency"
        level, selected = route(config, task)
        _, state = self.run_workflow(config=config, task=task)
        self.assertEqual(level, "high")
        self.assertEqual(state["complexity"], "high")
        for call in self.calls:
            self.assertEqual(call["selection"], selected[call["role"]])
            self.assertEqual(call["executable"], config.executables[call["selection"].backend])
            self.assertGreater(call["timeout"], 0)
            self.assertLessEqual(call["timeout"], config.timeout_seconds)
        self.assertEqual(state["routing"]["reviewer_b"], json.loads(json.dumps(asdict(override))))

    def test_failed_attempt_telemetry_is_counted_once_alongside_retry(self):
        usage = {
            "input_tokens": 12,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 3,
        }
        for cost in (0.0, 0.75):
            with self.subTest(cost=cost):
                self.scripts["reviewer_a"] = [
                    BackendError("temporary failure", cost_usd=cost, usage=usage),
                    response(),
                ]
                _, state = self.run_workflow(config=replace(self.config, max_retries=1))
                self.assertEqual(state["status"], "succeeded")
                self.assertEqual(state["agent_calls"], 4)
                self.assertAlmostEqual(state["known_cost_usd"], cost + 0.3)
                self.assertEqual(state["unknown_cost_calls"], 0)
                failed = state["agents"][1]
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["usage"], usage)
                self.assertEqual(failed["token_usage"]["total_tokens"], 15)
                totals = state["token_usage"]["by_backend"]["claude"]
                self.assertEqual(totals["known_totals"]["total_tokens"], 15)
                self.assertEqual(totals["missing_calls"]["total_tokens"], 2)

    def test_unexpected_provider_exception_keeps_cost_unknown(self):
        self.scripts["implementation"] = [RuntimeError("unexpected transport failure")]
        flow, state = self.run_workflow()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["agent_calls"], 1)
        self.assertEqual(state["unknown_cost_calls"], 1)
        self.assertIn("total unknown", (flow.run_dir / "report.md").read_text())

    def test_assets_for_later_roles_are_checked_before_any_provider_calls(self):
        for role in ("reviewer_a", "reviewer_b", "triage", "fixer"):
            for asset in ("missing_skill", "excluded_skill", "escaping_skill", "missing_mcp"):
                with self.subTest(role=role, asset=asset):
                    selection = self.config.profiles["medium"]["reviewer_a"]
                    name = "skills/review.md"
                    if asset == "missing_mcp":
                        selection = replace(selection, mcp_config="missing-mcp.json")
                    else:
                        if asset == "excluded_skill":
                            path = self.repo / name
                            path.parent.mkdir(exist_ok=True)
                            path.write_text("review instructions")
                        elif asset == "escaping_skill":
                            name = "../outside.md"
                            (self.repo.parent / "outside.md").write_text("outside instructions")
                        selection = replace(selection, skills=(name,))
                    config = replace(
                        self.config,
                        roles={role: selection},
                        exclude=(*self.config.exclude, "skills")
                        if asset == "excluded_skill"
                        else self.config.exclude,
                    )
                    _, state = self.run_workflow(config=config)
                    self.assertEqual(state["status"], "blocked")
                    self.assertEqual(state["agent_calls"], 0)
                    self.assertFalse(self.calls)
                    self.assertFalse(self.check_calls)
                    (self.repo / "skills/review.md").unlink(missing_ok=True)

    def test_skill_removed_after_preflight_is_checked_again_before_role(self):
        name = "review-skill.md"
        (self.repo / name).write_text("review instructions")
        selection = replace(self.config.profiles["medium"]["reviewer_a"], skills=(name,))

        def remove_skill(call):
            (call["cwd"] / name).unlink()
            return response()

        self.scripts["implementation"] = [remove_skill]
        _, state = self.run_workflow(config=replace(self.config, roles={"reviewer_a": selection}))
        self.assertEqual(state["status"], "blocked")
        self.assertEqual([call["role"] for call in self.calls], ["implementation"])

    def test_existing_role_assets_pass_preflight_and_resolve_mcp_from_original_project(self):
        (self.repo / "review-skill.md").write_text("review instructions")
        (self.repo / "review-mcp.json").write_text('{"mcpServers": {}}')
        selection = replace(
            self.config.profiles["medium"]["reviewer_a"],
            skills=("review-skill.md",),
            mcp_config="review-mcp.json",
        )
        _, state = self.run_workflow(config=replace(self.config, roles={"reviewer_a": selection}))
        self.assertEqual(state["status"], "succeeded")
        call = next(call for call in self.calls if call["role"] == "reviewer_a")
        self.assertEqual(call["selection"].mcp_config, str(self.repo / "review-mcp.json"))
        self.assertIn('"path":"review-skill.md"', call["prompt"])

    def test_fixer_receives_baseline_for_deleted_file_and_duplicate_evidence(self):
        def delete_source(call):
            (call["cwd"] / "src/lib.rs").unlink()
            return response()

        self.scripts["implementation"] = [delete_source]
        self.scripts["reviewer_a"] = [response(findings=[finding("a", severity="low")])]
        duplicate = {**finding("b", severity="high"), "evidence": "Deleted public API is required"}
        self.scripts["reviewer_b"] = [response(findings=[duplicate])]
        self.scripts["triage"] = [
            response(decisions=[decision("F1", "confirmed"), decision("F2", "duplicate", "F1")])
        ]

        def restore_source(call):
            evidence = json.loads(call["prompt"].split("Task and evidence (JSON):\n")[1])[
                "evidence"
            ]
            baseline = Path(evidence["baseline_directory"])
            self.assertEqual((baseline / "src/lib.rs").read_text(), self.original)
            confirmed = evidence["confirmed_findings"][0]
            self.assertEqual(confirmed["severity"], "high")
            self.assertEqual(confirmed["duplicates"][0]["evidence"], duplicate["evidence"])
            self.assertEqual(confirmed["duplicates"][0]["sources"], ["reviewer_b:b"])
            (call["cwd"] / "src/lib.rs").write_text(self.original)
            return response()

        self.scripts["fixer"] = [restore_source]
        _, state = self.run_workflow()
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["cycles"], 1)

    def test_nonretryable_backend_failure_stops_without_spending_retry_budget(self):
        self.scripts["reviewer_a"] = [
            BackendError("Backend model drift", retryable=False),
            response(),
        ]
        _, state = self.run_workflow(config=replace(self.config, max_retries=3))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["agent_calls"], 2)
        self.assertEqual(self.events.count("reviewer_a"), 1)
        self.assertFalse(state["agents"][-1]["retryable"])
        self.assertNotIn("reviewer_b", self.events)

    def test_candidate_change_during_finding_triage_fails_before_repair(self):
        def mutate_candidate(call):
            (flow.work / "src/lib.rs").write_text("External change during triage\n")
            return response(decisions=[decision("F1", "confirmed")])

        self.scripts["reviewer_a"] = [response(findings=[finding()])]
        self.scripts["triage"] = [mutate_candidate]
        flow = self.workflow()
        flow.run()
        self.assertEqual(flow.state["status"], "failed", flow.state["reason"])
        self.assertIn("changed during triage", flow.state["reason"])
        self.assertEqual(flow.state["cycles"], 0)
        self.assertEqual(
            self.events, ["check", "implementation", "check", "reviewer_a", "reviewer_b", "triage"]
        )


if __name__ == "__main__":
    unittest.main()
