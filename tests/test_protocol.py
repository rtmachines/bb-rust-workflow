"""Provider-independent validation and conservative triage semantics."""

import copy
import tempfile
import unittest
from pathlib import Path

from bb_workflow.protocol import ProtocolError, merge_findings, resolve_triage, validate_response


def finding(identifier="A1", **changes):
    return {
        "id": identifier,
        "path": "src/lib.rs",
        "line": 1,
        "severity": "high",
        "title": "Incorrect result",
        "detail": "Concrete defect",
        "evidence": "Source demonstrates the defect",
        **changes,
    }


def response(**changes):
    return {
        "status": "completed",
        "summary": "Review complete",
        "findings": [],
        "decisions": [],
        **changes,
    }


def decision(identifier, disposition, duplicate_of=""):
    return {
        "finding_id": identifier,
        "disposition": disposition,
        "rationale": "Checked source evidence",
        "duplicate_of": duplicate_of,
    }


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        (self.work / "src").mkdir()
        (self.work / "src/lib.rs").write_text("first\nsecond\n", encoding="utf-8")

    def test_valid_review_references_real_source(self):
        payload = response(findings=[finding(line=2)])
        self.assertEqual(validate_response(payload, "reviewer_a", self.work), payload)

    def test_deleted_file_can_be_anchored_in_baseline_only(self):
        baseline = self.work / "baseline"
        baseline.mkdir()
        (baseline / "deleted.rs").write_text("old first line\n", encoding="utf-8")
        payload = response(findings=[finding(path="deleted.rs")])
        self.assertEqual(validate_response(payload, "reviewer_a", self.work, baseline), payload)
        with self.assertRaises(ProtocolError):
            validate_response(
                response(findings=[finding(path="deleted.rs", line=2)]),
                "reviewer_a",
                self.work,
                baseline,
            )

    def test_schema_rejects_missing_extra_blank_and_wrong_type_fields(self):
        missing = response()
        missing.pop("summary")
        invalid = [
            missing,
            response(extra=True),
            response(summary=" \n"),
            response(status="success"),
            response(findings={}),
            response(decisions="none"),
            response(summary=12),
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                validate_response(payload, "reviewer_a", self.work)

    def test_finding_rejects_wrong_types_lines_and_unknown_severity(self):
        invalid = [
            finding(line=True),
            finding(line=0),
            finding(line=3),
            finding(line="1"),
            finding(severity="informational"),
            finding(evidence=""),
            finding(id=" "),
        ]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ProtocolError):
                validate_response(response(findings=[item]), "reviewer_a", self.work)

    def test_finding_rejects_traversal_absolute_windows_and_missing_paths(self):
        paths = [
            "../outside.rs",
            "/etc/passwd",
            "src/../../outside.rs",
            "src\\lib.rs",
            "C:/source/lib.rs",
            "src/missing.rs",
            "src",
        ]
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ProtocolError):
                validate_response(response(findings=[finding(path=path)]), "reviewer_a", self.work)

    def test_empty_file_can_reference_first_line_only(self):
        (self.work / "empty.rs").touch()
        validate_response(response(findings=[finding(path="empty.rs")]), "reviewer_a", self.work)
        with self.assertRaises(ProtocolError):
            validate_response(
                response(findings=[finding(path="empty.rs", line=2)]), "reviewer_a", self.work
            )

    def test_duplicate_ids_within_one_review_are_invalid(self):
        with self.assertRaisesRegex(ProtocolError, "duplicate finding ID"):
            validate_response(
                response(findings=[finding(), finding(title="Different")]), "reviewer_a", self.work
            )

    def test_role_specific_response_fields_are_enforced(self):
        for role in ("implementation", "fixer", "triage"):
            with self.subTest(role=role), self.assertRaises(ProtocolError):
                validate_response(response(findings=[finding()]), role, self.work)
        for role in ("implementation", "fixer", "reviewer_a", "reviewer_b"):
            with self.subTest(role=role), self.assertRaises(ProtocolError):
                validate_response(
                    response(decisions=[decision("F1", "confirmed")]), role, self.work
                )
        validate_response(response(decisions=[decision("F1", "confirmed")]), "triage", self.work)

    def test_merge_exact_duplicates_preserves_both_sources_and_does_not_mutate_reviews(self):
        reviews = [response(findings=[finding("a")]), response(findings=[finding("b")])]
        original = copy.deepcopy(reviews)
        merged = merge_findings(reviews)
        self.assertEqual(reviews, original)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "F1")
        self.assertEqual(merged[0]["sources"], ["reviewer_a:a", "reviewer_b:b"])

    def test_merge_preserves_nonidentical_evidence_or_severity(self):
        reviews = [
            response(findings=[finding("a")]),
            response(
                findings=[finding("a", evidence="Different evidence"), finding("b", severity="low")]
            ),
        ]
        merged = merge_findings(reviews)
        self.assertEqual([item["id"] for item in merged], ["F1", "F2", "F3"])
        self.assertEqual([item["severity"] for item in merged], ["high", "high", "low"])

    def test_triage_splits_confirmed_rejected_and_unresolved(self):
        findings = [finding("F1"), finding("F2"), finding("F3")]
        triage = response(
            decisions=[
                decision("F1", "confirmed"),
                decision("F2", "rejected"),
                decision("F3", "unresolved"),
            ]
        )
        confirmed, unresolved = resolve_triage(findings, triage)
        self.assertEqual([item["id"] for item in confirmed], ["F1"])
        self.assertEqual([item["id"] for item in unresolved], ["F3"])

    def test_triage_requires_exactly_one_decision_per_finding(self):
        findings = [finding("F1"), finding("F2")]
        invalid = [
            [],
            [decision("F1", "confirmed")],
            [decision("F1", "confirmed"), decision("F1", "rejected")],
            [decision("F1", "confirmed"), decision("F3", "confirmed")],
        ]
        for decisions in invalid:
            with self.subTest(decisions=decisions), self.assertRaises(ProtocolError):
                resolve_triage(findings, response(decisions=decisions))

    def test_duplicate_must_reference_a_directly_confirmed_finding(self):
        findings = [finding("F1"), finding("F2"), finding("F3")]
        valid = response(
            decisions=[
                decision("F1", "confirmed"),
                decision("F2", "duplicate", "F1"),
                decision("F3", "rejected"),
            ]
        )
        confirmed, unresolved = resolve_triage(findings, valid)
        self.assertEqual([item["id"] for item in confirmed], ["F1"])
        self.assertFalse(unresolved)
        for target in ("F2", "F3", "missing", ""):
            with self.subTest(target=target), self.assertRaises(ProtocolError):
                resolve_triage(
                    findings,
                    response(
                        decisions=[
                            decision("F1", "confirmed"),
                            decision("F2", "duplicate", target),
                            decision("F3", "rejected"),
                        ]
                    ),
                )

    def test_duplicate_cycles_and_chains_are_invalid(self):
        findings = [finding("F1"), finding("F2"), finding("F3")]
        for decisions in (
            [
                decision("F1", "duplicate", "F2"),
                decision("F2", "duplicate", "F1"),
                decision("F3", "confirmed"),
            ],
            [
                decision("F1", "confirmed"),
                decision("F2", "duplicate", "F1"),
                decision("F3", "duplicate", "F2"),
            ],
        ):
            with self.subTest(decisions=decisions), self.assertRaises(ProtocolError):
                resolve_triage(findings, response(decisions=decisions))

    def test_nonduplicate_disposition_must_leave_duplicate_target_empty(self):
        for disposition in ("confirmed", "rejected", "unresolved"):
            with self.subTest(disposition=disposition), self.assertRaises(ProtocolError):
                resolve_triage(
                    [finding("F1")], response(decisions=[decision("F1", disposition, "F1")])
                )

    def test_duplicate_group_preserves_highest_severity_without_mutating_findings(self):
        findings = [
            finding("F1", severity="low"),
            finding("F2", severity="critical"),
            finding("F3", severity="medium"),
        ]
        original = copy.deepcopy(findings)
        triage = response(
            decisions=[
                decision("F1", "confirmed"),
                decision("F2", "duplicate", "F1"),
                decision("F3", "duplicate", "F1"),
            ]
        )
        confirmed, unresolved = resolve_triage(findings, triage)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["id"], "F1")
        self.assertEqual(confirmed[0]["severity"], "critical")
        self.assertEqual(findings, original)
        self.assertFalse(unresolved)

    def test_duplicate_groups_retain_distinct_evidence_and_provenance_for_repair(self):
        findings = [
            finding("F1", severity="low", sources=["reviewer_a:a"]),
            finding(
                "F2",
                severity="critical",
                line=2,
                evidence="The second path loses data",
                sources=["reviewer_b:b"],
            ),
            finding("F3", evidence="Separate confirmed defect", sources=["reviewer_b:c"]),
            finding("F4", evidence="Rejected concern", sources=["reviewer_a:d"]),
        ]
        original = copy.deepcopy(findings)
        triage = response(
            decisions=[
                decision("F2", "duplicate", "F1"),
                decision("F1", "confirmed"),
                decision("F3", "confirmed"),
                decision("F4", "rejected"),
            ]
        )
        confirmed, unresolved = resolve_triage(findings, triage)
        self.assertEqual(confirmed[0]["severity"], "critical")
        self.assertEqual(confirmed[0]["evidence"], findings[0]["evidence"])
        self.assertEqual(confirmed[0]["sources"], ["reviewer_a:a"])
        self.assertEqual(confirmed[0]["duplicates"], [findings[1]])
        self.assertEqual(confirmed[1], findings[2])
        self.assertNotIn("duplicates", confirmed[1])
        self.assertFalse(unresolved)
        self.assertEqual(findings, original)
        confirmed[0]["sources"].append("later annotation")
        confirmed[0]["duplicates"][0]["sources"].append("later annotation")
        confirmed[0]["duplicates"][0]["evidence"] = "Updated copy"
        self.assertEqual(findings, original)


if __name__ == "__main__":
    unittest.main()
