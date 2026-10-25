"""Candidate-bound acceptance through the real workflow with offline provider fixtures."""

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bb_workflow.backends import BackendResult
from bb_workflow.cli import main
from bb_workflow.config import load_config
from bb_workflow.engine import Workflow
from bb_workflow.process import ProcessResult
from bb_workflow.requirements import Criterion, Requirements, load_requirements
from bb_workflow.workspace import manifest, snapshot_id


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / "project"
        self.repo.mkdir()
        (self.repo / "lib.rs").write_text("pub fn value() -> u32 { 0 }\n")
        self.config = replace(
            load_config(),
            config_version=3,
            prepare=[],
            checks=[["fixture-check"]],
            max_cycles=2,
            max_seconds=60,
        )
        self.requirements = Requirements(
            criteria=(
                Criterion("gate", "Check works", "mechanical", "Run the gate", "0"),
                Criterion("scope", "Keep API", "semantic", "Compare exported signatures"),
            )
        )
        self.calls = []
        self.events = []
        self.scripts = {}
        self.process_script = []

    def invoke(self, **call):
        role = call["role"]
        self.events.append(role)
        self.calls.append(call)
        payload = json.loads(call["prompt"].split("Task and evidence (JSON):\n", 1)[1])
        # These files must already be durable before even the implementation starts.
        root = call["artifact_dir"].parents[1]
        self.assertTrue((root / "requirements.json").is_file())
        self.assertTrue((root / "verification_plan.json").is_file())
        response = {
            "status": "completed",
            "summary": role + " private summary",
            "findings": [],
            "decisions": [],
            "schema_version": 2,
            "binding": payload["acceptance"]["binding"],
            "criterion_evidence": [],
            "criterion_assessments": [],
            "criterion_decisions": [],
        }
        if role in {"implementation", "fixer"}:
            self.assertEqual(
                response["binding"]["candidate_id"], snapshot_id(manifest(call["cwd"]))
            )
            (call["cwd"] / "lib.rs").write_text(f"pub fn value() -> u32 {{ {len(self.calls)} }}\n")
        if role in {"reviewer_a", "reviewer_b"}:
            response["criterion_assessments"] = [
                {"criterion_id": item["id"], "verdict": "satisfied", "evidence": "API inspected"}
                for item in payload["acceptance"]["requirements"]["criteria"]
                if item["kind"] == "semantic"
            ]
        if role == "triage":
            response["criterion_decisions"] = [
                {
                    "criterion_id": identifier,
                    "verdict": "satisfied",
                    "rationale": "Source inspected",
                }
                for identifier in payload["evidence"]["disputed_criteria"]
            ]
        actions = self.scripts.get(role, [])
        if actions:
            actions.pop(0)(response, call)
        return BackendResult(response, None, {}, call["selection"].model)

    def process(self, argv, **kwargs):
        self.events.append("check")
        if self.process_script:
            action = self.process_script.pop(0)
            if callable(action):
                return action(argv, kwargs)
            if isinstance(action, BaseException):
                raise action
            return action
        return ProcessResult(0, "Check passed", "", 0)

    def workflow(self, **kwargs):
        return Workflow(
            self.repo,
            "Implement value",
            self.config,
            requirements=self.requirements,
            invoke_fn=self.invoke,
            process_fn=self.process,
            progress=lambda text: None,
            **kwargs,
        )

    def run_flow(self):
        flow = self.workflow()
        flow.run()
        self.assertEqual(
            json.loads((flow.run_dir / "state.json").read_text()),
            json.loads(json.dumps(flow.state)),
        )
        return flow

    def test_v3_requires_criteria_before_any_check_or_provider(self):
        flow = Workflow(
            self.repo,
            "Task",
            self.config,
            invoke_fn=self.invoke,
            process_fn=self.process,
            progress=lambda text: None,
        )
        flow.run()
        self.assertEqual(flow.state["status"], "blocked")
        self.assertIn("requirements", flow.state["reason"].lower())
        self.assertEqual(self.events, [])

    def test_bound_success_persists_inputs_and_independent_current_assessments(self):
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        acceptance = flow.state["acceptance"]
        self.assertEqual(acceptance["mode"], "criteria")
        self.assertFalse(acceptance["compatibility_mode"])
        round_record = acceptance["rounds"][0]
        self.assertEqual(round_record["mechanical"][0]["verdict"], "satisfied")
        self.assertEqual(round_record["binding"]["candidate_id"], flow.state["candidate_id"])
        first, second = self.calls[1:]
        first_payload = json.loads(first["prompt"].split("Task and evidence (JSON):\n")[1])
        second_payload = json.loads(second["prompt"].split("Task and evidence (JSON):\n")[1])
        self.assertEqual(first_payload["acceptance"], second_payload["acceptance"])
        self.assertEqual(first_payload["evidence"], second_payload["evidence"])
        self.assertNotIn("reviewer_a private summary", second["prompt"])
        self.assertNotEqual(first["cwd"], second["cwd"])
        self.assertEqual(
            self.events, ["check", "implementation", "check", "reviewer_a", "reviewer_b"]
        )
        self.assertEqual(first["schema"]["properties"]["schema_version"]["enum"], [2])
        self.assertIn("criteria", (flow.run_dir / "report.md").read_text())

    def test_old_config_explicitly_opts_in_with_requirements(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.config.config_version = version
                flow = self.run_flow()
                self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
                self.assertEqual(flow.state["acceptance"]["mode"], "criteria")

    def test_bad_binding_or_missing_unknown_duplicate_assessment_cannot_succeed(self):
        def stale(response, call):
            response["binding"]["requirements_id"] = "stale"

        def missing(response, call):
            response["criterion_assessments"] = []

        def unknown(response, call):
            response["criterion_assessments"][0]["criterion_id"] = "unknown"

        def duplicate(response, call):
            response["criterion_assessments"] *= 2

        for mutate in (stale, missing, unknown, duplicate):
            with self.subTest(mutate=mutate.__name__):
                self.scripts["reviewer_a"] = [mutate]
                flow = self.run_flow()
                self.assertEqual(flow.state["status"], "failed")
                self.assertIn("ProtocolError", flow.state["reason"])

    def test_writer_self_approval_is_rejected(self):
        def approve(response, call):
            response["criterion_assessments"] = [
                {"criterion_id": "scope", "verdict": "satisfied", "evidence": "I approve"}
            ]

        self.scripts["implementation"] = [approve]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "failed")
        self.assertNotIn("reviewer_a", self.events)

    def test_writer_hints_remain_proposals_with_pre_call_binding(self):
        def hint(response, call):
            response["criterion_evidence"] = [{"criterion_id": "scope", "evidence": "Look here"}]

        self.scripts["implementation"] = [hint]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        writer = flow.state["agents"][0]
        self.assertEqual(writer["criterion_evidence"][0]["evidence"], "Look here")
        self.assertNotEqual(writer["binding"]["candidate_id"], flow.state["candidate_id"])

    def test_semantic_only_dispute_reaches_triage_without_findings(self):
        def unmet(response, call):
            response["criterion_assessments"][0]["verdict"] = "unmet"

        self.scripts["reviewer_a"] = [unmet]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        self.assertIn("triage", self.events)
        triage = self.calls[-1]
        payload = json.loads(triage["prompt"].split("Task and evidence (JSON):\n")[1])
        self.assertEqual(payload["evidence"]["findings"], [])
        self.assertEqual(payload["evidence"]["disputed_criteria"], ["scope"])
        self.assertEqual(len(payload["evidence"]["criterion_assessments"]), 2)

    def test_unresolved_semantic_triage_blocks(self):
        def unresolved(response, call):
            response["criterion_assessments"][0]["verdict"] = "unresolved"

        def decide(response, call):
            response["criterion_decisions"][0]["verdict"] = "unresolved"

        self.scripts["reviewer_a"] = [unresolved]
        self.scripts["triage"] = [decide]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "blocked")
        self.assertEqual(self.events[-1], "triage")

    def test_unmet_semantic_repairs_even_without_blocking_severities_and_repeats_reviews(self):
        self.config.blocking_severities = ()

        def unmet(response, call):
            response["criterion_assessments"][0]["verdict"] = "unmet"

        def decide(response, call):
            response["criterion_decisions"][0]["verdict"] = "unmet"

        self.scripts["reviewer_a"] = [unmet]
        self.scripts["reviewer_b"] = [unmet]
        self.scripts["triage"] = [decide]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        self.assertEqual(
            self.events,
            [
                "check",
                "implementation",
                "check",
                "reviewer_a",
                "reviewer_b",
                "triage",
                "fixer",
                "check",
                "reviewer_a",
                "reviewer_b",
            ],
        )
        rounds = flow.state["acceptance"]["rounds"]
        self.assertEqual(len(rounds), 2)
        self.assertNotEqual(rounds[0]["binding"], rounds[1]["binding"])

    def test_missing_triage_criterion_decision_does_not_downgrade(self):
        def unmet(response, call):
            response["criterion_assessments"][0]["verdict"] = "unmet"

        def omit(response, call):
            response["criterion_decisions"] = []

        self.scripts["reviewer_a"] = [unmet]
        self.scripts["triage"] = [omit]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "failed")
        self.assertIn("ProtocolError", flow.state["reason"])

    def test_frozen_config_prevents_caller_gate_and_exclude_mutation(self):
        flow = self.workflow()
        self.config.checks[:] = [["different-command"]]
        self.config.exclude = ("lib.rs",)
        self.config.max_agent_calls = 1
        flow.run()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        self.assertEqual(
            [item["command"] for item in flow.state["checks"]], [["fixture-check"]] * 2
        )
        self.assertIn("lib.rs", flow.state["changed_paths"])

    def test_requirements_file_edits_do_not_replace_parsed_spec(self):
        path = self.repo / "criteria.toml"
        path.write_text(
            'requirements_version = 1\n[[criteria]]\nid = "scope"\n'
            'description = "Keep API"\nkind = "semantic"\nverification = "Read API"\n'
        )
        self.requirements = load_requirements(path)

        def edit(response, call):
            (call["cwd"] / "criteria.toml").write_text("invalid replacement")
            path.write_text("invalid replacement")

        self.scripts["implementation"] = [edit]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        stored = json.loads((flow.run_dir / "requirements.json").read_text())
        self.assertEqual(stored["criteria"][0]["description"], "Keep API")

    def test_mechanical_zero_runs_repairs_before_reviews_and_cannot_be_overridden(self):
        self.config.checks = [["cargo", "test", "missing"]]
        self.config.max_cycles = 1
        zero = ProcessResult(
            0,
            "running 0 tests\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 1 filtered out\n",
            "",
            0,
        )
        self.process_script = [zero] * 3
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "limit_exhausted", flow.state["reason"])
        self.assertEqual(self.events, ["check", "implementation", "check", "fixer", "check"])
        self.assertEqual(
            [r["mechanical"][0]["verdict"] for r in flow.state["acceptance"]["rounds"]],
            ["unmet", "unmet"],
        )

    def test_unknown_harness_proof_blocks_without_fixer_or_review(self):
        self.config.checks = [["cargo", "test"]]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "blocked", flow.state["reason"])
        self.assertEqual(self.events, ["check", "implementation", "check"])
        self.assertEqual(
            flow.state["acceptance"]["rounds"][0]["mechanical"][0]["verdict"], "unresolved"
        )

    def test_failed_gate_records_mechanical_before_fixer(self):
        self.process_script = [ProcessResult(0, "", "", 0), ProcessResult(1, "", "bad", 0)]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        self.assertEqual(flow.state["acceptance"]["rounds"][0]["mechanical"][0]["verdict"], "unmet")
        self.assertLess(self.events.index("fixer"), self.events.index("reviewer_a"))

    def test_source_mutating_check_preserves_fatal_status_and_partial_audit(self):
        def mutate(argv, kwargs):
            (kwargs["cwd"] / "lib.rs").write_text("Changed during gate")
            return ProcessResult(0, "", "", 0)

        self.process_script = [ProcessResult(0, "", "", 0), mutate]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "failed")
        self.assertIn("changed source", flow.state["reason"])
        self.assertNotIn("fixer", self.events)
        self.assertEqual(len(flow.state["acceptance"]["rounds"]), 1)

    def test_mechanical_only_criteria_still_use_v2_and_empty_semantic_arrays(self):
        self.requirements = Requirements(criteria=self.requirements.criteria[:1])
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        self.assertEqual(flow.state["acceptance"]["rounds"][0]["semantic"], [])

    def test_cli_parses_once_and_runs_current_criteria(self):
        path = self.repo / "requirements.toml"
        path.write_text(
            'requirements_version = 1\n[[criteria]]\nid = "scope"\n'
            'description = "Keep API"\nkind = "semantic"\nverification = "Read API"\n'
        )

        def workflow(*args, **kwargs):
            return Workflow(*args, **kwargs, invoke_fn=self.invoke, process_fn=self.process)

        output = io.StringIO()
        with (
            patch("bb_workflow.cli._config", return_value=self.config),
            patch("bb_workflow.cli.Workflow", side_effect=workflow),
            patch("bb_workflow.cli.load_requirements", wraps=load_requirements) as loader,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = main(["run", "Task", "--repo", str(self.repo), "--requirements", str(path)])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "succeeded")
        self.assertEqual(loader.call_count, 1)

    def test_plan_with_criteria_binds_checks_without_providers(self):
        path = self.repo / "requirements.toml"
        path.write_text(
            'requirements_version = 1\n[[criteria]]\nid = "gate"\n'
            'description = "Gate"\nkind = "mechanical"\nverification = "Run"\n'
            'check_id = "0"\n'
        )
        for version in (1, 2, 3):
            self.config.config_version = version
            output = io.StringIO()
            with (
                patch("bb_workflow.cli._config", return_value=self.config),
                contextlib.redirect_stdout(output),
            ):
                code = main(["plan", "Task", "--requirements", str(path)])
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["compatibility_mode"])
            self.assertFalse(payload["requirements_needed"])
            self.assertEqual(payload["acceptance_mode"], "criteria")
            self.assertEqual(len(payload["requirements_id"]), 64)
        self.assertEqual(self.events, [])

    def test_invalid_programmatic_contract_never_downgrades_legacy_configuration(self):
        self.config.config_version = 1
        self.requirements = Requirements()
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "failed")
        self.assertIn("RequirementsError", flow.state["reason"])
        self.assertEqual(self.events, [])
        self.assertFalse(flow.state["acceptance"]["compatibility_mode"])

    def test_missing_named_test_is_repaired_then_proved_at_new_binding(self):
        self.config.checks = [["cargo", "test", "regression"]]
        self.requirements = Requirements(
            criteria=(
                Criterion(
                    "regression", "Fix behavior", "mechanical", "Run exact test", "0", "regression"
                ),
            )
        )
        zero = ProcessResult(
            0,
            "running 0 tests\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 1 filtered out\n",
            "",
            0,
        )
        passing = ProcessResult(
            0,
            "running 1 test\ntest regression ... ok\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n",
            "",
            0,
        )
        self.process_script = [zero, zero, passing]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        self.assertEqual(
            self.events,
            ["check", "implementation", "check", "fixer", "check", "reviewer_a", "reviewer_b"],
        )
        rounds = flow.state["acceptance"]["rounds"]
        self.assertEqual([r["mechanical"][0]["verdict"] for r in rounds], ["unmet", "satisfied"])
        self.assertNotEqual(rounds[0]["binding"], rounds[1]["binding"])

    def test_interrupted_gate_preserves_status_and_partial_mechanical_record(self):
        self.process_script = [ProcessResult(0, "", "", 0), KeyboardInterrupt()]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "interrupted")
        self.assertEqual(len(flow.state["acceptance"]["rounds"][0]["mechanical"]), 1)
        self.assertNotIn("fixer", self.events)

    def test_expired_gate_budget_preserves_status_and_partial_mechanical_record(self):
        flow = self.workflow()

        def expire(argv, kwargs):
            flow.started -= 1000
            return ProcessResult(0, "", "", 0)

        self.process_script = [ProcessResult(0, "", "", 0), expire]
        flow.run()
        self.assertEqual(flow.state["status"], "budget_exhausted", flow.state["reason"])
        self.assertEqual(len(flow.state["acceptance"]["rounds"][0]["mechanical"]), 1)
        self.assertNotIn("fixer", self.events)

    def test_unresolved_mechanical_proof_blocks_even_with_another_failed_check(self):
        self.config.checks = [["fixture-check"], ["cargo", "test"]]
        self.requirements = Requirements(
            criteria=(
                *self.requirements.criteria,
                Criterion("tests", "Tests execute", "mechanical", "Run tests", "1"),
            )
        )
        failed = ProcessResult(1, "", "Check failed", 0)
        unknown = ProcessResult(0, "Unrecognized custom harness output", "", 0)
        self.process_script = [failed, unknown, failed, unknown]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "blocked", flow.state["reason"])
        self.assertNotIn("fixer", self.events)
        self.assertEqual(
            [item["verdict"] for item in flow.state["acceptance"]["rounds"][0]["mechanical"]],
            ["unmet", "unresolved"],
        )

    def test_candidate_change_during_semantic_triage_fails_before_repair(self):
        def unmet(response, call):
            response["criterion_assessments"][0]["verdict"] = "unmet"

        def mutate_candidate(response, call):
            (flow.work / "lib.rs").write_text("External change during triage\n")
            response["criterion_decisions"][0]["verdict"] = "unmet"

        self.scripts["reviewer_a"] = [unmet]
        self.scripts["triage"] = [mutate_candidate]
        flow = self.workflow()
        flow.run()
        self.assertEqual(flow.state["status"], "failed", flow.state["reason"])
        self.assertIn("changed during triage", flow.state["reason"])
        self.assertEqual(flow.state["cycles"], 0)
        self.assertEqual(len(flow.state["acceptance"]["rounds"]), 1)
        self.assertEqual(
            self.events, ["check", "implementation", "check", "reviewer_a", "reviewer_b", "triage"]
        )
