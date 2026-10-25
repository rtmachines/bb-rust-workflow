"""Diagnostics evidence must never turn missing observations into resolved findings."""

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path

from bb_workflow.diagnostics import (
    DiagnosticContext,
    compare_diagnostics,
    parse_diagnostics,
    summarize_diagnostics,
)

ROOT = Path("/work/baseline")
CONTEXT = DiagnosticContext(
    profile="gate",
    variant="default",
    argv=("cargo", "check"),
    scope={"package": None, "test_filter": None},
    tool_versions={"cargo": "cargo 1.90.0", "rustc": "rustc 1.90.0"},
    configuration_digest="config-hash",
)
EXECUTION = {
    "returncode": 0,
    "timed_out": False,
    "output_limit_exceeded": False,
    "source_unchanged": True,
    "passed": True,
}
FINISHED = json.dumps({"reason": "build-finished", "success": True})
TEST_CONTEXT = replace(
    CONTEXT,
    argv=("cargo", "test"),
    tool_versions={**CONTEXT.tool_versions, "rustdoc": "rustdoc 1.90.0"},
)
HEADER = "     Running unittests src/lib.rs (target/debug/deps/demo-deadbeef)\n"


def diagnostic(
    root=ROOT,
    *,
    line=4,
    message="unused variable: `x`",
    code="unused_variables",
    level="warning",
    path="src/lib.rs",
    anchor="let x = 1;",
):
    return {
        "$message_type": "diagnostic",
        "message": message,
        "code": {"code": code, "explanation": None},
        "level": level,
        "spans": [
            {
                "file_name": str(root / path) if not path.startswith("/") else path,
                "line_start": line,
                "line_end": line,
                "column_start": 5,
                "column_end": 6,
                "byte_start": 10,
                "byte_end": 11,
                "is_primary": True,
                "text": [{"text": anchor, "highlight_start": 5, "highlight_end": 6}],
                "label": None,
            }
        ],
        "children": [],
        "rendered": None,
    }


def cargo(root=ROOT, **kwargs):
    return json.dumps(
        {
            "reason": "compiler-message",
            "package_id": "path+file://" + str(root) + "#demo@0.1.0",
            "manifest_path": str(root / "Cargo.toml"),
            "target": {"name": "demo", "kind": ["lib"], "src_path": str(root / "src/lib.rs")},
            "message": diagnostic(root, **kwargs),
        }
    )


def parse(stdout="", stderr="", *, context=CONTEXT, execution=None, root=ROOT):
    return parse_diagnostics(
        stdout,
        stderr,
        project_root=root,
        context=context,
        execution=EXECUTION if execution is None else execution,
    )


def harness(outcome="FAILED", *, filtered=0, count=1, name="tests::works"):
    failed = outcome == "FAILED"
    return (
        f"running {count} tests\ntest {name} ... {outcome}\n"
        f"test result: {'FAILED' if failed else 'ok'}. "
        f"{0 if failed else count} passed; {count if failed else 0} failed; "
        f"0 ignored; 0 measured; {filtered} filtered out; finished in 0.00s\n"
    )


def test_report(outcome="FAILED", *, stdout=None, stderr=HEADER, execution=None):
    if execution is None and outcome == "FAILED":
        execution = dict(EXECUTION, returncode=101, passed=False)
    return parse(
        harness(outcome) if stdout is None else stdout,
        stderr,
        context=TEST_CONTEXT,
        execution=execution,
    )


class ParserTests(unittest.TestCase):
    def test_cargo_embeds_rustc_and_retains_locations(self):
        report = parse(cargo() + "\n" + FINISHED)
        (finding,) = report["findings"]
        self.assertEqual(finding["domain"], "compiler")
        self.assertEqual(finding["severity"], "warning")
        self.assertEqual(finding["code"], "unused_variables")
        self.assertEqual(finding["locations"][0]["path"], "src/lib.rs")
        self.assertEqual(finding["locations"][0]["line"], 4)
        self.assertEqual(finding["occurrences"], 1)
        self.assertTrue(report["domains"]["compiler"]["complete"])
        json.dumps(report)

    def test_direct_rustc_json_from_stderr(self):
        context = replace(CONTEXT, argv=("rustc", "--error-format=json", "src/lib.rs"))
        report = parse("", json.dumps(diagnostic()), context=context)
        self.assertEqual(len(report["findings"]), 1)
        self.assertTrue(report["domains"]["compiler"]["complete"])

    def test_text_rustc_and_clippy_with_source_anchor(self):
        stderr = (
            "warning: unused variable: `x`\n --> src/lib.rs:4:5\n"
            "  |\n4 | let x = 1;\n  |     ^ help: prefix it with an underscore\n"
            "  = note: `#[warn(unused_variables)]` on by default\n"
            "warning: needless borrow\n --> src/lib.rs:9:3\n"
            "9 | let y = &x;\n  = note: `#[warn(clippy::needless_borrow)]` on by default\n"
            "warning: `demo` (lib) generated 2 warnings\n"
            "    Finished `dev` profile [unoptimized] target(s) in 0.01s\n"
        )
        report = parse("", stderr, context=replace(CONTEXT, argv=("cargo", "clippy")))
        self.assertEqual(
            [f["code"] for f in report["findings"]], ["unused_variables", "clippy::needless_borrow"]
        )
        self.assertEqual(report["findings"][0]["locations"][0]["source"], "let x = 1;")
        self.assertTrue(report["domains"]["compiler"]["complete"])

    def test_full_streams_independent_do_not_hide_early_records(self):
        report = parse(cargo() + "\n" + FINISHED, "\n" * 15000)
        self.assertEqual(len(report["findings"]), 1)
        self.assertTrue(report["domains"]["compiler"]["complete"])

    def test_malformed_or_unrecognized_records_block_absence_evidence(self):
        for extra in (
            "{broken",
            '{"reason":"compiler-message","message":null}',
            '{"$message_type":"diagnostic","spans":"bad"}',
            '{"reason":"build-finished","success":"true"}',
            "custom success",
            '{"type":"suite","event":"ok"}',
        ):
            with self.subTest(extra=extra):
                report = parse(cargo() + "\n" + FINISHED + "\n" + extra)
                self.assertEqual(len(report["findings"]), 1)
                self.assertFalse(report["domains"]["compiler"]["complete"])
                self.assertTrue(report["uncertainty"])

    def test_unknown_rustc_severity_and_malformed_span_prevent_resolution(self):
        for mutate in ("severity", "primary", "label"):
            record = diagnostic()
            if mutate == "severity":
                record["level"] = "future-severity"
            elif mutate == "primary":
                record["spans"][0]["is_primary"] = "yes"
            else:
                record["spans"][0]["label"] = {"unsupported": True}
            with self.subTest(mutate=mutate):
                current = parse(json.dumps(record) + "\n" + FINISHED)
                self.assertTrue(current["uncertainty"])
                self.assertEqual(
                    compare_diagnostics(current, parse(cargo()))["counts"]["resolved"], 0
                )

    def test_malformed_rustc_coordinates_cannot_authorize_resolution(self):
        invalid = [
            ("line_start", {"x": "x" * 100000}),
            ("line_start", True),
            ("line_start", 0),
            ("line_start", None),
            ("line_start", "4"),
            ("column_start", 0),
            ("column_end", -1),
            ("line_end", 3),
            ("column_end", 4),
            ("byte_start", -1),
            ("byte_end", 9),
            ("byte_end", 11.5),
            ("byte_start", False),
        ]
        for key, value in invalid:
            with self.subTest(key=key, value_type=type(value).__name__):
                record = diagnostic()
                record["spans"][0][key] = value
                current = parse(json.dumps(record) + "\n" + FINISHED)
                self.assertTrue(current["uncertainty"])
                self.assertFalse(current["domains"]["compiler"]["complete"])
                self.assertLess(len(json.dumps(summarize_diagnostics(current))), 12000)
                compared = compare_diagnostics(current, parse(cargo()))
                self.assertEqual(compared["counts"]["resolved"], 0)
                self.assertEqual(compared["counts"]["unobserved_previous"], 1)
        record = diagnostic()
        del record["spans"][0]["line_start"]
        self.assertTrue(parse(json.dumps(record))["uncertainty"])

    def test_missing_rustc_code_is_uncertain_but_explicit_null_is_supported(self):
        missing = diagnostic()
        del missing["code"]
        current = parse(json.dumps(missing) + "\n" + FINISHED)
        self.assertTrue(current["uncertainty"])
        self.assertFalse(current["domains"]["compiler"]["complete"])
        result = compare_diagnostics(current, parse(cargo()))
        self.assertEqual(result["counts"]["resolved"], 0)
        self.assertEqual(result["counts"]["unobserved_previous"], 1)
        explicit = diagnostic()
        explicit["code"] = None
        current = parse(json.dumps(explicit) + "\n" + FINISHED)
        self.assertFalse(current["uncertainty"])
        self.assertIsNone(current["findings"][0]["code"])
        self.assertTrue(current["domains"]["compiler"]["complete"])

    def test_text_source_anchor_roots_match_between_snapshots(self):
        def text(root):
            return (
                f'warning: unused variable: `x`\n --> src/lib.rs:4:5\n4 | let x = "{root}/data";\n'
            )

        other_root = Path("/scratch/candidate")
        result = compare_diagnostics(
            parse("", text(other_root), root=other_root), parse("", text(ROOT))
        )
        self.assertEqual(result["counts"]["persistent"], 1)
        self.assertEqual(result["counts"]["new"], 0)
        self.assertEqual(result["counts"]["resolved"], 0)

    def test_unknown_commands_do_not_create_complete_observations(self):
        report = parse("", "", context=replace(CONTEXT, argv=("custom-check",)))
        self.assertFalse(report["domains"]["compiler"]["complete"])
        self.assertFalse(report["domains"]["test"]["complete"])

    def test_build_success_never_means_tests_ran(self):
        report = parse(FINISHED, context=TEST_CONTEXT)
        self.assertFalse(report["domains"]["test"]["observed"])
        self.assertFalse(report["domains"]["test"]["complete"])

    def test_explicit_libtest_failures_and_passes_are_separate_evidence(self):
        failed = test_report()
        passed = test_report("ok")
        self.assertEqual(failed["findings"][0]["test_name"], "tests::works")
        self.assertEqual(failed["findings"][0]["domain"], "test")
        self.assertEqual(passed["findings"], [])
        (suite,) = passed["domains"]["test"]["suites"]
        self.assertEqual(suite["passed"], ["tests::works"])
        self.assertTrue(suite["complete"])
        self.assertTrue(suite["identity"])

    def test_external_paths_are_preserved_and_project_paths_rebased(self):
        report = parse(cargo(path="/registry/dependency/src/lib.rs"))
        self.assertEqual(
            report["findings"][0]["locations"][0]["path"], "/registry/dependency/src/lib.rs"
        )
        external = parse(cargo(path="/work/baseline-other/src/lib.rs"))
        self.assertEqual(
            external["findings"][0]["locations"][0]["path"], "/work/baseline-other/src/lib.rs"
        )

    def test_relative_external_paths_keep_absolute_dependency_identity(self):
        record = diagnostic()
        record["spans"][0]["file_name"] = "../dependency/src/lib.rs"
        report = parse(json.dumps(record))
        self.assertEqual(
            report["findings"][0]["locations"][0]["path"], "/work/dependency/src/lib.rs"
        )

    def test_root_paths_in_source_labels_match_between_snapshots(self):
        other_root = Path("/scratch/candidate")
        left = diagnostic()
        left["spans"][0]["label"] = "declared in /work/baseline/src/lib.rs"
        right = diagnostic(other_root)
        right["spans"][0]["label"] = "declared in /scratch/candidate/src/lib.rs"
        result = compare_diagnostics(
            parse(json.dumps(right), root=other_root), parse(json.dumps(left))
        )
        self.assertEqual(result["counts"]["persistent"], 1)

    def test_duplicates_retain_counts_and_all_locations(self):
        report = parse(cargo() + "\n" + cargo(line=40))
        (finding,) = report["findings"]
        self.assertEqual(finding["occurrences"], 2)
        self.assertEqual([x["line"] for x in finding["locations"]], [4, 40])

    def test_execution_input_and_context_are_copied(self):
        execution = dict(EXECUTION)
        context = replace(CONTEXT, scope={"nested": {"value": 1}})
        report = parse(cargo(), execution=execution, context=context)
        context.scope["nested"]["value"] = 2
        execution["returncode"] = 100
        self.assertEqual(report["context"]["scope"], {"nested": {"value": 1}})
        self.assertTrue(report["execution"]["successful"])


class ComparisonTests(unittest.TestCase):
    def test_no_baseline_is_unclassified_with_all_classification_lists(self):
        compared = compare_diagnostics(parse(cargo()), None)
        self.assertEqual(
            compared["counts"],
            {"new": 0, "persistent": 0, "resolved": 0, "unclassified": 1, "unobserved_previous": 0},
        )
        self.assertTrue(compared["reasons"])
        for name in compared["counts"]:
            self.assertIsInstance(compared[name], list)

    def test_changed_roots_and_lines_match_without_local_package_url(self):
        baseline = parse(cargo())
        new_root = Path("/scratch/candidate")
        current = parse(cargo(new_root, line=100), root=new_root)
        compared = compare_diagnostics(current, baseline)
        self.assertEqual(compared["counts"]["persistent"], 1)
        self.assertEqual(compared["counts"]["new"], 0)

    def test_fingerprint_distinguishes_severity_code_path_message_and_source(self):
        baseline = parse(cargo() + "\n" + FINISHED)
        for change in (
            {"level": "error"},
            {"code": "different"},
            {"path": "src/main.rs"},
            {"message": "unused variable: `y`"},
            {"anchor": "let x = 2;"},
        ):
            with self.subTest(change=change):
                current = parse(cargo(**change) + "\n" + FINISHED)
                compared = compare_diagnostics(current, baseline)
                self.assertEqual(compared["counts"]["new"], 1)
                self.assertEqual(compared["counts"]["persistent"], 0)

    def test_incomparable_or_unknown_context_never_classifies(self):
        baseline = parse(cargo() + "\n" + FINISHED)
        changes = (
            {"profile": "focused"},
            {"variant": "other"},
            {"argv": ("cargo", "clippy")},
            {"scope": {"package": "other"}},
            {"tool_versions": {"rustc": "changed"}},
            {"configuration_digest": "changed"},
            {"configuration_digest": None},
            {"tool_versions": {"rustc": None}},
            {"tool_versions": {}},
            {"scope": {}},
            {"profile": ""},
            {"argv": ()},
        )
        for change in changes:
            with self.subTest(change=change):
                current = parse(cargo() + "\n" + FINISHED, context=replace(CONTEXT, **change))
                compared = compare_diagnostics(current, baseline)
                self.assertEqual(compared["counts"]["unclassified"], 1)
                self.assertEqual(compared["counts"]["unobserved_previous"], 1)
                self.assertEqual(compared["counts"]["persistent"], 0)
        missing = parse(cargo(), context=replace(CONTEXT, configuration_digest=None))
        self.assertEqual(compare_diagnostics(missing, missing)["counts"]["unclassified"], 1)

    def test_matching_finding_is_persistent_with_partial_current_output(self):
        current = parse(
            cargo() + "\n{truncated",
            execution=dict(EXECUTION, timed_out=True, passed=False, returncode=-9),
        )
        result = compare_diagnostics(current, parse(cargo() + "\n" + FINISHED))
        self.assertEqual(result["counts"]["persistent"], 1)
        self.assertEqual(result["counts"]["resolved"], 0)

    def test_new_requires_sufficient_baseline_domain_observation(self):
        baseline = parse("", execution=dict(EXECUTION, returncode=101, passed=False))
        result = compare_diagnostics(parse(cargo()), baseline)
        self.assertEqual(result["counts"]["new"], 0)
        self.assertEqual(result["counts"]["unclassified"], 1)
        observed = compare_diagnostics(parse(cargo()), parse(FINISHED))
        self.assertEqual(observed["counts"]["new"], 1)

    def test_missing_compiler_finding_resolves_only_after_completed_valid_success(self):
        baseline = parse(cargo())
        self.assertEqual(compare_diagnostics(parse(FINISHED), baseline)["counts"]["resolved"], 1)
        for execution in (
            dict(EXECUTION, returncode=101, passed=False),
            dict(EXECUTION, timed_out=True),
            dict(EXECUTION, output_limit_exceeded=True),
            dict(EXECUTION, source_unchanged=False),
            dict(EXECUTION, interrupted=True),
            dict(EXECUTION, returncode=-1),
            {},
            dict(EXECUTION, passed=False),
        ):
            with self.subTest(execution=execution):
                compared = compare_diagnostics(parse(FINISHED, execution=execution), baseline)
                self.assertEqual(compared["counts"]["resolved"], 0)
                self.assertEqual(compared["counts"]["unobserved_previous"], 1)

    def test_failed_compilation_cannot_resolve_previous_test(self):
        current = parse(
            cargo(level="error")
            + "\n"
            + json.dumps({"reason": "build-finished", "success": False}),
            context=TEST_CONTEXT,
            execution=dict(EXECUTION, returncode=101, passed=False),
        )
        result = compare_diagnostics(current, test_report())
        self.assertEqual(result["counts"]["resolved"], 0)
        self.assertEqual(result["counts"]["unobserved_previous"], 1)

    def test_negative_build_evidence_invalidates_even_an_explicit_test_pass(self):
        current = test_report(
            "ok", stdout=harness("ok") + json.dumps({"reason": "build-finished", "success": False})
        )
        self.assertFalse(current["domains"]["test"]["complete"])
        self.assertTrue(current["domains"]["test"]["uncertainty"])
        result = compare_diagnostics(current, test_report())
        self.assertEqual(result["counts"]["resolved"], 0)
        self.assertEqual(result["counts"]["unobserved_previous"], 1)

    def test_corresponding_explicit_pass_resolves_test_failure(self):
        result = compare_diagnostics(test_report("ok"), test_report())
        self.assertEqual(result["counts"]["resolved"], 1)
        self.assertEqual(result["counts"]["unobserved_previous"], 0)

    def test_test_absence_or_other_test_pass_is_not_a_resolution(self):
        for stdout in (
            "",
            "running 0 tests\ntest result: ok. 0 passed; 0 failed; "
            "0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n",
            harness("ok", name="tests::other"),
            harness("ok", filtered=1),
        ):
            with self.subTest(stdout=stdout):
                result = compare_diagnostics(test_report("ok", stdout=stdout), test_report())
                self.assertEqual(result["counts"]["resolved"], 0)
                self.assertEqual(result["counts"]["unobserved_previous"], 1)

    def test_bad_execution_or_output_cannot_resolve_explicit_test_pass(self):
        for execution in (
            dict(EXECUTION, returncode=101, passed=False),
            dict(EXECUTION, timed_out=True),
            dict(EXECUTION, output_limit_exceeded=True),
            dict(EXECUTION, source_unchanged=False),
            dict(EXECUTION, interrupted=True),
        ):
            with self.subTest(execution=execution):
                result = compare_diagnostics(test_report("ok", execution=execution), test_report())
                self.assertEqual(result["counts"]["resolved"], 0)
        for extra in (
            "\n{bad",
            "\ncustom harness says success",
            "\nfailures:\ncustom harness says success",
        ):
            result = compare_diagnostics(
                test_report("ok", stdout=harness("ok") + extra), test_report()
            )
            self.assertEqual(result["counts"]["resolved"], 0)

    def test_missing_and_ambiguous_suite_identity_prevent_false_matches(self):
        for header in ("", HEADER + HEADER):
            with self.subTest(header=header):
                baseline = test_report(stderr=header)
                current = test_report("ok", stderr=header)
                self.assertEqual(compare_diagnostics(current, baseline)["counts"]["resolved"], 0)
                matching = compare_diagnostics(test_report(stderr=header), baseline)
                self.assertEqual(matching["counts"]["persistent"], 0)
                self.assertEqual(matching["counts"]["unclassified"], 1)

    def test_repeated_names_in_distinct_suites_are_distinct(self):
        other = "     Running tests/integration.rs (target/debug/deps/integration-cafe)\n"
        baseline = test_report(stdout=harness() + harness(), stderr=HEADER + other)
        current = test_report(
            "ok",
            stdout=harness("ok") + harness(),
            stderr=HEADER + other,
            execution=dict(EXECUTION, returncode=101, passed=False),
        )
        compared = compare_diagnostics(current, baseline)
        self.assertEqual(len(baseline["findings"]), 2)
        self.assertEqual(compared["counts"]["persistent"], 1)
        # The failed overall command makes the other pass insufficient for resolution.
        self.assertEqual(compared["counts"]["unobserved_previous"], 1)

    def test_inconsistent_suite_counts_and_duplicate_names_are_uncertain(self):
        for text in (
            harness("ok", count=2),
            harness("ok").replace("test result:", "test tests::works ... ok\ntest result:"),
        ):
            with self.subTest(text=text):
                current = test_report("ok", stdout=text)
                self.assertEqual(
                    compare_diagnostics(current, test_report())["counts"]["resolved"], 0
                )

    def test_missing_relevant_tool_metadata_does_not_compare_equal(self):
        for versions in ({"cargo": "cargo 1.90.0"}, {"unrelated": "known"}):
            with self.subTest(versions=versions):
                context = replace(CONTEXT, tool_versions=versions)
                baseline = parse(cargo(), context=context)
                current = parse(FINISHED, context=context)
                result = compare_diagnostics(current, baseline)
                self.assertEqual(result["counts"]["resolved"], 0)
                self.assertEqual(result["counts"]["unobserved_previous"], 1)
        context = replace(CONTEXT, argv=("cargo", "clippy"))
        result = compare_diagnostics(
            parse(FINISHED, context=context), parse(cargo(), context=context)
        )
        self.assertEqual(result["counts"]["resolved"], 0)

    def test_absolute_manifest_arguments_compare_across_project_roots(self):
        other_root = Path("/scratch/candidate")
        baseline_context = replace(
            CONTEXT, argv=("cargo", "check", "--manifest-path=/work/baseline/Cargo.toml")
        )
        current_context = replace(
            CONTEXT, argv=("cargo", "check", "--manifest-path=/scratch/candidate/Cargo.toml")
        )
        baseline = parse(cargo(), context=baseline_context)
        current = parse(cargo(other_root, line=99), root=other_root, context=current_context)
        self.assertEqual(
            current["context"]["argv"][-1], "--manifest-path=/scratch/candidate/Cargo.toml"
        )
        self.assertEqual(compare_diagnostics(current, baseline)["counts"]["persistent"], 1)

    def test_unmatched_distinct_suite_headers_do_not_identify_a_pass(self):
        other = "     Running tests/other.rs (target/debug/deps/other-cafe)\n"
        baseline = test_report(stderr=other)
        current = test_report("ok", stdout=HEADER + other + harness("ok"), stderr="")
        self.assertEqual(compare_diagnostics(current, baseline)["counts"]["resolved"], 0)

    def test_cargo_test_failure_summary_is_not_a_compiler_finding(self):
        report = test_report(stderr=HEADER + "error: test failed, to rerun pass `--lib`\n")
        self.assertEqual([f["domain"] for f in report["findings"]], ["test"])

    def test_comparison_does_not_mutate_inputs(self):
        baseline = parse(cargo())
        current = parse(FINISHED)
        before = copy.deepcopy((current, baseline))
        compare_diagnostics(current, baseline)
        self.assertEqual((current, baseline), before)


class FinalReviewRegressions(unittest.TestCase):
    def test_malformed_cargo_identity_cannot_authorize_resolution(self):
        baseline = parse(cargo())
        for field, value in (
            ("manifest_path", None),
            ("manifest_path", []),
            ("manifest_path", ""),
            ("target", {}),
            ("name", None),
            ("name", []),
            ("name", ""),
            ("kind", None),
            ("kind", {}),
            ("kind", []),
            ("kind", [1]),
            ("src_path", None),
            ("src_path", []),
            ("src_path", ""),
        ):
            with self.subTest(field=field, value=value):
                record = json.loads(cargo())
                owner = record if field in {"manifest_path", "target"} else record["target"]
                owner[field] = value
                current = parse(json.dumps(record) + "\n" + FINISHED)
                comparison = compare_diagnostics(current, baseline)
                self.assertTrue(current["uncertainty"])
                self.assertEqual(comparison["counts"]["resolved"], 0)
                self.assertEqual(comparison["counts"]["unobserved_previous"], 1)

    def test_oversized_text_numbers_are_uncertainty_and_never_resolution(self):
        huge = "9" * 5000
        samples = ["running " + huge + " tests\n"]
        for token in ("1 passed", "0 failed", "0 ignored", "0 measured", "0 filtered out"):
            samples.append(harness("ok").replace(token, huge + token[1:]))
        for coordinate in (f"{huge}:1", f"1:{huge}", "0:1", "1:0"):
            samples.append(
                "warning[unused_variables]: unused variable: `x`\n"
                f" --> src/lib.rs:{coordinate}\n4 | let x = 1;\n"
            )
        for index, sample in enumerate(samples):
            with self.subTest(case=index):
                report = parse(sample, context=TEST_CONTEXT)
                self.assertTrue(report["uncertainty"])
                self.assertEqual(
                    compare_diagnostics(report, parse(cargo(), context=TEST_CONTEXT))["counts"][
                        "resolved"
                    ],
                    0,
                )
                self.assertTrue(report["execution"]["successful"])

    def test_plain_cargo_test_requires_rustdoc_identity(self):
        context = replace(
            TEST_CONTEXT, tool_versions={"cargo": "cargo 1.90", "rustc": "rustc 1.90"}
        )
        baseline = parse(cargo(), context=context)
        current = parse(FINISHED, context=context)
        result = compare_diagnostics(current, baseline)
        self.assertEqual(result["counts"]["resolved"], 0)
        self.assertEqual(result["counts"]["unobserved_previous"], 1)


class SummaryTests(unittest.TestCase):
    def test_summary_bounds_items_and_text_without_mutating_full_report(self):
        report = parse("\n".join(cargo(message=f"{i}: " + "x" * 10000) for i in range(30)))
        compared = compare_diagnostics(report, None)
        report["comparison"] = compared
        before = copy.deepcopy(report)
        summary = summarize_diagnostics(report, max_items=2)
        self.assertLessEqual(len(summary["findings"]), 2)
        self.assertEqual(summary["omitted_findings"], 28)
        self.assertEqual(summary["counts"]["unclassified"], 30)
        self.assertLess(len(json.dumps(summary)), 12000)
        self.assertEqual(report, before)

    def test_summary_drops_non_scalar_and_oversized_numeric_location_fields(self):
        report = parse(cargo())
        location = report["findings"][0]["locations"][0]
        location["line"] = {"x": "x" * 100000}
        location["column"] = True
        report["findings"][0]["message"] = {"x": "x" * 100000}
        summary = summarize_diagnostics(report)
        self.assertLess(len(json.dumps(summary)), 12000)
        self.assertNotIn("line", summary["findings"][0]["location"])
        self.assertNotIn("column", summary["findings"][0]["location"])
        self.assertNotIn("message", summary["findings"][0])
        location["line"] = 10**1000
        summary = summarize_diagnostics(report)
        self.assertNotIn("line", summary["findings"][0]["location"])

    def test_summary_rejects_negative_limit_and_retains_uncertainty(self):
        report = parse("{broken")
        summary = summarize_diagnostics(report, max_items=0)
        self.assertEqual(summary["findings"], [])
        self.assertTrue(summary["uncertainty"])
        with self.assertRaises(ValueError):
            summarize_diagnostics(report, max_items=-1)


if __name__ == "__main__":
    unittest.main()
