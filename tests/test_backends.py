"""Fixture tests exercise CLI contracts without spending model tokens."""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from bb_workflow.backends import BackendError, invoke, probe
from bb_workflow.process import ProcessResult
from bb_workflow.protocol import response_schema


@dataclass(frozen=True)
class Selection:
    backend: str = "claude"
    model: str = "exact-model"
    effort: str | None = "high"
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    mcp_config: str | None = None


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name).resolve()
        self.artifacts = self.cwd / "artifacts"
        self.schema = {"type": "object"}
        self.runner = patch("bb_workflow.backends.run_process")
        self.run = self.runner.start()
        self.addCleanup(self.runner.stop)

    def invoke(self, selection=None, role="reviewer_a"):
        return invoke(
            selection or Selection(),
            role,
            "prompt from stdin",
            self.cwd,
            self.artifacts,
            12,
            self.schema,
            executable="model-fixture",
        )

    def result(self, value, **kwargs):
        self.run.return_value = ProcessResult(0, json.dumps(value), "diagnostic", 0.1, **kwargs)

    def codex_result(self, events=None, payload=None):
        def fake(argv, **kwargs):
            if payload is not None:
                Path(argv[argv.index("-o") + 1]).write_text(json.dumps(payload), encoding="utf-8")
            return ProcessResult(
                0,
                "\n".join(
                    json.dumps(event)
                    for event in (
                        events or [{"type": "turn.completed", "usage": {"input_tokens": 4}}]
                    )
                ),
                "",
                0.1,
            )

        self.run.side_effect = fake

    def test_full_v2_schema_reaches_both_native_adapters_unchanged(self):
        self.schema = response_schema(2)
        self.result({"structured_output": {}})
        self.invoke()
        argv = self.run.call_args.args[0]
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), self.schema)
        self.codex_result(payload={})
        self.invoke(Selection(backend="codex"))
        argv = self.run.call_args.args[0]
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        self.assertEqual(json.loads(schema_path.read_text()), self.schema)
        self.assertIn("binding", self.schema["required"])
        self.assertIn("criterion_assessments", self.schema["required"])

    def test_claude_parses_structured_output_and_records_raw_audit(self):
        value = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": {"findings": []},
            "total_cost_usd": 0.03,
            "usage": {"input_tokens": 20},
            "modelUsage": {"exact-model": {}},
        }
        self.result(value)
        result = self.invoke()
        self.assertEqual(result.payload, {"findings": []})
        self.assertEqual(result.cost_usd, 0.03)
        self.assertEqual(result.usage, {"input_tokens": 20})
        self.assertEqual(result.model, "exact-model")
        self.assertEqual(json.loads((self.artifacts / "stdout.txt").read_text()), value)
        self.assertEqual((self.artifacts / "stderr.txt").read_text(), "diagnostic")
        self.assertEqual(json.loads((self.artifacts / "response.json").read_text()), result.payload)
        command = json.loads((self.artifacts / "command.json").read_text())
        self.assertEqual(command["cwd"], str(self.cwd))
        self.assertEqual(self.run.call_args.kwargs["input_text"], "prompt from stdin")

    def test_raw_audit_preserves_newlines_with_windows_text_translation(self):
        stdout = '{"structured_output": {}}\r\n'
        stderr = "first\r\nsecond\nprogress\r"
        self.run.return_value = ProcessResult(0, stdout, stderr, 0.1)
        original_open = Path.open

        def windows_open(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
            # Use a real TextIOWrapper with Windows defaults even on POSIX.
            if "b" not in mode and newline is None:
                newline = "\r\n"
            return original_open(path, mode, buffering, encoding, errors, newline)

        with patch.object(Path, "open", windows_open):
            result = self.invoke()
        self.assertEqual(result.payload, {})
        self.assertEqual((self.artifacts / "stdout.txt").read_bytes(), stdout.encode("utf-8"))
        self.assertEqual((self.artifacts / "stderr.txt").read_bytes(), stderr.encode("utf-8"))

    def test_claude_role_tool_boundaries_and_explicit_mcp(self):
        self.result({"structured_output": {}})
        for role, expected in (
            ("reviewer_a", "Read,Glob,Grep,LSP"),
            ("reviewer_b", "Read,Glob,Grep,LSP"),
            ("triage", "Read,Glob,Grep,LSP"),
            ("implementation", "Read,Glob,Grep,Edit,Write,LSP"),
            ("fixer", "Read,Glob,Grep,Edit,Write,LSP"),
        ):
            with self.subTest(role=role):
                self.invoke(Selection(tools=("LSP",), mcp_config="mcp.json"), role)
                argv = self.run.call_args.args[0]
                self.assertEqual(argv[argv.index("--tools") + 1], expected)
                self.assertEqual(argv[argv.index("--allowedTools") + 1], expected)
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
                self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
                self.assertEqual(argv[argv.index("--mcp-config") + 1], "mcp.json")
                self.assertIn("--strict-mcp-config", argv)
                self.assertIn("--no-session-persistence", argv)
                self.assertNotIn("Bash", expected)

    def test_claude_omits_optional_effort_and_mcp(self):
        self.result({"structured_output": {}})
        self.invoke(Selection(effort=None))
        argv = self.run.call_args.args[0]
        self.assertNotIn("--effort", argv)
        self.assertNotIn("--mcp-config", argv)

    def test_explicit_mcp_tools_are_authorized_separately_from_builtins(self):
        self.result({"structured_output": {"ok": True}})
        self.invoke(Selection(tools=("mcp__rust__hover",), mcp_config="mcp.json"))
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Glob,Grep,mcp__rust__hover")
        with self.assertRaises(BackendError):
            self.invoke(Selection(tools=("mcp__rust__hover",)))
        with self.assertRaises(BackendError):
            self.invoke(Selection(tools=("mcp__rust__*",), mcp_config="mcp.json"))

    def test_claude_rejects_missing_structured_output_and_error_envelopes(self):
        invalid = [
            [],
            {"result": '{"findings":[]}'},
            {"structured_output": []},
            {"is_error": True, "structured_output": {}},
            {"subtype": "error_max_turns", "structured_output": {}},
            {"structured_output": {}, "model": "other-model"},
            {"structured_output": {}, "modelUsage": {"other-model": {}}},
            {"structured_output": {}, "usage": []},
            {"structured_output": {}, "total_cost_usd": -1},
            {"structured_output": {}, "total_cost_usd": True},
            {"structured_output": {}, "total_cost_usd": 10**400},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.result(value)
                with self.assertRaises(BackendError):
                    self.invoke()
                self.assertIn("error", json.loads((self.artifacts / "response.json").read_text()))

    def test_malformed_claude_json_is_not_scraped_from_prose(self):
        self.run.return_value = ProcessResult(0, 'Here is JSON: {"structured_output":{}}', "", 0.1)
        with self.assertRaisesRegex(BackendError, "Invalid JSON"):
            self.invoke()

    def test_codex_schema_output_usage_and_permission_command(self):
        self.codex_result(payload={"approved": True})
        for role in ("reviewer_a", "reviewer_b", "triage", "implementation", "fixer"):
            with self.subTest(role=role):
                result = self.invoke(Selection(backend="codex"), role)
                self.assertEqual(result.payload, {"approved": True})
                self.assertIsNone(result.cost_usd)
                self.assertEqual(result.usage, {"input_tokens": 4})
                argv = self.run.call_args.args[0]
                expected = "workspace-write" if role in ("implementation", "fixer") else "read-only"
                self.assertEqual(argv[argv.index("--sandbox") + 1], expected)
                self.assertEqual(argv[-1], "-")
                self.assertIn('approval_policy="never"', argv)
                self.assertIn('model_reasoning_effort="high"', argv)
                self.assertIn("--ignore-user-config", argv)
                self.assertIn("--skip-git-repo-check", argv)
                self.assertIn("--ephemeral", argv)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
                self.assertEqual(
                    json.loads((self.artifacts / "schema.json").read_text()),
                    self.schema,
                )

    def test_codex_detects_errors_even_after_success_event(self):
        for failure in ("turn.failed", "error"):
            self.codex_result(events=[{"type": "turn.completed"}, {"type": failure}], payload={})
            with self.subTest(failure=failure), self.assertRaises(BackendError):
                self.invoke(Selection(backend="codex"))

    def test_codex_requires_completed_turn_and_final_object(self):
        for events, payload in (
            ([{"type": "turn.started"}], {}),
            ([{"type": "turn.completed"}], None),
            ([{"type": "turn.completed"}], []),
            ([{"type": "turn.completed", "model": "other"}], {}),
        ):
            with self.subTest(events=events, payload=payload):
                self.codex_result(events=events, payload=payload)
                with self.assertRaises(BackendError):
                    self.invoke(Selection(backend="codex"))

    def test_codex_rejects_malformed_event_and_stale_output(self):
        self.artifacts.mkdir()
        (self.artifacts / "last-message.json").write_text('{"stale":true}')
        self.run.return_value = ProcessResult(0, '{"type":"turn.completed"}\nnot JSON', "", 0.1)
        with self.assertRaisesRegex(BackendError, "Invalid JSON"):
            self.invoke(Selection(backend="codex"))
        self.assertFalse((self.artifacts / "last-message.json").exists())

    def test_timeout_nonzero_and_missing_executable_fail_with_artifacts(self):
        for result in (
            ProcessResult(1, "out", "error", 0.1),
            ProcessResult(-9, "partial", "", 0.1, True),
        ):
            with self.subTest(result=result):
                self.run.return_value = result
                with self.assertRaises(BackendError):
                    self.invoke()
                self.assertEqual((self.artifacts / "stdout.txt").read_text(), result.stdout)
        self.run.side_effect = FileNotFoundError("missing")
        with self.assertRaisesRegex(BackendError, "Cannot start"):
            self.invoke()
        self.assertTrue((self.artifacts / "command.json").exists())

    def test_interrupted_provider_retains_raw_logs_and_measured_or_unknown_audit(self):
        from bb_workflow.process import _OutputCapture, run_process

        cases = (
            (
                "claude",
                '{"usage":{"input_tokens":7},"total_cost_usd":0.02,"model":"exact-model"}',
                {"input_tokens": 7},
                0.02,
            ),
            (
                "codex",
                '{"type":"turn.completed","usage":{"input_tokens":9},"model":"exact-model"}\n{"partial":',
                {"input_tokens": 9},
                None,
            ),
            ("codex", '{"type":"turn.started"}\n{"partial":', {}, None),
        )
        original_drain = _OutputCapture.drain
        for backend, stdout, usage, cost in cases:
            interrupted = False

            def drain(capture):
                nonlocal interrupted
                active = original_drain(capture)
                if (
                    not interrupted
                    and capture.stdout == stdout.encode()
                    and capture.stderr == b"provider diagnostic"
                ):
                    interrupted = True
                    raise KeyboardInterrupt("cancel provider")
                return active

            def provider(argv, **kwargs):
                code = f"import os,time; os.write(1,{stdout.encode()!r}); os.write(2,b'provider diagnostic'); time.sleep(30)"
                return run_process([sys.executable, "-c", code], **kwargs)

            self.run.side_effect = provider
            with (
                self.subTest(backend=backend, usage=usage),
                patch.object(_OutputCapture, "drain", drain),
                self.assertRaises(KeyboardInterrupt),
            ):
                self.invoke(Selection(backend=backend))
            self.assertEqual((self.artifacts / "stdout.txt").read_text(), stdout)
            self.assertEqual((self.artifacts / "stderr.txt").read_text(), "provider diagnostic")
            audit = json.loads((self.artifacts / "invocation.json").read_text())
            self.assertEqual(audit["status"], "interrupted")
            self.assertFalse(audit["retryable"])
            self.assertEqual(audit["usage"], usage)
            self.assertEqual(audit["cost_usd"], cost)
            self.assertFalse((self.artifacts / "response.json").exists())

    def test_invalid_backend_role_tool_and_codex_mcp_fail_before_spawn(self):
        cases = (
            (Selection(backend="invalid"), "reviewer_a"),
            (Selection(), "typo"),
            (Selection(tools=("Bash",)), "reviewer_a"),
            (Selection(backend="codex", mcp_config="mcp.json"), "reviewer_a"),
            (Selection(backend="codex", tools=("LSP",)), "reviewer_a"),
        )
        for selection, role in cases:
            with (
                self.subTest(selection=selection, role=role),
                self.assertRaises(BackendError) as raised,
            ):
                self.invoke(selection, role)
            self.assertFalse(raised.exception.retryable)
        self.run.assert_not_called()

    def test_probe_only_uses_version_and_help_and_reports_missing_flags(self):
        self.run.side_effect = [
            ProcessResult(0, "fixture-cli 1.0\n", "", 0.1),
            ProcessResult(0, "--model --sandbox", "", 0.1),
        ]
        report = probe("fixture", "codex")
        self.assertTrue(report["available"])
        self.assertFalse(report["supported"])
        self.assertEqual(report["version"], "fixture-cli 1.0")
        self.assertIn("--json", report["missing_flags"])
        self.assertTrue(report["required_flags"]["--model"])
        self.assertEqual(
            [call.args[0] for call in self.run.call_args_list],
            [["fixture", "--version"], ["fixture", "exec", "--help"]],
        )

    def test_probe_missing_cli_and_timeout_do_not_start_a_session(self):
        self.run.side_effect = FileNotFoundError("missing")
        self.assertFalse(probe("fixture", "claude")["available"])
        self.run.side_effect = None
        self.run.return_value = ProcessResult(-9, "", "", 10, True)
        report = probe("fixture", "claude")
        self.assertFalse(report["available"])
        self.assertIsNotNone(report["error"])

    def test_versioned_provider_contract_fixtures(self):
        for path in sorted((Path(__file__).parent / "fixtures" / "backends").glob("*.json")):
            contract = json.loads(path.read_text(encoding="utf-8"))
            selection = Selection(backend=contract["provider"], model=contract["requested_model"])
            for case in contract["cases"]:
                with self.subTest(provider=path.name, case=case["name"]):

                    def fake(argv, **kwargs):
                        if "payload" in case or "final_text" in case:
                            final = case.get("final_text", json.dumps(case.get("payload")))
                            (self.artifacts / "last-message.json").write_text(
                                final, encoding="utf-8"
                            )
                        stdout = case.get("stdout")
                        if stdout is None:
                            stdout = (
                                json.dumps(case["response"])
                                if "response" in case
                                else "\n".join(json.dumps(event) for event in case["events"])
                            )
                        return ProcessResult(0, stdout, "", 0.1)

                    self.run.side_effect = fake
                    if "error" in case:
                        with self.assertRaisesRegex(BackendError, case["error"]) as raised:
                            self.invoke(selection)
                        self.assertEqual(raised.exception.requested_model, selection.model)
                        self.assertEqual(
                            raised.exception.observed_model, case.get("observed_model")
                        )
                        diagnostic = json.loads((self.artifacts / "response.json").read_text())
                        self.assertEqual(diagnostic["requested_model"], selection.model)
                        self.assertEqual(diagnostic["observed_model"], case.get("observed_model"))
                    else:
                        result = self.invoke(selection)
                        expected = case.get(
                            "payload", case.get("response", {}).get("structured_output")
                        )
                        self.assertEqual(result.payload, expected)
                    audit = json.loads((self.artifacts / "invocation.json").read_text())
                    self.assertEqual(audit["requested_model"], selection.model)
                    if "observed_model" in case:
                        self.assertIn(case["observed_model"], audit["observed_models"])
                    if case["name"] == "reroute":
                        self.assertEqual(audit["reroutes"][0]["reason"], "HighRisk")

    def test_model_evidence_is_preserved_on_nonzero_exit(self):
        self.run.return_value = ProcessResult(
            1, json.dumps({"model": "other-model", "structured_output": {}}), "failed", 0.1
        )
        with self.assertRaises(BackendError) as raised:
            self.invoke()
        self.assertEqual(raised.exception.observed_model, "other-model")
        audit = json.loads((self.artifacts / "invocation.json").read_text())
        self.assertEqual(audit["observed_models"], ["other-model"])
        self.assertFalse(raised.exception.retryable)

    def test_failed_claude_retains_reported_usage_and_cost(self):
        usage = {"input_tokens": 10, "output_tokens": 4}
        for cost in (0, 0.25):
            for failure in ("nonzero", "error_envelope", "missing_output", "timeout"):
                with self.subTest(cost=cost, failure=failure):
                    envelope = {"usage": usage, "total_cost_usd": cost}
                    if failure == "error_envelope":
                        envelope.update(is_error=True, subtype="error_max_turns")
                    self.run.return_value = ProcessResult(
                        1 if failure == "nonzero" else 0,
                        json.dumps(envelope),
                        "",
                        0.1,
                        timed_out=failure == "timeout",
                    )
                    with self.assertRaises(BackendError) as raised:
                        self.invoke()
                    self.assertEqual(raised.exception.usage, usage)
                    self.assertEqual(raised.exception.cost_usd, cost)
                    self.assertTrue(raised.exception.retryable)
                    audit = json.loads((self.artifacts / "invocation.json").read_text())
                    self.assertEqual(audit["usage"], usage)
                    self.assertEqual(audit["cost_usd"], cost)
                    self.assertTrue(audit["retryable"])

    def test_failed_codex_retains_completed_turn_measurements_only(self):
        usage = {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 4}
        for failure in ("missing_output", "error", "malformed"):
            with self.subTest(failure=failure):
                stdout = json.dumps({"type": "turn.completed", "usage": usage})
                if failure == "error":
                    stdout += '\n{"type":"error","message":"lost connection"}'
                elif failure == "malformed":
                    stdout += "\npartial event"
                self.run.return_value = ProcessResult(0, stdout, "", 0.1)
                with self.assertRaises(BackendError) as raised:
                    self.invoke(Selection(backend="codex"))
                self.assertEqual(raised.exception.usage, usage)
                self.assertIsNone(raised.exception.cost_usd)
                audit = json.loads((self.artifacts / "invocation.json").read_text())
                self.assertEqual(audit["usage"], usage)
        self.run.return_value = ProcessResult(
            1, json.dumps({"type": "item.completed", "usage": usage}), "", 0.1
        )
        with self.assertRaises(BackendError) as raised:
            self.invoke(Selection(backend="codex"))
        self.assertEqual(raised.exception.usage, {})

    def test_failure_does_not_fabricate_or_retain_stale_measurements(self):
        self.result({"structured_output": {}, "total_cost_usd": 0.25, "usage": {"input_tokens": 8}})
        self.invoke()
        invalid = [None, -1, True, "0.2", float("inf"), float("nan"), 10**400]
        for cost in invalid:
            with self.subTest(cost=cost):
                self.result({"is_error": True, "usage": [], "total_cost_usd": cost})
                with self.assertRaises(BackendError) as raised:
                    self.invoke()
                self.assertEqual(raised.exception.usage, {})
                self.assertIsNone(raised.exception.cost_usd)
                audit = json.loads((self.artifacts / "invocation.json").read_text())
                self.assertEqual(audit["usage"], {})
                self.assertIsNone(audit["cost_usd"])
        self.run.return_value = ProcessResult(0, '{"usage":{"input_tokens":1}', "", 0.1)
        with self.assertRaises(BackendError) as raised:
            self.invoke()
        self.assertEqual(raised.exception.usage, {})

    def test_model_drift_is_not_retryable_even_after_process_failure(self):
        reroute = {
            "type": "item.completed",
            "item": {"type": "error", "message": "model rerouted: exact-model -> other-model"},
        }
        for backend, envelope in (
            ("claude", {"structured_output": {}, "model": "other-model"}),
            ("codex", reroute),
        ):
            for timed_out, returncode in ((False, 0), (False, 1), (True, -9)):
                with self.subTest(backend=backend, timeout=timed_out, returncode=returncode):
                    self.run.return_value = ProcessResult(
                        returncode, json.dumps(envelope), "", 0.1, timed_out=timed_out
                    )
                    with self.assertRaises(BackendError) as raised:
                        self.invoke(Selection(backend=backend))
                    self.assertFalse(raised.exception.retryable)
                    self.assertEqual(raised.exception.observed_model, "other-model")

    def test_cannot_start_is_not_retryable_and_unknown_errors_remain_retryable(self):
        self.run.side_effect = FileNotFoundError("missing")
        with self.assertRaises(BackendError) as raised:
            self.invoke()
        self.assertFalse(raised.exception.retryable)
        self.assertTrue(BackendError("temporary fixture failure").retryable)

    def test_output_overflow_rejects_even_valid_success_before_parsing(self):
        self.run.return_value = ProcessResult(
            0, '{"structured_output":{}}', "", 0.1, output_limit_exceeded=True
        )
        with patch("bb_workflow.backends._parse_claude") as parse:
            with self.assertRaisesRegex(BackendError, "output byte limit") as raised:
                self.invoke()
            parse.assert_not_called()
        self.assertFalse(raised.exception.retryable)
        self.assertEqual((self.artifacts / "stdout.txt").read_text(), '{"structured_output":{}}')
        self.assertEqual(self.run.call_args.kwargs["max_output_bytes"], 16 * 1024 * 1024)

    def test_codex_final_file_shares_the_output_budget(self):
        self.codex_result(payload={"large": "x" * 512})
        with self.assertRaisesRegex(BackendError, "final response exceeded") as raised:
            invoke(
                Selection(backend="codex"),
                "reviewer_a",
                "prompt",
                self.cwd,
                self.artifacts,
                12,
                self.schema,
                max_output_bytes=128,
            )
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(self.run.call_args.kwargs["max_output_bytes"], 128)

    def test_invocation_removes_inherited_git_redirection(self):
        self.result({"structured_output": {}})
        with patch.dict(os.environ, {"GIT_DIR": "/outside/.git", "GIT_WORK_TREE": "/outside"}):
            self.invoke()
        environment = self.run.call_args.kwargs["env"]
        self.assertNotIn("GIT_DIR", environment)
        self.assertNotIn("GIT_WORK_TREE", environment)

    def test_probe_uses_target_directory_for_posix_and_windows_relative_executables(self):
        for executable in ("./scripts/codex", r".\scripts\codex.cmd"):
            with self.subTest(executable=executable):
                self.run.reset_mock()
                self.run.side_effect = [
                    ProcessResult(0, "fixture 1", "", 0),
                    ProcessResult(0, "--model", "", 0),
                ]
                with patch.dict(os.environ, {"GIT_DIR": "/outside/.git"}):
                    probe(executable, "codex", cwd=self.cwd, max_output_bytes=1024)
                for call in self.run.call_args_list:
                    self.assertEqual(call.args[0][0], executable)
                    self.assertEqual(call.kwargs["cwd"], self.cwd.resolve())
                    self.assertEqual(call.kwargs["max_output_bytes"], 1024)
                    self.assertNotIn("GIT_DIR", call.kwargs["env"])

    def test_probe_rejects_overflow_in_version_or_help(self):
        for overflow_in_version in (True, False):
            with self.subTest(version=overflow_in_version):
                overflow = ProcessResult(0, "--model", "", 0, output_limit_exceeded=True)
                self.run.side_effect = (
                    [overflow]
                    if overflow_in_version
                    else [ProcessResult(0, "fixture", "", 0), overflow]
                )
                report = probe("fixture", "codex")
                self.assertFalse(report["supported"])
                self.assertIn("output byte limit", report["error"])


if __name__ == "__main__":
    unittest.main()
