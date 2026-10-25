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


class DiagnosticsIntegrationTests(unittest.TestCase):
    setUp = ExecutionTests.setUp
    execute = ExecutionTests.execute

    def cargo(self, output="", *, stderr="", code=0, **flags):
        self.config = replace(self.config, checks=[["cargo", "check", "--offline"]])
        self.plan = verification.plan_verification(self.config)
        calls = []

        def process(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[-1] == "--version":
                name = "rustfmt" if "fmt" in argv else "clippy" if "clippy" in argv else argv[0]
                return ProcessResult(0, f"{name} 1.90.0 (fixture)\n", "", 0)
            return ProcessResult(code, output, stderr, 0, **flags)

        return process, calls

    def report(self, record):
        return json.loads((self.artifacts / record["diagnostics_file"]).read_text())

    def test_full_stream_diagnostic_is_saved_beyond_check_tail(self):
        process, calls = self.cargo(
            stderr="warning[unused_variables]: unused variable: `x`\n"
            " --> src/lib.rs:4:5\n4 | let x = 1;\n" + " " * 18000
        )
        record = self.execute(process_fn=process)[0]
        report = self.report(record)
        self.assertEqual(report["findings"][0]["code"], "unused_variables")
        self.assertNotIn("unused_variables", record["stderr_tail"])
        self.assertEqual(record["diagnostics"]["counts"]["unclassified"], 1)
        self.assertEqual(
            [c[0] for c in calls],
            [["cargo", "--version"], ["rustc", "--version"], ["cargo", "check", "--offline"]],
        )
        metadata = json.loads((self.artifacts / record["metadata_file"]).read_text())
        self.assertEqual(metadata["tool_versions"]["rustc"], "rustc 1.90.0 (fixture)")
        self.assertTrue(
            (self.artifacts / metadata["probes"][0]["artifacts"] / "stdout.txt").is_file()
        )

    def test_fatal_output_retains_diagnostics_before_propagation(self):
        process, _ = self.cargo(
            stderr="error[E0001]: broken\n --> src/lib.rs:1:1\n1 | broken\n",
            output_limit_exceeded=True,
        )
        with self.assertRaises(verification.VerificationStop):
            self.execute(process_fn=process)
        self.assertEqual(self.report(self.records[-1])["findings"][0]["code"], "E0001")
        self.assertFalse(self.records[-1]["passed"])

    def test_baseline_comparison_tracks_config_tool_scope_and_unknown_probes(self):
        diagnostic = (
            "warning[unused_variables]: unused variable: `x`\n --> src/lib.rs:4:5\n4 | let x = 1;\n"
        )
        process, _ = self.cargo(stderr=diagnostic)
        baseline = self.execute(process_fn=process)
        # The artifact root is stable for baseline lookup; use another command directory.
        baseline_root = self.artifacts

        def run(**kwargs):
            return verification.execute_checks(
                self.plan.checks,
                cwd=self.repo,
                artifact_root=baseline_root,
                command_root=baseline_root / f"candidate-{len(self.records)}",
                config=self.config,
                timeout=lambda: 10,
                record=self.records.append,
                baseline_records=baseline,
                process_fn=kwargs.pop("process_fn", process),
                **kwargs,
            )[0]

        self.assertEqual(run()["diagnostics"]["counts"]["persistent"], 1)
        (self.repo / "Cargo.toml").write_text('[package]\nname="changed"\n')
        self.assertEqual(run()["diagnostics"]["counts"]["unclassified"], 1)
        (self.repo / "Cargo.toml").unlink()
        self.assertEqual(
            run(scope={"package": "other", "test_filter": None})["diagnostics"]["counts"][
                "unclassified"
            ],
            1,
        )

        def unknown(argv, **kwargs):
            if argv[-1] == "--version":
                return ProcessResult(0, "unknown version text", "", 0)
            return process(argv, **kwargs)

        self.assertEqual(run(process_fn=unknown)["diagnostics"]["counts"]["unclassified"], 1)

        def newer(argv, **kwargs):
            result = process(argv, **kwargs)
            return replace(result, stdout=result.stdout.replace("1.90", "1.91"))

        self.assertEqual(run(process_fn=newer)["diagnostics"]["counts"]["unclassified"], 1)

    def test_probe_source_mutation_interrupt_and_budget_are_audited(self):
        for mode in ("mutation", "interrupt", "budget"):
            with self.subTest(mode=mode):
                (self.repo / "source.txt").write_text("original")
                process, calls = self.cargo()

                def probe(argv, **kwargs):
                    if argv[-1] == "--version":
                        if mode == "mutation":
                            (self.repo / "source.txt").write_text("mutated")
                        elif mode == "interrupt":
                            raise KeyboardInterrupt()
                        else:
                            raise verification.VerificationStop("budget_exhausted", "budget")
                    return process(argv, **kwargs)

                expected = (
                    KeyboardInterrupt if mode == "interrupt" else verification.VerificationStop
                )
                with self.assertRaises(expected):
                    self.execute(process_fn=probe)
                self.assertFalse(self.records[-1]["passed"])
                self.assertTrue((self.artifacts / self.records[-1]["metadata_file"]).exists())
                self.assertFalse(any(c[0] == ["cargo", "check", "--offline"] for c in calls))

    def test_metadata_hashes_inherited_config_and_environment_without_secret_values(self):
        from unittest.mock import patch

        process, _ = self.cargo()
        cargo_home = self.repo.parent / (self.repo.name + "-cargo-home")
        cargo_home.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(cargo_home))
        (cargo_home / "config.toml").write_text("[build]\njobs=1\n")
        with patch.dict(os.environ, {"CARGO_HOME": str(cargo_home), "RUSTFLAGS": "secret-marker"}):
            first = self.execute(process_fn=process)[0]
            metadata = (self.artifacts / first["metadata_file"]).read_text()
            digest = self.report(first)["context"]["configuration_digest"]
            self.assertNotIn("secret-marker", metadata)
            (cargo_home / "config.toml").write_text("[build]\njobs=2\n")
            second = self.execute(process_fn=process)[0]
            self.assertNotEqual(digest, self.report(second)["context"]["configuration_digest"])

    def test_selector_probes_are_bounded_and_cached_only_within_one_phase(self):
        process, calls = self.cargo()
        self.config = replace(
            self.config, checks=[["cargo", "+stable", "clippy"], ["cargo", "+stable", "check"]]
        )
        self.plan = verification.plan_verification(self.config)
        self.execute(process_fn=process)
        commands = [call[0] for call in calls]
        self.assertEqual(
            commands,
            [
                ["cargo", "+stable", "--version"],
                ["rustc", "+stable", "--version"],
                ["cargo", "+stable", "clippy", "--version"],
                ["cargo", "+stable", "clippy"],
                ["cargo", "+stable", "check"],
            ],
        )
        self.assertTrue(
            all(call[1]["max_output_bytes"] == self.config.max_output_bytes for call in calls)
        )
        self.assertTrue(all(call[1]["timeout"] <= 10 for call in calls))
        self.execute(process_fn=process)
        self.assertEqual(
            sum(command == ["cargo", "+stable", "--version"] for command, _ in calls), 2
        )

    def test_unknown_wrappers_never_trigger_guessed_tool_probes(self):
        self.config = replace(self.config, checks=[["wrapper", "cargo", "check"]])
        self.plan = verification.plan_verification(self.config)
        calls = []

        def custom(argv, **kwargs):
            calls.append(argv)
            return ProcessResult(
                0, "", "warning[unused]: warning\n --> src/lib.rs:1:1\n1 | code\n", 0
            )

        records = self.execute(process_fn=custom)
        self.assertEqual(calls, [["wrapper", "cargo", "check"]])
        self.assertEqual(records[0]["diagnostics"]["counts"]["unclassified"], 1)

    def test_probe_failures_unknown_override_and_cli_config_cannot_resolve(self):
        from unittest.mock import patch

        diagnostic = "warning[unused]: warning\n --> src/lib.rs:1:1\n1 | code\n"
        process, _ = self.cargo(stderr=diagnostic)
        baseline = self.execute(process_fn=process)
        root = self.artifacts
        for index, mode in enumerate(
            ("failed", "empty", "timeout", "overflow", "wrapper", "cli-config")
        ):
            with self.subTest(mode=mode):

                def probe(argv, **kwargs):
                    if argv[-1] == "--version":
                        if mode == "failed":
                            raise FileNotFoundError("missing")
                        if mode in {"empty", "timeout", "overflow"}:
                            return ProcessResult(
                                0,
                                "",
                                "",
                                0,
                                timed_out=mode == "timeout",
                                output_limit_exceeded=mode == "overflow",
                            )
                    return process(argv, **kwargs)

                command = ("cargo", "check", "--offline")
                if mode == "cli-config":
                    # Even the same CLI config argv cannot establish which effective tool it selects.
                    command += ("--config", 'build.rustc="custom"')
                check = replace(self.plan.checks[0], argv=command)
                with patch.dict(
                    os.environ, {"RUSTC_WRAPPER": "custom-wrapper"} if mode == "wrapper" else {}
                ):
                    records = verification.execute_checks(
                        (check,),
                        cwd=self.repo,
                        artifact_root=root,
                        command_root=root / f"probe-failure-{index}",
                        config=self.config,
                        timeout=lambda: 10,
                        baseline_records=baseline,
                        process_fn=probe,
                    )
                self.assertEqual(records[0]["diagnostics"]["counts"]["resolved"], 0)
                self.assertEqual(records[0]["diagnostics"]["counts"]["unobserved_previous"], 1)
                if mode in {"wrapper", "cli-config"}:
                    full = json.loads((root / records[0]["metadata_file"]).read_text())
                    self.assertIn(None, full["tool_versions"].values())

    def test_empty_scope_remains_unknown_and_clippy_config_contents_change_identity(self):
        from unittest.mock import patch

        process, _ = self.cargo()
        directory = self.repo.parent / (self.repo.name + "-lint-config")
        self.addCleanup(lambda: __import__("shutil").rmtree(directory))
        directory.mkdir()
        config_file = directory / "clippy.toml"
        config_file.write_text('msrv="1.80"\n')
        with patch.dict(os.environ, {"CLIPPY_CONF_DIR": str(directory)}):
            first = self.execute(process_fn=process, scope={})[0]
            first_report = self.report(first)
            self.assertEqual(first_report["context"]["scope"], {})
            config_file.write_text('msrv="1.81"\n')
            second = self.execute(process_fn=process)[0]
            self.assertNotEqual(
                first_report["context"]["configuration_digest"],
                self.report(second)["context"]["configuration_digest"],
            )

    @unittest.skipIf(os.name == "nt", "symlink creation requires Windows privileges")
    def test_probe_invalid_source_manifest_still_retains_failed_check_evidence(self):
        process, _ = self.cargo()

        def invalid(argv, **kwargs):
            if argv[-1] == "--version":
                source = self.repo / "source.txt"
                source.unlink()
                source.symlink_to("missing")
            return process(argv, **kwargs)

        with self.assertRaises(verification.VerificationStop):
            self.execute(process_fn=invalid)
        self.assertEqual(len(self.records), 1)
        self.assertFalse(self.records[0]["passed"])
        self.assertTrue((self.artifacts / self.records[0]["diagnostics_file"]).exists())

    def test_external_configuration_changed_during_probe_or_check_is_not_resolution(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temporary:
            config_file = Path(temporary) / "config.toml"
            process, _ = self.cargo(
                stderr="warning[unused]: warning\n --> src/lib.rs:1:1\n1 | code\n"
            )
            with patch.dict(os.environ, {"CARGO_HOME": temporary}):
                for mode in ("probe", "check"):
                    with self.subTest(mode=mode):
                        config_file.write_text("[build]\njobs=1\n")
                        baseline = self.execute(process_fn=process)
                        root = self.artifacts
                        changed = False

                        def mutate(argv, **kwargs):
                            nonlocal changed
                            if (
                                (mode == "probe" and argv[-1] == "--version")
                                or (mode == "check" and argv[-1] != "--version")
                            ) and not changed:
                                config_file.write_text("[build]\njobs=2\n")
                                changed = True
                            if argv[-1] == "--version":
                                return process(argv, **kwargs)
                            return ProcessResult(0, "", "", 0)

                        result = verification.execute_checks(
                            self.plan.checks,
                            cwd=self.repo,
                            artifact_root=root,
                            command_root=root / "changed",
                            config=self.config,
                            timeout=lambda: 10,
                            baseline_records=baseline,
                            process_fn=mutate,
                        )[0]
                        self.assertTrue(result["passed"])
                        self.assertEqual(result["diagnostics"]["counts"]["resolved"], 0)
                        self.assertEqual(result["diagnostics"]["counts"]["unobserved_previous"], 1)

    @unittest.skipIf(os.name == "nt", "symlink creation requires Windows privileges")
    def test_configured_excluded_symlink_does_not_break_metadata_or_custom_execution(self):
        ignored = self.repo / "ignored-link"
        ignored.symlink_to("missing-target")
        process, _ = self.cargo()
        self.config = replace(self.config, exclude=(*self.config.exclude, "ignored-link"))
        for commands, runner in (
            ([[sys.executable, "-c", "pass"]], None),
            ([["cargo", "check", "--offline"]], process),
        ):
            with self.subTest(commands=commands):
                config = replace(self.config, checks=commands)
                kwargs = {"process_fn": runner} if runner is not None else {}
                _, report = verification.verify_repository(self.repo, config, "gate", **kwargs)
                self.assertEqual(report["status"], "passed", report["reason"])
                self.assertTrue(report["source_unchanged"])

    def test_environment_root_normalization_does_not_conflate_external_prefix_paths(self):
        from bb_workflow.verification_metadata import configuration

        other_root = self.repo / "other-root"
        other_root.mkdir()
        left = configuration(self.repo, self.config, {"RUSTFLAGS": str(self.repo) + "-external"})
        right = configuration(other_root, self.config, {"RUSTFLAGS": str(other_root) + "-external"})
        self.assertNotEqual(left["environment"]["RUSTFLAGS"], right["environment"]["RUSTFLAGS"])

    def test_external_and_excluded_selected_manifests_cannot_resolve(self):
        process, _ = self.cargo(stderr="warning[unused]: warning\n --> src/lib.rs:1:1\n1 | code\n")
        with tempfile.TemporaryDirectory() as external:
            for index, arguments in enumerate(
                (
                    ["--manifest-path", str(Path(external) / "Cargo.toml")],
                    ["--manifest-path=" + str(Path(external) / "Cargo.toml")],
                    ["--manifest-path", "target/selected/Cargo.toml"],
                )
            ):
                with self.subTest(arguments=arguments):
                    selected = (
                        Path(external) / "Cargo.toml"
                        if index < 2
                        else self.repo / "target/selected/Cargo.toml"
                    )
                    selected.parent.mkdir(parents=True, exist_ok=True)
                    selected.write_text('[package]\nname="first"\nversion="0.1.0"\n')
                    self.plan = verification.plan_verification(
                        replace(self.config, checks=[["cargo", "check", *arguments, "--offline"]])
                    )
                    baseline = self.execute(process_fn=process)
                    root = self.artifacts
                    selected.write_text('[package]\nname="second"\nversion="0.2.0"\n')

                    def quiet(argv, **kwargs):
                        return (
                            process(argv, **kwargs)
                            if argv[-1] == "--version"
                            else ProcessResult(0, "", "", 0)
                        )

                    result = verification.execute_checks(
                        self.plan.checks,
                        cwd=self.repo,
                        artifact_root=root,
                        command_root=root / "candidate",
                        config=self.config,
                        timeout=lambda: 10,
                        baseline_records=baseline,
                        process_fn=quiet,
                    )[0]
                    self.assertTrue(result["passed"])
                    self.assertEqual(result["diagnostics"]["counts"]["resolved"], 0)
                    self.assertEqual(result["diagnostics"]["counts"]["unobserved_previous"], 1)
                    metadata = json.loads((root / result["metadata_file"]).read_text())
                    self.assertIsNone(metadata["digest"])
                    self.assertTrue(metadata["reasons"])

    def test_ancestor_workspace_lock_and_external_path_dependencies_are_uncaptured(self):
        from bb_workflow.verification_metadata import configuration

        member = self.repo / "member"
        member.mkdir()
        manifest_file = member / "Cargo.toml"
        for case in ("ancestor-workspace", "path-dependency", "explicit-workspace"):
            with self.subTest(case=case):
                (self.repo / "Cargo.toml").write_text(
                    '[workspace]\nmembers=["member"]\n[workspace.package]\nversion="0.1.0"\n'
                )
                (self.repo / "Cargo.lock").write_text("version = 4\n")
                text = '[package]\nname="member"\nversion.workspace=true\n'
                if case == "path-dependency":
                    text = '[package]\nname="member"\nversion="0.1.0"\n[workspace]\n[dependencies]\nother={path="../../external"}\n'
                if case == "explicit-workspace":
                    text += 'workspace="../../external"\n'
                manifest_file.write_text(text)
                first = configuration(member, self.config, {})
                (self.repo / "Cargo.toml").write_text(
                    '[workspace]\nmembers=["member"]\n[workspace.package]\nversion="0.2.0"\n'
                )
                second = configuration(member, self.config, {})
                self.assertIsNone(first["digest"])
                self.assertIsNone(second["digest"])
                self.assertTrue(first["reasons"])
        # A complete in-tree workspace/dependency inventory remains comparable.
        manifest_file.write_text('[package]\nname="member"\nversion.workspace=true\n')
        captured = configuration(self.repo, self.config, {})
        self.assertIsNotNone(captured["digest"])
        self.assertIn("member/Cargo.toml", captured["files"])
        self.assertIn("Cargo.lock", captured["files"])

    def test_plain_cargo_test_probes_actual_rustdoc_and_changed_version_is_incomparable(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temporary:
            executable = str(Path(temporary) / "custom-rustdoc")
            version = "1.90.0"
            emit_warning = True
            calls = []
            self.config = replace(self.config, checks=[["cargo", "test", "--offline", "--locked"]])
            self.plan = verification.plan_verification(self.config)

            def process(argv, **kwargs):
                calls.append(argv)
                if argv[-1] == "--version":
                    tool = "rustdoc" if argv[0] == executable else argv[0]
                    return ProcessResult(
                        0, f"{tool} {version if tool == 'rustdoc' else '1.90.0'}\n", "", 0
                    )
                return ProcessResult(
                    0,
                    "",
                    "warning[doc_fixture]: warning\n --> src/lib.rs:1:1\n1 | code\n"
                    if emit_warning
                    else "",
                    0,
                )

            with patch.dict(os.environ, {"RUSTDOC": executable}):
                baseline = self.execute(process_fn=process)
                root = self.artifacts
                version, emit_warning = "1.91.0", False
                result = verification.execute_checks(
                    self.plan.checks,
                    cwd=self.repo,
                    artifact_root=root,
                    command_root=root / "candidate",
                    config=self.config,
                    timeout=lambda: 10,
                    baseline_records=baseline,
                    process_fn=process,
                )[0]
            self.assertIn([executable, "--version"], calls)
            self.assertTrue(result["passed"])
            self.assertEqual(result["diagnostics"]["counts"]["resolved"], 0)
            self.assertEqual(result["diagnostics"]["counts"]["unobserved_previous"], 1)
            baseline_report = json.loads((root / baseline[0]["diagnostics_file"]).read_text())
            current_report = json.loads((root / result["diagnostics_file"]).read_text())
            self.assertEqual(
                baseline_report["context"]["tool_versions"]["rustdoc"], "rustdoc 1.90.0"
            )
            self.assertEqual(
                current_report["context"]["tool_versions"]["rustdoc"], "rustdoc 1.91.0"
            )

    def test_malformed_numeric_output_preserves_standalone_execution_and_audit(self):
        self.config = replace(self.config, checks=[["fixture-check"]])
        text = "running " + "9" * 5000 + " tests\n"
        directory, report = verification.verify_repository(
            self.repo,
            self.config,
            "gate",
            process_fn=lambda *args, **kwargs: ProcessResult(0, text, "", 0),
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["results"]), 1)
        self.assertTrue(report["results"][0]["passed"])
        saved = json.loads((directory / "report.json").read_text())
        self.assertEqual(saved["status"], "passed")
        diagnostics = json.loads((directory / saved["results"][0]["diagnostics_file"]).read_text())
        self.assertTrue(diagnostics["uncertainty"])
        self.assertTrue(diagnostics["execution"]["successful"])

    def test_unresolvable_manifest_scope_is_unknown_and_preserves_command_audit(self):
        from bb_workflow.verification_metadata import configuration

        manifest_file = self.repo / "Cargo.toml"
        manifest_file.write_text(
            '[package]\nname="fixture"\nversion="0.1.0"\n[dependencies]\nfoo={path="\\u0000"}\n'
        )
        metadata = configuration(self.repo, self.config, {}, ("cargo", "check"))
        self.assertIsNone(metadata["digest"])
        self.assertIn("unresolved_manifest_scope", metadata["reasons"])
        process, _ = self.cargo()
        directory, report = verification.verify_repository(
            self.repo, self.config, "gate", process_fn=process
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["results"]), 1)
        self.assertTrue(report["results"][0]["passed"])
        self.assertTrue((directory / report["results"][0]["diagnostics_file"]).is_file())
        # Resolve failures for selected argv use the same conservative boundary.
        manifest_file.write_text('[package]\nname="fixture"\nversion="0.1.0"\n')
        metadata = configuration(
            self.repo, self.config, {}, ("cargo", "check", "--manifest-path=\0")
        )
        self.assertIsNone(metadata["digest"])
        self.assertIn("unresolved_manifest_scope", metadata["reasons"])

    def test_uncaptured_dependency_directory_with_rs_suffix_cannot_resolve(self):
        from bb_workflow.verification_metadata import configuration

        source = self.repo / "src/custom.rs"
        source.parent.mkdir()
        source.write_text("pub fn value() {}\n")
        root_manifest = self.repo / "Cargo.toml"
        local = (
            '[package]\nname="fixture"\nversion="0.1.0"\n[workspace]\n[lib]\npath="src/custom.rs"\n'
        )
        root_manifest.write_text(local + '[dependencies]\nfoo={path="target/foo.rs"}\n')
        dependency = self.repo / "target/foo.rs/Cargo.toml"
        dependency.parent.mkdir(parents=True)
        dependency.write_text('[package]\nname="foo"\nversion="0.1.0"\n')
        process, _ = self.cargo(
            stderr="warning[unused]: warning\n --> src/custom.rs:1:1\n1 | code\n"
        )
        baseline = self.execute(process_fn=process)
        root = self.artifacts
        dependency.write_text('[package]\nname="foo"\nversion="0.2.0"\n')

        def quiet(argv, **kwargs):
            return (
                process(argv, **kwargs) if argv[-1] == "--version" else ProcessResult(0, "", "", 0)
            )

        result = verification.execute_checks(
            self.plan.checks,
            cwd=self.repo,
            artifact_root=root,
            command_root=root / "candidate",
            config=self.config,
            timeout=lambda: 10,
            baseline_records=baseline,
            process_fn=quiet,
        )[0]
        self.assertTrue(result["passed"])
        self.assertEqual(result["diagnostics"]["counts"]["resolved"], 0)
        self.assertEqual(result["diagnostics"]["counts"]["unobserved_previous"], 1)
        metadata = json.loads((root / result["metadata_file"]).read_text())
        self.assertIsNone(metadata["digest"])
        self.assertIn("uncaptured_manifest_reference", metadata["reasons"])
        # Captured source files can still identify legitimate source-target paths.
        root_manifest.write_text(local)
        self.assertIsNotNone(
            configuration(self.repo, self.config, {}, ("cargo", "check"))["digest"]
        )
