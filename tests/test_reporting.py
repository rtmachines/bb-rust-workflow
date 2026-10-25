"""Human reports distinguish completed approval from historical or partial evidence."""

import unittest

from bb_workflow.reporting import render_report


class ReportingTests(unittest.TestCase):
    def test_partial_report_exposes_scope_logs_and_prior_candidate_without_approval(self):
        state = {
            "status": "budget_exhausted",
            "reason": "Time exhausted",
            "cycles": 1,
            "acceptance": {
                "mode": "criteria",
                "rounds": [
                    {
                        "cycle": 0,
                        "binding": {"candidate_id": "older-candidate"},
                        "mechanical": [
                            {
                                "criterion_id": "works",
                                "verdict": "satisfied",
                                "evidence": "Earlier tests",
                                "binding": {"candidate_id": "older-candidate"},
                            }
                        ],
                        "semantic": [],
                    }
                ],
            },
            "checks": [
                {
                    "phase": "candidate-1",
                    "id": "test",
                    "variant": "default",
                    "command": ["cargo", "test"],
                    "passed": False,
                    "artifacts": "checks/candidate-1/test",
                    "diagnostics_file": "diagnostics.json",
                }
            ],
            "diagnostics": {
                "candidate-1": {"counts": {"persistent": 1}, "file": "comparison.json"}
            },
        }
        plan = {
            "profile": "gate",
            "package": None,
            "test_filter": None,
            "variants": ["default", "feature"],
            "checks": [
                {"id": "test", "profile": "gate", "variant": "default", "argv": ["cargo", "test"]},
                {
                    "id": "feature",
                    "profile": "gate",
                    "variant": "feature",
                    "argv": ["cargo", "test", "--features", "extra"],
                },
            ],
        }
        requirements = {
            "criteria": [
                {
                    "id": "works",
                    "description": "Return correct output",
                    "verification": "Execute tests",
                    "kind": "mechanical",
                }
            ]
        }
        text = render_report(state, plan, requirements)
        for expected in (
            "No final approval",
            "Planned variants: default, feature",
            "Executed variants: default",
            "Return correct output",
            "older-candidate",
            "historical/partial",
            "persistent",
            "comparison.json",
            "checks/candidate-1/test",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("Final approval: recorded", text)

    def test_legacy_report_explicitly_has_no_requirements_guarantee(self):
        text = render_report({"status": "succeeded", "acceptance": {"mode": "legacy"}}, {}, None)
        self.assertIn("criteria absent", text)
        self.assertIn("No requirements guarantee", text)
