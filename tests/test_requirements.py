"""Acceptance requirements reject stale proof and use complete audited test output."""

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path

from bb_workflow import requirements as req
from bb_workflow.verification import PlannedCheck, VerificationPlan


class RequirementsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = VerificationPlan(
            "gate",
            ("default",),
            None,
            None,
            (PlannedCheck("2", "gate", "default", ("cargo", "test", "--workspace")),),
        )

    def load(self, text):
        path = self.root / "requirements.toml"
        path.write_text(text, encoding="utf-8")
        return req.load_requirements(path)

    def criterion(self, **changes):
        return req.Criterion(
            **{
                "id": "upper-bound",
                "description": "Reject upper bound",
                "kind": "mechanical",
                "verification": "Run regression",
                "check_id": "2",
                "test_name": "validation::upper",
                **changes,
            }
        )

    def bound(self, criterion=None, plan=None):
        return req.bind_requirements(
            req.Requirements(criteria=(criterion or self.criterion(),)), plan or self.plan
        )

    def binding(self, bound):
        return dict(
            candidate_id="candidate",
            requirements_id=bound.requirements_id,
            verification_plan_id=bound.verification_plan_id,
        )

    def record(self, stdout=None, **changes):
        directory = self.root / "checks/0"
        directory.mkdir(parents=True, exist_ok=True)
        if stdout is not None:
            (directory / "stdout.txt").write_text(stdout, encoding="utf-8")
            (directory / "stderr.txt").write_text("", encoding="utf-8")
        return dict(
            id="2",
            profile="gate",
            variant="default",
            kind="validation",
            snapshot_id="candidate",
            result_snapshot_id="candidate",
            source_unchanged=True,
            command=["cargo", "test", "--workspace"],
            passed=True,
            returncode=0,
            timed_out=False,
            output_limit_exceeded=False,
            artifacts="checks/0",
            stdout_tail="not proof",
            stderr_tail="",
            **changes,
        )

    def verdict(self, records, bound=None, binding=None):
        bound = bound or self.bound()
        items = req.evaluate_mechanical(bound, records, binding or self.binding(bound), self.root)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["criterion_id"], "upper-bound")
        self.assertEqual(items[0]["binding"], binding or self.binding(bound))
        return items[0]["verdict"]

    def test_loading_is_strict_and_immutable(self):
        requirements = self.load("""requirements_version = 1
[[criteria]]
id = "upper-bound"
description = "Reject upper bound"
kind = "mechanical"
verification = "Run regression"
check_id = "2"
test_name = "validation::upper"
""")
        self.assertEqual(requirements.criteria, (self.criterion(),))
        with self.assertRaises(FrozenInstanceError):
            requirements.criteria = ()
        with self.assertRaises(FrozenInstanceError):
            requirements.criteria[0].kind = "semantic"
        # Parsing is frozen: changing the file cannot change IDs or bound input.
        identifier = req.requirements_id(requirements)
        (self.root / "requirements.toml").write_text("invalid", encoding="utf-8")
        self.assertEqual(req.requirements_id(requirements), identifier)

    def test_rejects_invalid_root_and_criterion_contracts(self):
        valid = """requirements_version = 1
[[criteria]]
id = "upper-bound"
description = "Reject upper bound"
kind = "mechanical"
verification = "Run regression"
check_id = "2"
"""
        invalid = [
            valid.replace("= 1", "= true", 1),
            valid.replace("= 1", "= 2", 1),
            valid.replace("= 1", '= "1"', 1),
            valid + 'extra = "bad"\n',
            valid.replace('id = "upper-bound"', 'id = "2-invalid"'),
            valid.replace('description = "Reject upper bound"', 'description = " "'),
            valid.replace('verification = "Run regression"\n', ""),
            valid.replace('check_id = "2"\n', ""),
            valid.replace('kind = "mechanical"', 'kind = "other"'),
            valid.replace('kind = "mechanical"', 'kind = "semantic"'),
            valid + valid[valid.index("[[criteria]]") :],
            "requirements_version = 1\ncriteria = []\n",
            'requirements_version = 1\ncriteria = "bad"\n',
            "requirements_version = 1\nunknown = true\ncriteria = []\n",
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)

    def test_binding_rejects_unknown_checks_and_custom_named_tests(self):
        for criterion in (replace(self.criterion(), check_id="missing"),):
            with self.assertRaises(ValueError):
                self.bound(criterion)
        custom = replace(self.plan, checks=(replace(self.plan.checks[0], argv=("make", "test")),))
        with self.assertRaises(ValueError):
            self.bound(plan=custom)
        self.bound(replace(self.criterion(), test_name=None), custom)
        duplicate = replace(self.plan, checks=self.plan.checks * 2)
        with self.assertRaises(ValueError):
            self.bound(plan=duplicate)
        with self.assertRaises(ValueError):
            self.bound(plan=replace(self.plan, profile="focused"))

    def test_hashes_include_every_parsed_field_and_order(self):
        requirements = self.bound().requirements
        canonical = json.dumps(asdict(requirements), sort_keys=True, separators=(",", ":"))
        self.assertEqual(
            req.requirements_id(requirements), hashlib.sha256(canonical.encode()).hexdigest()
        )
        for key, value in (
            ("description", "other"),
            ("verification", "other"),
            ("kind", "semantic"),
            ("check_id", "3"),
            ("test_name", None),
        ):
            changed = replace(requirements, criteria=(replace(self.criterion(), **{key: value}),))
            self.assertNotEqual(req.requirements_id(changed), req.requirements_id(requirements))
        canonical = json.dumps(asdict(self.plan), sort_keys=True, separators=(",", ":"))
        self.assertEqual(
            req.verification_plan_id(self.plan), hashlib.sha256(canonical.encode()).hexdigest()
        )
        for key, value in (
            ("profile", "focused"),
            ("variants", ("other",)),
            ("package", "p"),
            ("test_filter", "t"),
            ("checks", (replace(self.plan.checks[0], argv=("cargo", "check")),)),
        ):
            self.assertNotEqual(
                req.verification_plan_id(replace(self.plan, **{key: value})),
                req.verification_plan_id(self.plan),
            )

    def test_completed_named_test_uses_full_log_beyond_tail(self):
        log = "running 1 test\ntest validation::upper ... ok\n" + "noise\n" * 5000
        log += "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n"
        record = self.record(log)
        original = copy.deepcopy(record)
        self.assertEqual(self.verdict([record]), "satisfied")
        self.assertEqual(record, original)

    def test_should_panic_annotation_preserves_exact_named_test_proof(self):
        log = (
            "running 1 test\ntest rejects_invalid_input - should panic ... ok\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        )
        bound = self.bound(self.criterion(test_name="rejects_invalid_input"))
        self.assertEqual(self.verdict([self.record(log)], bound), "satisfied")

    def test_should_panic_annotation_does_not_relax_exact_name_or_known_suffix(self):
        for display_name in (
            "validation::upper_extra - should panic",
            "other - should panic",
            "validation::upper - arbitrary annotation",
            "validation::upper - should panic extra",
        ):
            with self.subTest(display_name=display_name):
                log = (
                    f"running 1 test\ntest {display_name} ... ok\n"
                    "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
                )
                self.assertEqual(self.verdict([self.record(log)]), "unmet")

    def test_stale_missing_duplicate_or_mismatched_proof_is_unresolved(self):
        record = self.record(
            "test validation::upper ... ok\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        )
        for records in (
            [],
            [record, record],
            [dict(record, snapshot_id="old")],
            [dict(record, result_snapshot_id="old")],
            [dict(record, source_unchanged=False)],
            [dict(record, command=["cargo", "test"])],
            [dict(record, kind="preparation")],
            [dict(record, profile="focused")],
            [dict(record, variant="other")],
        ):
            with self.subTest(records=records):
                self.assertEqual(self.verdict(records), "unresolved")
        bound = self.bound()
        for key in self.binding(bound):
            binding = dict(self.binding(bound), **{key: "stale"})
            expected = "unresolved"
            self.assertEqual(self.verdict([record], bound, binding), expected)

    def test_failure_and_time_limits_cannot_satisfy(self):
        record = self.record()
        for changes in (
            dict(passed=False),
            dict(returncode=1),
            dict(timed_out=True),
            dict(output_limit_exceeded=True),
            dict(error="failed"),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.verdict([dict(record, **changes)]), "unmet")

    def test_zero_ignored_only_and_missing_exact_test_are_unmet(self):
        for log in (
            "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n",
            "test validation::upper ... ignored\ntest result: ok. 0 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out\n",
            "test validation::upper_extra ... ok\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n",
        ):
            with self.subTest(log=log):
                self.assertEqual(self.verdict([self.record(log)]), "unmet")
        check_only = self.bound(replace(self.criterion(), test_name=None))
        self.assertEqual(
            self.verdict(
                [
                    self.record(
                        "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
                    )
                ],
                check_only,
            ),
            "unmet",
        )

    def test_unknown_missing_or_incomplete_harness_output_is_unresolved(self):
        for log in (
            None,
            "all tests passed",
            "test validation::upper ... ok\n",
            "test validation::upper ... ok\ntest result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n",
        ):
            with self.subTest(log=log):
                self.assertEqual(self.verdict([self.record(log)]), "unresolved")

    def test_cargo_toolchain_and_custom_success_and_nonexecuting_test_flags(self):
        custom = replace(self.plan, checks=(replace(self.plan.checks[0], argv=("make", "check")),))
        bound = self.bound(replace(self.criterion(), test_name=None), custom)
        self.assertEqual(
            self.verdict([dict(self.record(), command=["make", "check"])], bound), "satisfied"
        )
        for argv in (("cargo", "test", "--no-run"), ("cargo", "test", "--", "--list")):
            plan = replace(self.plan, checks=(replace(self.plan.checks[0], argv=argv),))
            bound = self.bound(plan=plan)
            record = dict(
                self.record(
                    "test validation::upper ... ok\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
                ),
                command=list(argv),
            )
            self.assertEqual(self.verdict([record], bound), "unmet")
        argv = ("cargo", "+stable", "test")
        plan = replace(self.plan, checks=(replace(self.plan.checks[0], argv=argv),))
        bound = self.bound(plan=plan)
        record = dict(
            self.record(
                "test validation::upper ... ok\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
            ),
            command=list(argv),
        )
        self.assertEqual(self.verdict([record], bound), "satisfied")


class RequirementsBoundaryTests(unittest.TestCase):
    setUp = RequirementsTests.setUp
    criterion = RequirementsTests.criterion
    bound = RequirementsTests.bound
    binding = RequirementsTests.binding
    record = RequirementsTests.record
    verdict = RequirementsTests.verdict

    def test_programmatic_mutable_or_malformed_inputs_are_rejected(self):
        invalid = [
            req.Requirements(criteria=[self.criterion()]),
            req.Requirements(requirements_version=True, criteria=(self.criterion(),)),
            req.Requirements(criteria=(self.criterion(), self.criterion())),
            req.Requirements(criteria=(replace(self.criterion(), kind=[]),)),
            req.Requirements(criteria=(replace(self.criterion(), test_name=2),)),
            req.Requirements(criteria=(replace(self.criterion(), description=False),)),
            req.Requirements(criteria=(replace(self.criterion(), verification=" "),)),
            req.Requirements(criteria=(replace(self.criterion(), id=[]),)),
            req.Requirements(criteria=(replace(self.criterion(), check_id=2),)),
            req.Requirements(criteria=(replace(self.criterion(), kind="semantic", check_id=None),)),
        ]
        for requirements in invalid:
            with self.subTest(requirements=requirements), self.assertRaises(req.RequirementsError):
                req.bind_requirements(requirements, self.plan)
        for plan in (
            replace(self.plan, checks=list(self.plan.checks)),
            replace(self.plan, variants=["default"]),
            replace(self.plan, variants=([],)),
            replace(self.plan, checks=(replace(self.plan.checks[0], argv=["cargo", "test"]),)),
        ):
            with self.subTest(plan=plan), self.assertRaises(req.RequirementsError):
                self.bound(plan=plan)

    def test_named_test_must_belong_to_the_successful_completed_harness(self):
        log = (
            "running 1 test\ntest validation::upper ... ok\n"
            "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n"
            "running 1 test\ntest other ... ok\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        )
        self.assertEqual(self.verdict([self.record(log)]), "unmet")
        record = self.record("test validation::upper ... ok\n")
        (self.root / "checks/0/stderr.txt").write_text(
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n",
            encoding="utf-8",
        )
        self.assertEqual(self.verdict([record]), "unmet")

    def test_log_paths_cannot_escape_the_audit_directory(self):
        record = self.record(
            "test validation::upper ... ok\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        )
        record["artifacts"] = ".."
        self.assertEqual(self.verdict([record]), "unresolved")
        (self.root / "checks/0/stdout.txt").unlink()
        (self.root / "checks/0/stdout.txt").symlink_to("/etc/passwd")
        self.assertEqual(self.verdict([self.record()]), "unresolved")

    def test_semantic_criteria_emit_no_mechanical_approval(self):
        criterion = req.Criterion("scope", "Keep API", "semantic", "Review source")
        bound = self.bound(criterion)
        self.assertEqual(req.evaluate_mechanical(bound, [], self.binding(bound), self.root), [])

    def test_hashes_preserve_requirement_check_and_variant_order(self):
        second = replace(self.criterion(), id="second")
        first = req.Requirements(criteria=(self.criterion(), second))
        self.assertNotEqual(
            req.requirements_id(first),
            req.requirements_id(replace(first, criteria=(second, self.criterion()))),
        )
        check = replace(self.plan.checks[0], id="3")
        plan = replace(self.plan, checks=(*self.plan.checks, check), variants=("default", "other"))
        self.assertNotEqual(
            req.verification_plan_id(plan),
            req.verification_plan_id(replace(plan, checks=tuple(reversed(plan.checks)))),
        )
        self.assertNotEqual(
            req.verification_plan_id(plan),
            req.verification_plan_id(replace(plan, variants=("other", "default"))),
        )


class CargoInvocationProofTests(unittest.TestCase):
    setUp = RequirementsTests.setUp
    criterion = RequirementsTests.criterion
    bound = RequirementsTests.bound
    binding = RequirementsTests.binding
    record = RequirementsTests.record
    verdict = RequirementsTests.verdict

    COMMANDS = (
        ("/usr/bin/cargo", "test", "missing_filter"),
        ("cargo.exe", "test", "missing_filter"),
        (r"C:\Rust\bin\cargo.exe", "test", "missing_filter"),
        ("/opt/rust/bin/cargo", "+stable", "test", "missing_filter"),
        ("cargo", "--color", "never", "test", "missing_filter"),
        ("cargo", "--color=never", "test", "missing_filter"),
        ("cargo", "--config", "net.offline=true", "test", "missing_filter"),
        ("cargo", "--config=net.offline=true", "-Zunstable-options", "-Cproject", "test"),
        ("cargo", "+nightly", "-Z", "unstable-options", "-C", "project", "test"),
        ("cargo", "--offline", "--locked", "--frozen", "-vv", "-q", "test"),
        ("cargo", "t", "missing_filter"),
    )

    def test_every_direct_cargo_form_requires_nonzero_test_execution(self):
        zero = (
            "running 0 tests\n"
            "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        )
        for argv in self.COMMANDS:
            with self.subTest(argv=argv):
                plan = replace(self.plan, checks=(replace(self.plan.checks[0], argv=argv),))
                bound = self.bound(replace(self.criterion(), test_name=None), plan)
                record = dict(self.record(zero), command=list(argv))
                self.assertEqual(self.verdict([record], bound), "unmet")
                self.assertEqual(bound.gate_plan.checks[0].argv, argv)

    def test_named_positive_proof_uses_same_direct_cargo_classification(self):
        positive = (
            "running 1 test\ntest validation::upper ... ok\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        )
        for argv in self.COMMANDS:
            with self.subTest(argv=argv):
                plan = replace(self.plan, checks=(replace(self.plan.checks[0], argv=argv),))
                bound = self.bound(plan=plan)
                record = dict(self.record(positive), command=list(argv))
                self.assertEqual(self.verdict([record], bound), "satisfied")
                for suffix in (("--no-run",), ("--", "--list")):
                    nonexecuting = (*argv, *suffix)
                    plan = replace(
                        self.plan, checks=(replace(self.plan.checks[0], argv=nonexecuting),)
                    )
                    bound = self.bound(plan=plan)
                    self.assertEqual(
                        self.verdict([dict(record, command=list(nonexecuting))], bound), "unmet"
                    )

    def test_unknown_or_incomplete_cargo_global_grammar_fails_closed(self):
        for argv in (
            ("cargo", "--unknown", "test"),
            ("cargo", "--color"),
            ("cargo", "--config=", "test"),
            ("cargo", "+", "test"),
        ):
            with self.subTest(argv=argv), self.assertRaises(req.RequirementsError):
                plan = replace(self.plan, checks=(replace(self.plan.checks[0], argv=argv),))
                self.bound(replace(self.criterion(), test_name=None), plan)


class MiriInvocationProofTests(unittest.TestCase):
    setUp = RequirementsTests.setUp
    criterion = RequirementsTests.criterion
    bound = RequirementsTests.bound
    binding = RequirementsTests.binding
    record = RequirementsTests.record
    verdict = RequirementsTests.verdict

    COMMANDS = (
        ("cargo", "miri", "test"),
        ("/usr/bin/cargo", "+nightly", "miri", "test", "validation::upper"),
        (r"C:\Rust\bin\cargo.exe", "+nightly", "--color=never", "miri", "test"),
        ("cargo", "--config", "net.offline=true", "-Zunstable-options", "miri", "test"),
        ("cargo", "+nightly", "-Z", "unstable-options", "-C", "project", "miri", "test"),
        ("cargo", "--offline", "--locked", "--frozen", "-vv", "-q", "miri", "test"),
    )
    POSITIVE = (
        "running 1 test\ntest validation::upper ... ok\n"
        "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
    )

    def miri_plan(self, argv=("cargo", "+nightly", "miri", "test")):
        return replace(self.plan, checks=(replace(self.plan.checks[0], argv=argv),))

    def test_legacy_unnamed_miri_success_does_not_require_logs(self):
        plan = self.miri_plan()
        bound = self.bound(self.criterion(test_name=None), plan)
        record = dict(self.record(), command=list(plan.checks[0].argv))
        self.assertEqual(self.verdict([record], bound), "satisfied")

    def test_named_miri_without_logs_cannot_prove_test_execution(self):
        plan = self.miri_plan()
        bound = self.bound(plan=plan)
        record = dict(self.record(), command=list(plan.checks[0].argv))
        self.assertEqual(self.verdict([record], bound), "unresolved")

    def test_named_miri_requires_completed_nonempty_exact_test_proof(self):
        cases = (
            ("", "unresolved"),
            ("all tests passed\n", "unresolved"),
            ("test validation::upper ... ok\n", "unresolved"),
            (
                "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n",
                "unmet",
            ),
            (
                "test validation::upper ... ignored\n"
                "test result: ok. 0 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out\n",
                "unmet",
            ),
            (
                "test validation::upper_extra ... ok\n"
                "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n",
                "unmet",
            ),
            (self.POSITIVE, "satisfied"),
        )
        for argv in self.COMMANDS:
            with self.subTest(argv=argv):
                bound = self.bound(plan=self.miri_plan(argv))
                for output, expected in cases:
                    with self.subTest(output=output):
                        record = dict(self.record(output), command=list(argv))
                        self.assertEqual(self.verdict([record], bound), expected)

    def test_legacy_unnamed_miri_success_keeps_command_only_proof(self):
        plan = self.miri_plan()
        bound = self.bound(self.criterion(test_name=None), plan)
        record = dict(
            self.record(
                "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
            ),
            command=list(plan.checks[0].argv),
        )
        self.assertEqual(self.verdict([record], bound), "satisfied")

    def test_miri_nonexecuting_flags_cannot_prove_named_test(self):
        for suffix in (("--no-run",), ("--", "--list")):
            with self.subTest(suffix=suffix):
                argv = ("cargo", "+nightly", "miri", "test", *suffix)
                bound = self.bound(plan=self.miri_plan(argv))
                record = dict(self.record(self.POSITIVE), command=list(argv))
                self.assertEqual(self.verdict([record], bound), "unmet")

    def test_miri_stale_or_incomplete_validation_record_cannot_prove_named_test(self):
        plan = self.miri_plan()
        bound = self.bound(plan=plan)
        record = dict(self.record(self.POSITIVE), command=list(plan.checks[0].argv))
        for changes in (
            {"snapshot_id": "stale"},
            {"result_snapshot_id": "stale"},
            {"source_unchanged": False},
            {"command": ["cargo", "miri", "test"]},
            {"returncode": None},
            {"timed_out": None},
            {"output_limit_exceeded": None},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.verdict([dict(record, **changes)], bound), "unresolved")
        for key in self.binding(bound):
            with self.subTest(binding_key=key):
                binding = dict(self.binding(bound), **{key: "stale"})
                self.assertEqual(self.verdict([record], bound, binding), "unresolved")

    def test_miri_named_test_must_belong_to_completed_successful_harness(self):
        bound = self.bound(plan=self.miri_plan())
        output = (
            "running 1 test\ntest validation::upper ... ok\n"
            "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n"
            "running 1 test\ntest other ... ok\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        )
        record = dict(self.record(output), command=list(bound.gate_plan.checks[0].argv))
        self.assertEqual(self.verdict([record], bound), "unmet")

    def test_classification_distinguishes_direct_tests_and_miri_from_other_commands(self):
        for commands, expected in (
            (CargoInvocationProofTests.COMMANDS, "test"),
            (self.COMMANDS, "miri"),
            (
                (
                    ("cargo", "miri"),
                    ("cargo", "miri", "setup", "test"),
                    ("cargo", "miri", "run", "test"),
                    ("cargo", "miri", "--help", "test"),
                    ("cargo", "nextest", "run"),
                    ("cargo", "check", "test"),
                    ("make", "test"),
                    ("rustup", "run", "nightly", "cargo", "miri", "test"),
                    ("sh", "-c", "cargo miri test"),
                ),
                None,
            ),
        ):
            for argv in commands:
                with self.subTest(argv=argv):
                    self.assertEqual(req.cargo_test_kind(argv), expected)

    def test_miri_other_subcommands_cannot_bind_named_test(self):
        for argv in (
            ("cargo", "miri"),
            ("cargo", "miri", "setup", "test"),
            ("cargo", "miri", "run", "test"),
            ("cargo", "miri", "--help", "test"),
        ):
            with self.subTest(argv=argv), self.assertRaises(req.RequirementsError):
                self.bound(plan=self.miri_plan(argv))

    def test_miri_global_grammar_preserves_fail_closed_errors(self):
        for argv in (
            ("cargo", "--unknown", "miri", "test"),
            ("cargo", "--color", "miri", "test"),
            ("cargo", "--config=", "miri", "test"),
            ("cargo", "-Z", "--offline", "miri", "test"),
            ("cargo", "+", "miri", "test"),
        ):
            with self.subTest(argv=argv), self.assertRaises(req.RequirementsError):
                req.cargo_test_kind(argv)
