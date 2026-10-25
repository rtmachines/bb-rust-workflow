"""Verification plans, bounded execution and standalone audit contracts."""

import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from bb_workflow import verification
from bb_workflow.config import (
    DEFAULT_CHECKS,
    Config,
    ConfigError,
    VerificationProfile,
    default_text,
    load_config,
)
from bb_workflow.process import ProcessResult
from bb_workflow.workspace import WorkspaceError, apply_run, manifest


class PlanningTests(unittest.TestCase):
    def config(self, text):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workflow.toml"
            path.write_text(default_text() + text, encoding="utf-8")
            return load_config(path)

    def test_default_profiles_preserve_order_and_runtime_gate_checks(self):
        config = Config()
        expected = {"fast": [0, 1], "focused": [1, 2], "lint": [4], "gate": list(range(5))}
        for profile, indexes in expected.items():
            plan = verification.plan_verification(config, profile)
            self.assertEqual(
                [check.argv for check in plan.checks], [DEFAULT_CHECKS[i] for i in indexes]
            )
            self.assertEqual(
                [check.id for check in plan.checks], [str(i) for i in range(len(indexes))]
            )
        custom = [["wrapper", "--", "cargo", "test"], ["cargo", "test", "unit"]]
        for version in (1, 2):
            config = replace(config, checks=custom, config_version=version)
            self.assertEqual(
                [list(c.argv) for c in verification.plan_verification(config, "gate").checks],
                custom,
            )
        config.checks[:] = [["new-command"]]
        self.assertEqual(
            verification.plan_verification(config, "gate").checks[0].argv, ("new-command",)
        )

    def test_programmatic_profiles_cannot_waive_gate_or_hide_its_commands(self):
        for profile in (
            VerificationProfile(variants=()),
            VerificationProfile(variants=("default", "default")),
            VerificationProfile(checks=(("override",),)),
        ):
            config = Config()
            config.verification.profiles["gate"] = profile
            with self.subTest(profile=profile), self.assertRaises(ConfigError):
                verification.plan_verification(config, "gate")

    def test_variant_major_flags_precede_separator_and_fmt_ignores_build_flags(self):
        config = self.config("""
[verification.profiles.gate]
variants = ["default", "special"]
[verification.variants.special]
features = ["one", "two"]
no_default_features = true
target = "host-triple"
toolchain = "stable"
""")
        plan = verification.plan_verification(config, "gate")
        self.assertEqual([c.variant for c in plan.checks], ["default"] * 5 + ["special"] * 5)
        self.assertEqual(plan.checks[5].argv, ("cargo", "+stable", "fmt", "--all", "--", "--check"))
        self.assertEqual(
            plan.checks[-1].argv,
            (
                "cargo",
                "+stable",
                "clippy",
                "--workspace",
                "--all-targets",
                "--locked",
                "--features",
                "one,two",
                "--no-default-features",
                "--target",
                "host-triple",
                "--",
                "-D",
                "warnings",
            ),
        )
        self.assertEqual(set(config.profiles), {"low", "medium", "high"})

    def test_package_and_filter_replace_workspace_scope(self):
        fast = verification.plan_verification(Config(), "fast", package="member")
        self.assertEqual(
            fast.checks[0].argv, ("cargo", "fmt", "--package", "member", "--", "--check")
        )
        focused = verification.plan_verification(
            Config(), "focused", package="member", test_filter="specific"
        )
        self.assertEqual(
            focused.checks[-1].argv,
            ("cargo", "test", "--all-targets", "--locked", "--package", "member", "specific"),
        )
        for profile, kwargs in (
            ("gate", {"package": "x"}),
            ("gate", {"test_filter": "x"}),
            ("fast", {"test_filter": "x"}),
            ("lint", {"test_filter": "x"}),
        ):
            with self.subTest(profile=profile, kwargs=kwargs), self.assertRaises(ConfigError):
                verification.plan_verification(Config(), profile, **kwargs)

    def test_requested_transform_rejects_ambiguous_commands(self):
        for command in (
            ["wrapper", "cargo", "test"],
            ["cargo", "test", "existing"],
            ["cargo", "test", "--package", "old"],
            ["cargo", "test", "--features=old"],
            ["cargo", "+nightly", "test"],
            ["cargo", "test", "--manifest-path", "other/Cargo.toml"],
        ):
            with self.subTest(command=command):
                config = self.config(
                    "\n[verification.profiles.focused]\nchecks = " + json.dumps([command])
                )
                with self.assertRaises(ConfigError):
                    verification.plan_verification(
                        config, "focused", package="new", test_filter="needle"
                    )

    def test_test_filter_is_inserted_before_harness_flags_and_conflicts_rejected(self):
        config = self.config(
            '\n[verification.profiles.focused]\nchecks = [["cargo", "test", "--workspace", "--", "--nocapture"]]'
        )
        plan = verification.plan_verification(config, "focused", test_filter="needle")
        self.assertEqual(
            plan.checks[0].argv, ("cargo", "test", "--workspace", "needle", "--", "--nocapture")
        )
        for tail in (["existing"], ["--skip", "existing"], ["--ignored", "existing"]):
            config = self.config(
                "\n[verification.profiles.focused]\nchecks = "
                + json.dumps([["cargo", "test", "--", *tail]])
            )
            with self.subTest(tail=tail), self.assertRaises(ConfigError):
                verification.plan_verification(config, "focused", test_filter="needle")
        for value in ("", " ", "--all-features", "-q"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                verification.plan_verification(Config(), "focused", test_filter=value)

    def test_custom_focused_commands_cannot_silently_ignore_filter(self):
        config = self.config('\n[verification.profiles.focused]\nchecks = [["cargo", "check"]]')
        with self.assertRaises(ConfigError):
            verification.plan_verification(config, "focused", test_filter="needle")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        (self.repo / "source.txt").write_text("original", encoding="utf-8")
        self.config = Config(checks=[[sys.executable, "-c", "print('full output')"]])
        self.plan = verification.plan_verification(self.config)
        self.artifacts = self.repo / ".bb-workflow/test"
        self.records = []
        self.executions = 0

    def execute(self, **kwargs):
        self.executions += 1
        self.artifacts = self.repo / ".bb-workflow" / f"test-{self.executions}"
        return verification.execute_checks(
            self.plan.checks,
            cwd=self.repo,
            artifact_root=self.artifacts,
            config=self.config,
            timeout=kwargs.pop("timeout", lambda: 10),
            record=self.records.append,
            **kwargs,
        )

    def test_execution_writes_full_logs_and_frozen_snapshot_evidence(self):
        records = self.execute(expected=manifest(self.repo))
        self.assertEqual(len(records), 1)
        result = records[0]
        self.assertTrue(result["passed"])
        self.assertTrue(result["source_unchanged"])
        self.assertEqual(result["snapshot_id"], result["result_snapshot_id"])
        directory = self.artifacts / result["artifacts"]
        self.assertEqual((directory / "stdout.txt").read_text(), "full output\n")
        self.assertEqual(
            json.loads((directory / "command.json").read_text()), self.config.checks[0]
        )

    def test_source_mutation_invalidates_result_and_stops_later_commands(self):
        def mutate(argv, **kwargs):
            (self.repo / "source.txt").write_text("changed")
            return ProcessResult(0, "successful tool", "", 0)

        with self.assertRaises(verification.VerificationStop) as stopped:
            self.execute(process_fn=mutate)
        self.assertEqual(stopped.exception.status, "failed")
        self.assertFalse(self.records[0]["passed"])
        self.assertFalse(self.records[0]["source_unchanged"])

    @unittest.skipIf(os.name == "nt", "symlink creation requires Windows privileges")
    def test_invalid_source_manifest_still_records_completed_command_and_logs(self):
        def mutate(argv, **kwargs):
            source = self.repo / "source.txt"
            source.unlink()
            source.symlink_to("missing-source")
            return ProcessResult(0, "kept output", "diagnostic", 0)

        with self.assertRaises(verification.VerificationStop):
            self.execute(process_fn=mutate)
        self.assertEqual(len(self.records), 1)
        self.assertFalse(self.records[0]["passed"])
        self.assertFalse(self.records[0]["source_unchanged"])
        self.assertEqual(
            (self.artifacts / self.records[0]["artifacts"] / "stdout.txt").read_text(),
            "kept output",
        )

    def test_standalone_retains_prior_records_when_second_command_is_interrupted(self):
        config = replace(self.config, checks=[["first"], ["second"]])
        answers = iter([ProcessResult(0, "first log", "", 0), KeyboardInterrupt()])

        def process(*args, **kwargs):
            action = next(answers)
            if isinstance(action, BaseException):
                raise action
            return action

        directory, report = verification.verify_repository(
            self.repo, config, "gate", process_fn=process
        )
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual([r["passed"] for r in report["results"]], [True, False])
        self.assertEqual(
            (directory / report["results"][0]["artifacts"] / "stdout.txt").read_text(), "first log"
        )

    def test_preparation_can_mutate_sources(self):
        def mutate(argv, **kwargs):
            (self.repo / "source.txt").write_text("generated")
            return ProcessResult(0, "", "", 0)

        records = self.execute(process_fn=mutate, preparing=True)
        self.assertTrue(records[0]["passed"])
        self.assertFalse(records[0]["source_unchanged"])

    def test_ordinary_failure_and_timeout_retain_full_gate_evidence(self):
        for first in (
            ProcessResult(1, "bad", "failure", 0),
            ProcessResult(-9, "", "timeout", 0.1, timed_out=True),
        ):
            self.config.checks = [["first"], ["second"]]
            self.plan = verification.plan_verification(self.config)
            answers = iter([first, ProcessResult(0, "second output", "", 0)])
            records = self.execute(process_fn=lambda *a, **kw: next(answers))
            self.assertEqual([r["passed"] for r in records], [False, True])

    def test_fatal_launch_output_and_interrupt_errors_are_recorded(self):
        cases = [
            (FileNotFoundError("gone"), "blocked"),
            (OSError("disk problem"), "failed"),
            (KeyboardInterrupt(), "interrupted"),
            (ProcessResult(0, "truncated", "", 0, output_limit_exceeded=True), "failed"),
        ]
        for action, status in cases:
            with self.subTest(status=status, action=action):
                self.records.clear()

                def process(*args, **kwargs):
                    if isinstance(action, BaseException):
                        raise action
                    return action

                exception = (
                    KeyboardInterrupt if status == "interrupted" else verification.VerificationStop
                )
                with self.assertRaises(exception):
                    self.execute(process_fn=process)
                self.assertEqual(len(self.records), 1)
                self.assertFalse(self.records[0]["passed"])
                self.assertTrue(
                    (self.artifacts / self.records[0]["artifacts"] / "stderr.txt").exists()
                )

    def test_final_budget_and_manifest_checks_prevent_late_success(self):
        calls = 0

        def budget():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise verification.VerificationStop(
                    "budget_exhausted", "Total time budget exhausted"
                )
            return 10

        with self.assertRaises(verification.VerificationStop) as stopped:
            self.execute(timeout=budget)
        self.assertEqual(stopped.exception.status, "budget_exhausted")
        self.assertEqual(len(self.records), 1)
        expected = manifest(self.repo)
        (self.repo / "source.txt").write_text("external mutation")
        with self.assertRaises(verification.VerificationStop):
            self.execute(expected=expected)

    def test_standalone_runs_in_place_without_prepare_and_cannot_apply(self):
        self.config.prepare = [["must-never-run"]]
        self.config.checks = [[sys.executable, "-c", "from pathlib import Path; print(Path.cwd())"]]
        self.config.verification.profiles["fast"] = VerificationProfile(
            checks=(tuple(self.config.checks[0]),)
        )
        for profile in ("gate", "fast"):
            with self.subTest(profile=profile):
                directory, report = verification.verify_repository(self.repo, self.config, profile)
                self.assertEqual(directory.parent, self.repo / ".bb-workflow/verifications")
                self.assertEqual(report["status"], "passed")
                self.assertFalse(report["apply_eligible"])
                self.assertTrue(report["source_unchanged"])
                self.assertIn(str(self.repo), report["results"][0]["stdout_tail"])
                self.assertEqual(
                    json.loads((directory / "report.json").read_text())["status"], "passed"
                )
                self.assertTrue((directory / "report.md").exists())
                self.assertFalse((directory / "workspace").exists())
                self.assertFalse((directory / "candidate_manifest.json").exists())
                self.assertFalse((self.repo / ".bb-workflow/lock").exists())
                with self.assertRaises((WorkspaceError, OSError)):
                    apply_run(directory)

    def test_standalone_gate_narrowing_creates_no_artifacts(self):
        with self.assertRaises(ConfigError):
            verification.verify_repository(self.repo, self.config, "gate", package="x")
        self.assertFalse((self.repo / ".bb-workflow").exists())

    def test_standalone_finalizes_reports_for_every_terminal_error(self):
        cases = [
            (FileNotFoundError("gone"), "blocked"),
            (OSError("bad"), "failed"),
            (KeyboardInterrupt(), "interrupted"),
            (ProcessResult(0, "out", "", 0, output_limit_exceeded=True), "failed"),
            (ProcessResult(-9, "", "", 0.1, timed_out=True), "failed"),
        ]
        for action, status in cases:
            with self.subTest(action=action):

                def process(*args, **kwargs):
                    if isinstance(action, BaseException):
                        raise action
                    return action

                directory, report = verification.verify_repository(
                    self.repo, self.config, "gate", process_fn=process
                )
                self.assertEqual(report["status"], status)
                self.assertEqual(len(report["results"]), 1)
                self.assertEqual(
                    json.loads((directory / "report.json").read_text())["status"], status
                )

    def test_standalone_source_mutation_and_global_budget_fail_closed(self):
        def mutate(*args, **kwargs):
            (self.repo / "source.txt").write_text("mutated")
            return ProcessResult(0, "", "", 0)

        _, report = verification.verify_repository(
            self.repo, self.config, "gate", process_fn=mutate
        )
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["source_unchanged"])

        def slow(*args, **kwargs):
            time.sleep(0.02)
            return ProcessResult(0, "", "", 0.02)

        config = replace(self.config, max_seconds=0.01)
        directory, report = verification.verify_repository(
            self.repo, config, "gate", process_fn=slow
        )
        self.assertEqual(report["status"], "budget_exhausted")
        self.assertEqual(len(report["results"]), 1)
        self.assertTrue((directory / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
