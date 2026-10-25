"""Acceptance tests for the default gate against real, dependency-free Cargo workspaces."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bb_workflow.backends import BackendResult
from bb_workflow.config import (
    VerificationConfig,
    VerificationProfile,
    VerificationVariant,
    load_config,
)
from bb_workflow.engine import Workflow
from bb_workflow.process import run_process
from bb_workflow.requirements import Criterion, Requirements
from bb_workflow.verification import execute_checks, plan_verification, verify_repository
from bb_workflow.workspace import manifest, snapshot_id


class RustChecksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = ("git", "cargo", "rustc", "rustdoc", "rustfmt", "cargo-clippy")
        missing = [name for name in required if shutil.which(name) is None]
        if missing:
            raise unittest.SkipTest("Rust gate tools unavailable: " + ", ".join(missing))
        for command in (["cargo", "fmt", "--version"], ["cargo", "clippy", "--version"]):
            result = run_process(command, cwd=Path.cwd(), timeout=20)
            if result.returncode:
                output = result.stdout + result.stderr
                if any(
                    marker in output.lower()
                    for marker in ("not installed", "no such command", "no default is configured")
                ):
                    raise unittest.SkipTest(f"Rust gate tool unavailable: {output.strip()}")
                raise RuntimeError(f"Rust gate tool probe failed: {output.strip()}")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / "workspace"
        self.repo.mkdir()
        (self.repo / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["selected", "extra"]\n'
            'default-members = ["selected"]\nresolver = "2"\n',
            encoding="utf-8",
        )
        for name in ("selected", "extra"):
            directory = self.repo / name
            (directory / "src").mkdir(parents=True)
            (directory / "Cargo.toml").write_text(
                f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n',
                encoding="utf-8",
            )
            (directory / "src/lib.rs").write_text(
                f"/// ```\n/// assert_eq!({name}::value(), 1);\n/// ```\n"
                "pub fn value() -> u32 {\n    1\n}\n",
                encoding="utf-8",
            )
        # These fixtures need no registry access, even on a machine with an empty Cargo cache.
        environment = patch.dict(
            os.environ, {"CARGO_NET_OFFLINE": "true", "CARGO_TERM_COLOR": "never"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.config = replace(
            load_config(), config_version=2, max_cycles=0, timeout_seconds=60, max_seconds=240
        )
        self.original = manifest(self.repo, self.config.exclude)
        self.calls = []
        self.implement = lambda directory: None

    def invoke(self, **kwargs):
        self.calls.append(
            {"role": kwargs["role"], "cwd": kwargs["cwd"], "snapshot": manifest(kwargs["cwd"])}
        )
        if kwargs["role"] == "implementation":
            self.implement(kwargs["cwd"])
        return BackendResult(
            {"status": "completed", "summary": "Fixture complete", "findings": [], "decisions": []},
            0.0,
            {},
            kwargs["selection"].model,
        )

    def run_workflow(self):
        flow = Workflow(
            self.repo,
            "Validate the tiny Cargo workspace",
            self.config,
            invoke_fn=self.invoke,
            process_fn=run_process,
            progress=lambda message: None,
        )
        directory = flow.run()
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state, json.loads(json.dumps(flow.state)))
        self.assertEqual(manifest(self.repo, self.config.exclude), self.original)
        self.assertFalse((self.repo / "Cargo.lock").exists())
        self.assertFalse((self.repo / ".bb-workflow/lock").exists())
        return flow, state

    def test_acceptance_rejects_real_zero_filter_and_proves_exact_passing_named_test(self):
        library = self.repo / "selected/src/lib.rs"
        library.write_text(
            library.read_text()
            + "\n#[test]\nfn exact_regression() { assert_eq!(value(), 1); }\n"
            + '\n#[test]\n#[should_panic]\nfn rejects_invalid_input() { panic!("invalid"); }\n'
        )

        def provider(**call):
            payload = json.loads(call["prompt"].split("Task and evidence (JSON):\n", 1)[1])
            return BackendResult(
                {
                    "status": "completed",
                    "summary": "Offline fixture",
                    "findings": [],
                    "decisions": [],
                    "schema_version": 2,
                    "binding": payload["acceptance"]["binding"],
                    "criterion_evidence": [],
                    "criterion_assessments": [],
                    "criterion_decisions": [],
                },
                0.0,
                {},
                call["selection"].model,
            )

        for test_filter, test_name, expected in (
            ("missing_filter", "exact_regression", "limit_exhausted"),
            ("exact_regression", "exact_regression", "succeeded"),
            ("rejects_invalid_input", "rejects_invalid_input", "succeeded"),
            ("rejects_invalid_input", "exact_regression", "limit_exhausted"),
        ):
            with self.subTest(test_filter=test_filter, test_name=test_name):
                config = replace(
                    self.config,
                    config_version=3,
                    prepare=[["cargo", "generate-lockfile", "--offline"]],
                    checks=[
                        [
                            "cargo",
                            "test",
                            "--workspace",
                            "--lib",
                            "--offline",
                            test_filter,
                            "--",
                            "--exact",
                        ]
                    ],
                )
                requirements = Requirements(
                    criteria=(
                        Criterion(
                            "regression",
                            "Exact regression passes",
                            "mechanical",
                            "Run the exact named test",
                            "0",
                            test_name,
                        ),
                    )
                )
                flow = Workflow(
                    self.repo,
                    "Validate regression",
                    config,
                    requirements=requirements,
                    invoke_fn=provider,
                    progress=lambda text: None,
                )
                flow.run()
                self.assertEqual(flow.state["status"], expected, flow.state["reason"])
                assessment = flow.state["acceptance"]["rounds"][0]["mechanical"][0]
                self.assertEqual(
                    assessment["verdict"], "satisfied" if expected == "succeeded" else "unmet"
                )
                self.assertEqual(flow.state["checks"][-1]["returncode"], 0)
                self.assertFalse((self.repo / "Cargo.lock").exists())

    def test_standalone_gate_executes_real_offline_feature_variants(self):
        package = self.repo / "selected/Cargo.toml"
        package.write_text(package.read_text() + "\n[features]\nextra = []\n", encoding="utf-8")
        library = self.repo / "selected/src/lib.rs"
        library.write_text(
            library.read_text()
            + '\n#[cfg(all(test, feature = "extra"))]\n#[test]\nfn extra_enabled() { assert_eq!(value(), 1); }\n',
            encoding="utf-8",
        )
        lock = run_process(["cargo", "generate-lockfile", "--offline"], cwd=self.repo, timeout=30)
        self.assertEqual(lock.returncode, 0, lock.stderr)
        verification = VerificationConfig()
        verification.profiles["gate"] = VerificationProfile(variants=("default", "feature"))
        verification.variants["feature"] = VerificationVariant(
            features=("extra",), no_default_features=True
        )
        config = replace(
            self.config,
            verification=verification,
            checks=[["cargo", "test", "--workspace", "--all-targets", "--locked"]],
        )
        _, report = verify_repository(self.repo, config, "gate")
        self.assertEqual(report["status"], "passed", report["reason"])
        self.assertEqual([r["variant"] for r in report["results"]], ["default", "feature"])
        self.assertNotIn("extra_enabled", report["results"][0]["stdout_tail"])
        self.assertIn("extra_enabled ... ok", report["results"][1]["stdout_tail"])
        self.assertTrue(report["source_unchanged"])

    def test_standalone_focused_limits_real_package_and_test_filter(self):
        library = self.repo / "selected/src/lib.rs"
        library.write_text(
            library.read_text()
            + '\n#[test]\nfn needle_case() { assert_eq!(value(), 1); }\n#[test]\nfn unrelated_case() { panic!("must be filtered"); }\n',
            encoding="utf-8",
        )
        (self.repo / "extra/src/lib.rs").write_text(
            'compile_error!("outside chosen package");\n', encoding="utf-8"
        )
        lock = run_process(["cargo", "generate-lockfile", "--offline"], cwd=self.repo, timeout=30)
        self.assertEqual(lock.returncode, 0, lock.stderr)
        _, report = verify_repository(
            self.repo, self.config, "focused", package="selected", test_filter="needle"
        )
        self.assertEqual(report["status"], "passed", report["reason"])
        self.assertIn("needle_case ... ok", report["results"][1]["stdout_tail"])
        self.assertIn("1 filtered out", report["results"][1]["stdout_tail"])
        self.assertTrue(report["source_unchanged"])

    def test_error_outside_default_members_blocks_success(self):
        # Isolate validation coverage; the healthy control exercises the default prepare.
        self.config = replace(self.config, prepare=[["cargo", "generate-lockfile"]])

        def break_extra(directory):
            (directory / "extra/src/lib.rs").write_text(
                'pub fn value() -> u32 {\n    "wrong type"\n}\n', encoding="utf-8"
            )

        self.implement = break_extra
        flow, state = self.run_workflow()
        self.assertEqual(state["status"], "limit_exhausted", state["reason"])
        self.assertTrue(all(check["passed"] for check in state["baseline_checks"]))
        preparation = [check for check in state["checks"] if check["kind"] == "preparation"]
        self.assertTrue(preparation)
        self.assertTrue(all(check["passed"] for check in preparation))
        failed = [check for check in state["checks"] if not check["passed"]]
        self.assertTrue(failed)
        self.assertTrue(all(check["phase"] == "candidate-0" for check in failed))
        self.assertTrue(all("--workspace" in check["command"] for check in failed))
        self.assertTrue(
            any(
                check["command"] == ["cargo", "check", "--workspace", "--all-targets", "--locked"]
                for check in failed
            )
        )
        self.assertIn("extra/src/lib.rs", failed[0]["stderr_tail"].replace("\\", "/"))
        self.assertEqual([call["role"] for call in self.calls], ["implementation"])
        self.assertFalse((flow.run_dir / "candidate_manifest.json").exists())
        # Prove that Cargo's narrower default-member selection misses this exact defect.
        narrow = run_process(
            ["cargo", "check", "--all-targets", "--locked"], cwd=flow.work, timeout=60
        )
        self.assertEqual(narrow.returncode, 0, narrow.stderr)

    def test_failing_doctest_blocks_success_when_all_targets_pass(self):
        def break_doctest(directory):
            library = directory / "selected/src/lib.rs"
            library.write_text(
                library.read_text(encoding="utf-8").replace("::value(), 1)", "::value(), 2)"),
                encoding="utf-8",
            )

        self.implement = break_doctest
        flow, state = self.run_workflow()
        self.assertEqual(state["status"], "limit_exhausted", state["reason"])
        self.assertTrue(all(check["passed"] for check in state["baseline_checks"]))
        candidate = [check for check in state["checks"] if check["phase"] == "candidate-0"]
        all_targets = [
            check
            for check in candidate
            if check["command"][1] == "test" and "--all-targets" in check["command"]
        ]
        self.assertEqual(len(all_targets), 1)
        self.assertTrue(all_targets[0]["passed"], all_targets[0]["stderr_tail"])
        failed = [check for check in candidate if not check["passed"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(
            failed[0]["command"], ["cargo", "test", "--workspace", "--doc", "--locked"]
        )
        self.assertNotEqual(failed[0]["returncode"], 0)
        self.assertIn("assertion", failed[0]["stdout_tail"])
        self.assertTrue(all(check["source_unchanged"] for check in candidate))
        self.assertEqual([call["role"] for call in self.calls], ["implementation"])
        self.assertFalse((flow.run_dir / "candidate_manifest.json").exists())

    def test_valid_workspace_prepares_lockfile_then_passes_full_frozen_gate(self):
        flow, state = self.run_workflow()
        self.assertEqual(state["status"], "succeeded", state["reason"])
        self.assertEqual(
            [call["role"] for call in self.calls], ["implementation", "reviewer_a", "reviewer_b"]
        )
        self.assertTrue((flow.work / "Cargo.lock").is_file())
        preparation = state["baseline_preparation"]
        self.assertTrue(preparation)
        self.assertTrue(all(check["passed"] for check in preparation))
        self.assertFalse(preparation[0]["source_unchanged"])
        self.assertEqual(
            [check["phase"] for check in state["checks"]],
            ["baseline-prepare"] * len(self.config.prepare)
            + ["baseline"] * len(self.config.checks)
            + ["candidate-0-prepare"] * len(self.config.prepare)
            + ["candidate-0"] * len(self.config.checks),
        )
        candidate = [check for check in state["checks"] if check["phase"] == "candidate-0"]
        self.assertEqual([check["command"] for check in candidate], self.config.checks)
        for check in candidate:
            self.assertTrue(check["passed"], check["stderr_tail"])
            self.assertTrue(check["source_unchanged"])
            self.assertEqual(check["snapshot_id"], state["candidate_id"])
            self.assertEqual(check["result_snapshot_id"], state["candidate_id"])
        self.assertEqual(state["candidate_id"], snapshot_id(manifest(flow.work)))
        self.assertEqual(self.calls[1]["snapshot"], self.calls[2]["snapshot"])
        self.assertEqual(self.calls[1]["snapshot"], manifest(flow.work))

    def test_diagnostics_offline_test_failure_compile_failure_and_explicit_pass(self):
        # A single crate gives Cargo's human Running header a unique suite identity.
        crate = self.repo / "selected"
        source = crate / "src/lib.rs"
        source.write_text("#[test]\nfn regression() { assert_eq!(1, 2); }\n")
        lock = run_process(["cargo", "generate-lockfile", "--offline"], cwd=self.repo, timeout=30)
        self.assertEqual(lock.returncode, 0, lock.stderr)
        config = replace(
            self.config, checks=[["cargo", "test", "--lib", "--offline", "-p", "selected"]]
        )
        plan = plan_verification(config)
        artifacts = self.repo / ".bb-workflow/diagnostics-test"

        def checks(label, baseline=None):
            return execute_checks(
                plan.checks,
                cwd=self.repo,
                artifact_root=artifacts,
                command_root=artifacts / label,
                config=config,
                timeout=lambda: 60,
                baseline_records=baseline,
                process_fn=run_process,
            )

        baseline = checks("baseline")
        self.assertFalse(baseline[0]["passed"])
        source.write_text('#[test]\nfn regression() { let _: u32 = "bad"; }\n')
        broken = checks("compile-failure", baseline)
        self.assertFalse(broken[0]["passed"])
        self.assertEqual(broken[0]["diagnostics"]["counts"]["resolved"], 0)
        self.assertEqual(broken[0]["diagnostics"]["counts"]["unobserved_previous"], 1)
        source.write_text("#[test]\nfn regression() { assert_eq!(1, 1); }\n")
        passing = checks("passing", baseline)
        self.assertTrue(passing[0]["passed"])
        self.assertEqual(passing[0]["diagnostics"]["counts"]["resolved"], 1)
        self.assertEqual(baseline[0]["diagnostics"]["counts"]["unclassified"], 1)

    def test_real_json_compiler_diagnostic_matches_distinct_roots_and_moved_lines(self):
        artifacts = self.repo / ".bb-workflow/diagnostics-json"
        config = replace(
            self.config, checks=[["cargo", "check", "--offline", "--message-format=json"]]
        )
        plan = plan_verification(config)
        with tempfile.TemporaryDirectory() as temporary:
            roots = [Path(temporary) / "baseline", Path(temporary) / "candidate"]
            previous = None
            for index, root in enumerate(roots):
                (root / "src").mkdir(parents=True)
                (root / "Cargo.toml").write_text(
                    '[package]\nname="demo"\nversion="0.1.0"\nedition="2021"\n'
                )
                (root / "src/lib.rs").write_text(
                    "\n" * index + "pub fn demo() { let unused = 1; }\n"
                )
                lock = run_process(
                    ["cargo", "generate-lockfile", "--offline"], cwd=root, timeout=30
                )
                self.assertEqual(lock.returncode, 0, lock.stderr)
                result = execute_checks(
                    plan.checks,
                    cwd=root,
                    artifact_root=artifacts,
                    command_root=artifacts / str(index),
                    config=config,
                    timeout=lambda: 60,
                    baseline_records=previous,
                    process_fn=run_process,
                )
                self.assertTrue(result[0]["passed"])
                if previous is not None:
                    self.assertEqual(result[0]["diagnostics"]["counts"]["persistent"], 1)
                previous = result


if __name__ == "__main__":
    unittest.main()
