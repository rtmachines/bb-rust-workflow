"""Usage accounting must not double count caches or hide unmeasured calls."""

import json
import unittest
from pathlib import Path

from bb_workflow.usage import normalize_usage, summarize_usage


class UsageTests(unittest.TestCase):
    def test_claude_input_includes_uncached_cache_reads_and_writes_once(self):
        usage = {
            "input_tokens": 50,
            "cache_read_input_tokens": 100000,
            "cache_creation_input_tokens": 1200,
            "output_tokens": 250,
        }
        self.assertEqual(
            normalize_usage("claude", usage),
            {
                "input_tokens": 101250,
                "cached_input_tokens": 100000,
                "cache_creation_input_tokens": 1200,
                "output_tokens": 250,
                "total_tokens": 101500,
            },
        )
        self.assertEqual(usage["input_tokens"], 50)

    def test_codex_cache_and_reasoning_are_not_added_again_to_total(self):
        normalized = normalize_usage(
            "codex",
            {
                "input_tokens": 1000,
                "cached_input_tokens": 800,
                "cache_write_input_tokens": 100,
                "output_tokens": 60,
                "reasoning_output_tokens": 40,
            },
        )
        self.assertEqual(normalized["input_tokens"], 1000)
        self.assertEqual(normalized["total_tokens"], 1060)
        self.assertEqual(normalized["cached_input_tokens"], 800)
        self.assertEqual(normalized["cache_creation_input_tokens"], 100)

    def test_missing_measurements_stay_unknown_and_explicit_zero_stays_zero(self):
        for backend in ("claude", "codex", "future"):
            with self.subTest(backend=backend):
                self.assertTrue(all(v is None for v in normalize_usage(backend, {}).values()))
        claude = normalize_usage("claude", {"input_tokens": 20, "output_tokens": 0})
        self.assertIsNone(claude["input_tokens"])
        self.assertIsNone(claude["total_tokens"])
        self.assertEqual(claude["output_tokens"], 0)
        codex = normalize_usage("codex", {"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(codex["total_tokens"], 0)
        self.assertIsNone(codex["cached_input_tokens"])
        self.assertIsNone(codex["cache_creation_input_tokens"])

    def test_invalid_counters_do_not_become_measured_tokens(self):
        for invalid in (True, False, -1, 1.5, "5", [], {}, float("nan"), float("inf")):
            with self.subTest(value=invalid):
                normalized = normalize_usage(
                    "codex", {"input_tokens": invalid, "cached_input_tokens": invalid}
                )
                self.assertIsNone(normalized["input_tokens"])
                self.assertIsNone(normalized["cached_input_tokens"])
                self.assertIsNone(normalized["total_tokens"])

    def test_versioned_backend_fixtures_keep_unknown_totals_unknown(self):
        fixtures = Path(__file__).parent / "fixtures" / "backends"
        claude = json.loads((fixtures / "claude-2.1.308.json").read_text(encoding="utf-8"))
        usage = claude["cases"][0]["response"]["usage"]
        self.assertIsNone(normalize_usage("claude", usage)["total_tokens"])
        codex = json.loads((fixtures / "codex-0.164.0.json").read_text(encoding="utf-8"))
        usage = codex["cases"][0]["events"][-1]["usage"]
        self.assertEqual(normalize_usage("codex", usage)["total_tokens"], 14)

    def test_summary_counts_failed_and_partial_calls_without_hiding_unknowns(self):
        agents = [
            {
                "selection": {"backend": "codex"},
                "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10},
            },
            {"selection": {"backend": "codex"}, "usage": {"input_tokens": 20}},
            {"selection": {"backend": "codex"}, "status": "failed"},
            {
                "selection": {"backend": "claude"},
                "token_usage": normalize_usage(
                    "claude",
                    {
                        "input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 0,
                    },
                ),
            },
        ]
        summary = summarize_usage(agents)["by_backend"]
        codex = summary["codex"]
        self.assertEqual(codex["calls"], 3)
        self.assertEqual(codex["complete_calls"], 1)
        self.assertEqual(codex["partial_calls"], 1)
        self.assertEqual(codex["unknown_calls"], 1)
        self.assertEqual(codex["known_totals"]["total_tokens"], 110)
        self.assertEqual(codex["known_totals"]["input_tokens"], 120)
        self.assertEqual(codex["missing_calls"]["total_tokens"], 2)
        self.assertEqual(codex["missing_calls"]["input_tokens"], 1)
        self.assertIsNone(codex["known_totals"]["cache_creation_input_tokens"])
        self.assertEqual(summary["claude"]["known_totals"]["total_tokens"], 0)
        self.assertEqual(summary["claude"]["complete_calls"], 1)

    def test_empty_or_unmeasured_summary_never_claims_zero_spend(self):
        self.assertEqual(summarize_usage([]), {"by_backend": {}})
        summary = summarize_usage([{}])["by_backend"]["unknown"]
        self.assertEqual(summary["unknown_calls"], 1)
        self.assertTrue(all(v is None for v in summary["known_totals"].values()))
        self.assertTrue(all(v == 1 for v in summary["missing_calls"].values()))


if __name__ == "__main__":
    unittest.main()
