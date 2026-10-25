"""Rust review obligations run through real snapshots, criteria and local apply replay."""

import contextlib
import io
import json
import unittest
from copy import deepcopy
from dataclasses import replace

import test_acceptance

from bb_workflow.cli import main
from bb_workflow.config import (
    RustReviewConfig,
    RustReviewEvidence,
    default_text,
    load_config,
)
from bb_workflow.process import ProcessResult
from bb_workflow.requirements import Criterion, Requirements, RequirementsError
from bb_workflow.rust_review import prepare_rust_review, validate_rust_review
from bb_workflow.verification import PlannedCheck, VerificationPlan
from bb_workflow.workspace import WorkspaceError, apply_run, write_json


class RustReviewTests(unittest.TestCase):
    setUp = test_acceptance.AcceptanceTests.setUp
    invoke = test_acceptance.AcceptanceTests.invoke
    process = test_acceptance.AcceptanceTests.process
    workflow = test_acceptance.AcceptanceTests.workflow
    run_flow = test_acceptance.AcceptanceTests.run_flow

    def enable(self, kind=None):
        text = default_text() + "\n[rust_review]\nenabled = true\n"
        if kind:
            text += (
                f'required = ["{kind}"]\n[[rust_review.evidence]]\n'
                f'kind = "{kind}"\ncheck_id = "0"\ntest_name = "safety::boundary"\n'
            )
        path = self.repo / "review.toml"
        path.write_text(text, encoding="utf-8")
        try:
            loaded = load_config(path)
        except ValueError as exc:
            self.fail(f"Rust review policy must be supported: {exc}")
        self.config = replace(self.config, rust_review=loaded.rust_review)
        if kind:
            command = (
                ["cargo", "+nightly", "miri", "test"] if kind == "memory" else ["cargo", "test"]
            )
            self.config.checks = [command]

    def proof(self, name="safety::boundary", count=1):
        return ProcessResult(
            0,
            f"running {count} tests\n"
            + (f"test {name} ... ok\n" if count else "")
            + f"test result: ok. {count} passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n",
            "",
            0,
        )

    def test_both_reviewers_receive_adapter_and_identical_frozen_obligations(self):
        self.enable()
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        self.assertEqual(
            self.events, ["check", "implementation", "check", "reviewer_a", "reviewer_b"]
        )
        reviewers = self.calls[1:]
        for call in reviewers:
            self.assertIn("Rust PR review adapter, policy version 1.", call["prompt"])
            self.assertIn("Specification: separately compare task", call["prompt"])
        payloads = [
            json.loads(c["prompt"].split("Task and evidence (JSON):\n", 1)[1]) for c in reviewers
        ]
        self.assertIn("rust_review", payloads[0])
        self.assertEqual(payloads[0]["rust_review"], payloads[1]["rust_review"])
        self.assertEqual(payloads[0]["acceptance"], payloads[1]["acceptance"])
        self.assertNotEqual(reviewers[0]["cwd"], reviewers[1]["cwd"])
        self.assertNotIn("reviewer_a private summary", reviewers[1]["prompt"])
        ids = {c["id"] for c in payloads[0]["acceptance"]["requirements"]["criteria"]}
        self.assertTrue(
            {"scope", "rust-review-scope", "rust-review-memory", "rust-review-sync"} <= ids
        )
        self.assertEqual(apply_run(flow.run_dir), ["lib.rs"])

    def test_missing_required_execution_cannot_be_replaced_by_zero_findings(self):
        self.enable("memory")
        self.process_script = [self.proof(), ProcessResult(0, "", "", 0)]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "blocked", flow.state["reason"])
        self.assertNotIn("reviewer_a", self.events)
        self.assertNotIn("acceptance_report", flow.state)
        with self.assertRaises(WorkspaceError):
            apply_run(flow.run_dir)

    def test_zero_or_wrong_required_test_never_reaches_review(self):
        self.enable("memory")
        self.config.max_cycles = 0
        for result in (self.proof(count=0), self.proof(name="unrelated")):
            with self.subTest(result=result.stdout):
                self.events.clear()
                self.process_script = [self.proof(), result]
                flow = self.run_flow()
                self.assertEqual(flow.state["status"], "limit_exhausted", flow.state["reason"])
                self.assertNotIn("reviewer_a", self.events)
                with self.assertRaises(WorkspaceError):
                    apply_run(flow.run_dir)

    def test_summary_cannot_replace_required_semantic_assessments(self):
        self.enable()

        def omit(response, call):
            response["summary"] = "No findings; safety reviewed"
            response["criterion_assessments"] = [
                a
                for a in response["criterion_assessments"]
                if not a["criterion_id"].startswith("rust-review-")
            ]

        self.scripts["reviewer_a"] = [omit]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "failed")
        self.assertIn("ProtocolError", flow.state["reason"])
        with self.assertRaises(WorkspaceError):
            apply_run(flow.run_dir)

    def test_triage_cannot_waive_unresolved_safety_evidence(self):
        self.enable()

        def missing(response, call):
            for item in response["criterion_assessments"]:
                if item["criterion_id"] == "rust-review-memory":
                    item.update(verdict="unresolved", evidence="Aliasing argument unavailable")

        self.scripts["reviewer_b"] = [missing]
        # The fixture triage deliberately says satisfied for every dispute.
        flow = self.run_flow()
        self.assertEqual(self.events[-1], "triage")
        self.assertEqual(flow.state["status"], "blocked", flow.state["reason"])
        self.assertNotIn("acceptance_report", flow.state)
        with self.assertRaises(WorkspaceError):
            apply_run(flow.run_dir)

    def test_positive_miri_proof_is_replayed_before_apply(self):
        self.enable("memory")
        self.process_script = [self.proof(), self.proof()]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        record = flow.state["checks"][-1]
        path = flow.run_dir / record["artifacts"] / "stdout.txt"
        original = path.read_bytes()
        path.write_text("No tests executed\n")
        with self.assertRaises(WorkspaceError):
            apply_run(flow.run_dir)
        self.assertFalse((flow.run_dir / "apply-backup").exists())
        path.write_bytes(original)
        self.assertEqual(apply_run(flow.run_dir), ["lib.rs"])

    def test_saved_policy_cannot_be_disabled_to_drop_safety_contract(self):
        self.enable()
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        state = deepcopy(flow.state)
        state["config"].pop("rust_review")
        write_json(flow.run_dir / "state.json", state)
        with self.assertRaises(WorkspaceError):
            apply_run(flow.run_dir)

    def test_ordinary_cargo_test_cannot_satisfy_memory_policy(self):
        self.enable("memory")
        self.config.checks = [["cargo", "test"]]
        flow = self.run_flow()
        self.assertNotEqual(flow.state["status"], "succeeded")
        self.assertEqual(self.events, [])

    def test_plan_previews_generated_obligations_without_provider(self):
        self.enable()
        path = self.repo / "requirements.toml"
        path.write_text(
            'requirements_version = 1\n[[criteria]]\nid = "spec"\ndescription = "Implement value"\nkind = "semantic"\nverification = "Read source"\n'
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "plan",
                    "--repo",
                    str(self.repo),
                    "--config",
                    str(self.repo / "review.toml"),
                    "--requirements",
                    str(path),
                    "Implement value",
                ]
            )
        self.assertEqual(code, 0)
        preview = json.loads(output.getvalue())
        self.assertIn("rust_review", preview)
        self.assertTrue(preview["rust_review"]["enabled"])
        self.assertIn("rust-review-scope", [c["id"] for c in preview["requirements"]["criteria"]])
        self.assertEqual(self.calls, [])

    def test_missing_task_requirements_blocks_even_in_legacy_mode(self):
        self.enable()
        self.config.config_version = 2
        self.requirements = None
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "blocked")
        self.assertEqual(self.events, [])

    def test_invalid_programmatic_policy_blocks_before_legacy_execution(self):
        self.config.config_version = 2
        self.requirements = None
        self.config.rust_review = RustReviewConfig(
            False, 1, ("memory",), (RustReviewEvidence("memory", "0", "safety::boundary"),)
        )
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "failed")
        self.assertEqual(self.events, [])
        self.assertEqual(self.calls, [])
        self.assertIn("disabled rust_review", flow.state["reason"])

    def test_unknown_planned_check_blocks_before_any_execution(self):
        self.enable("memory")
        self.config.rust_review = replace(
            self.config.rust_review,
            evidence=(RustReviewEvidence("memory", "missing", "safety::boundary"),),
        )
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "failed")
        self.assertIn("unknown check_id", flow.state["reason"])
        self.assertEqual(self.events, [])

    def test_all_concurrency_kinds_require_named_current_execution(self):
        for kind in ("deadlocks", "cancellation", "task_lifecycle", "loom"):
            with self.subTest(kind=kind):
                self.enable(kind)
                self.process_script = [self.proof(), self.proof()]
                flow = self.run_flow()
                self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
                final = json.loads((flow.run_dir / "acceptance.json").read_text())
                criterion = next(c for c in final["criteria"] if c["id"] == "rust-review-test-0")
                self.assertEqual(criterion["kind"], "mechanical")
                self.assertEqual(criterion["test_name"], "safety::boundary")
                self.assertEqual(criterion["binding"]["candidate_id"], flow.state["candidate_id"])

    def test_fix_runs_full_gate_before_both_independent_reviews(self):
        self.enable("memory")
        self.process_script = [self.proof(), self.proof(count=0), self.proof()]
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        self.assertEqual(
            self.events,
            ["check", "implementation", "check", "fixer", "check", "reviewer_a", "reviewer_b"],
        )
        self.assertEqual(flow.state["cycles"], 1)
        self.assertEqual(
            [r["mechanical"][-1]["verdict"] for r in flow.state["acceptance"]["rounds"]],
            ["unmet", "satisfied"],
        )

    def test_historical_disabled_policy_can_replay_without_new_config_key(self):
        flow = self.run_flow()
        self.assertEqual(flow.state["status"], "succeeded", flow.state["reason"])
        state = deepcopy(flow.state)
        state["config"].pop("rust_review")
        write_json(flow.run_dir / "state.json", state)
        self.assertEqual(apply_run(flow.run_dir), ["lib.rs"])


class RustReviewPolicyTests(unittest.TestCase):
    def setUp(self):
        self.user = Requirements(criteria=(Criterion("task", "Task", "semantic", "Inspect task"),))
        self.plan = VerificationPlan(
            "gate",
            ("default",),
            None,
            None,
            (PlannedCheck("0", "gate", "default", ("cargo", "+nightly", "miri", "test")),),
        )
        self.policy = RustReviewConfig(
            True, 1, ("memory",), (RustReviewEvidence("memory", "0", "safety::boundary"),)
        )

    def test_expansion_keeps_user_contract_and_replay_rejects_changed_obligations(self):
        prepared = prepare_rust_review(self.user, self.policy, self.plan)
        self.assertEqual(prepared.criteria[0], self.user.criteria[0])
        self.assertEqual(
            self.user.criteria, (Criterion("task", "Task", "semantic", "Inspect task"),)
        )
        validate_rust_review(prepared, self.policy, self.plan)
        variants = (
            replace(prepared, criteria=prepared.criteria[:-1]),
            replace(
                prepared,
                criteria=(
                    *prepared.criteria[:-1],
                    replace(prepared.criteria[-1], test_name="other"),
                ),
            ),
            replace(prepared, criteria=prepared.criteria[1:]),
            replace(prepared, criteria=tuple(reversed(prepared.criteria))),
        )
        for modified in variants:
            with self.subTest(modified=modified), self.assertRaises(RequirementsError):
                validate_rust_review(modified, self.policy, self.plan)

    def test_authored_reserved_criteria_cannot_override_generated_obligations(self):
        authored = replace(
            self.user, criteria=(replace(self.user.criteria[0], id="rust-review-memory"),)
        )
        with self.assertRaisesRegex(RequirementsError, "reserved"):
            prepare_rust_review(authored, self.policy, self.plan)

    def test_wrappers_nextest_and_miri_setup_cannot_supply_required_memory_evidence(self):
        for command in (
            ("cargo", "test"),
            ("cargo", "miri", "setup"),
            ("cargo", "nextest", "run"),
            ("bash", "gates.sh"),
        ):
            plan = replace(self.plan, checks=(replace(self.plan.checks[0], argv=command),))
            with self.subTest(command=command), self.assertRaises(RequirementsError):
                prepare_rust_review(self.user, self.policy, plan)

    def test_disabled_policy_preserves_existing_requirement_identity(self):
        self.assertIs(prepare_rust_review(self.user, RustReviewConfig(), self.plan), self.user)
        validate_rust_review(self.user, RustReviewConfig(), self.plan)
