"""Pure diagnostic evidence; no parser result grants execution or apply authority.

Cargo JSON embeds rustc diagnostics, while stable libtest output is text. Streams
are consumed in full and separately: a Cargo build result never proves tests ran.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiagnosticContext:
    profile: str
    variant: str
    argv: tuple[str, ...]
    scope: dict
    tool_versions: dict[str, str | None]
    configuration_digest: str | None


_CLASSES = ("new", "persistent", "resolved", "unclassified", "unobserved_previous")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DIAGNOSTIC = re.compile(r"^(warning|error)(?:\[([^]]+)\])?: (.+)$")
_LOCATION = re.compile(r"^\s*-->\s+(.+):(\d+):(\d+)\s*$")
_SOURCE = re.compile(r"^\s*\d+\s*\| ?(.*)$")
_TEST = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)(?:,.*)?$")
_RUNNING = re.compile(r"^running (\d+) tests?$")
_RESULT = re.compile(
    r"^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; "
    r"(\d+) measured; (\d+) filtered out;(?: finished in .+)?$"
)
_HEADER = re.compile(r"^\s*Running\s+(unittests\s+)?(.+?)\s+\(([^)]+)\)\s*$")
_DOC_HEADER = re.compile(r"^\s*Doc-tests\s+(\S+)\s*$")


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _local_text(text: str, root: Path) -> str:
    # Boundaries avoid rewriting /project-other or source code identifiers.
    return re.sub(r"(?<![\w./-])" + re.escape(str(root)) + r'(?=/|[\s"\'`):,]|$)', ".", text)


def _path(value: str, root: Path) -> str:
    normalized = Path(os.path.normpath(root / value))
    try:
        return normalized.relative_to(root).as_posix()
    except ValueError:
        return str(normalized)


def _command(argv: tuple[str, ...]) -> str | None:
    if not argv:
        return None
    executable = Path(argv[0]).name
    offset = 2 if len(argv) > 1 and argv[1].startswith("+") else 1
    if executable == "cargo" and len(argv) > offset:
        return argv[offset] if argv[offset] in {"check", "clippy", "build", "test"} else None
    return "rustc" if executable == "rustc" else None


def _execution(value: dict) -> dict:
    reasons = []
    if value.get("source_unchanged") is not True:
        reasons.append("source_not_verified_unchanged")
    for key in ("timed_out", "output_limit_exceeded"):
        if value.get(key) is not False:
            reasons.append(key + "_or_unknown")
    if value.get("interrupted") or value.get("error") == "Interrupted by user":
        reasons.append("interrupted")
    returncode = value.get("returncode")
    completed = type(returncode) is int and returncode >= 0 and not reasons
    if not completed:
        reasons.append("execution_not_completed")
    successful = completed and returncode == 0 and value.get("passed") is True
    if value.get("error") and returncode == 0:
        reasons.append("execution_error")
        successful = False
    return {
        "valid": not reasons,
        "completed": completed,
        "successful": successful,
        "reasons": reasons,
    }


def _finding(
    domain: str,
    severity: str,
    code: str | None,
    message: str,
    locations: list[dict],
    *,
    target: dict | None = None,
    suite: str | None = None,
    test_name: str | None = None,
) -> dict:
    # Line and byte positions are presentation only. All source anchors matter.
    identity = {
        "domain": domain,
        "severity": severity,
        "code": code,
        "message": message,
        "anchors": sorted(
            {(x["path"], x.get("source", ""), x.get("label")) for x in locations}, key=repr
        ),
        "target": target,
        "suite": suite,
        "test_name": test_name,
    }
    return {
        "fingerprint": _hash(identity),
        "domain": domain,
        "severity": severity,
        "code": code,
        "message": message,
        "locations": locations,
        "target": target,
        "suite": suite,
        "test_name": test_name,
        "occurrences": 1,
    }


def _rustc(message: object, root: Path, target: dict | None) -> dict | None:
    if not isinstance(message, dict):
        raise ValueError("invalid rustc diagnostic")
    if not isinstance(message.get("message"), str) or not isinstance(message.get("level"), str):
        raise ValueError("invalid rustc message or severity")
    spans = message.get("spans")
    if not isinstance(spans, list) or any(not isinstance(x, dict) for x in spans):
        raise ValueError("invalid rustc spans")
    if "code" not in message:
        raise ValueError("missing rustc code field")
    code = message["code"]
    if code is not None and (not isinstance(code, dict) or not isinstance(code.get("code"), str)):
        raise ValueError("invalid rustc code")
    if message["level"] not in {
        "warning",
        "error",
        "error: internal compiler error",
        "note",
        "help",
        "failure-note",
    }:
        raise ValueError("unknown rustc severity")
    locations = []
    for span in spans:
        if not isinstance(span.get("file_name"), str):
            raise ValueError("invalid rustc span path")
        if type(span.get("is_primary")) is not bool:
            raise ValueError("invalid rustc primary flag")
        if span.get("label") is not None and not isinstance(span["label"], str):
            raise ValueError("invalid rustc span label")
        for coordinate in (
            "byte_start",
            "byte_end",
            "line_start",
            "line_end",
            "column_start",
            "column_end",
        ):
            value = span.get(coordinate)
            minimum = 0 if coordinate.startswith("byte_") else 1
            if type(value) is not int or value < minimum:
                raise ValueError("invalid rustc span coordinate")
        if (
            span["byte_end"] < span["byte_start"]
            or span["line_end"] < span["line_start"]
            or (
                span["line_end"] == span["line_start"] and span["column_end"] < span["column_start"]
            )
        ):
            raise ValueError("reversed rustc span coordinates")
        if not span.get("is_primary"):
            continue
        text = span.get("text", [])
        if not isinstance(text, list) or any(
            not isinstance(x, dict) or not isinstance(x.get("text"), str) for x in text
        ):
            raise ValueError("invalid rustc source anchor")
        locations.append(
            {
                "path": _path(span["file_name"], root),
                "line": span.get("line_start"),
                "column": span.get("column_start"),
                "end_line": span.get("line_end"),
                "end_column": span.get("column_end"),
                "source": "\n".join(_local_text(x["text"], root) for x in text),
                "label": _local_text(span["label"], root) if span.get("label") else None,
            }
        )
    if message["level"] not in {"warning", "error", "error: internal compiler error"}:
        return None
    if _is_summary(message["message"]):
        return None
    return _finding(
        "compiler",
        message["level"],
        code["code"] if code else None,
        _local_text(message["message"], root),
        locations,
        target=target,
    )


def _is_summary(message: str) -> bool:
    return bool(
        re.match(
            r"(?:aborting due to |could not compile |.* generated \d+ warnings?\b|"
            r"\d+ warnings? emitted\b|build failed\b|(?:test|doctest) failed, to rerun)",
            message,
        )
    )


def _suite(identity: str | None, expected: int | None = None) -> dict:
    return {
        "identity": identity,
        "expected": expected,
        "passed": [],
        "failed": [],
        "ignored": [],
        "complete": False,
        "filtered": None,
        "uncertainty": [],
    }


def _parse_stream(stream: str, root: Path, state: dict) -> tuple[list[dict], list[str]]:
    suites = []
    headers = []
    pending = None
    suite = None
    failure_detail = False

    def flush():
        nonlocal pending
        if pending is not None:
            state["findings"].append(pending)
            pending = None

    for raw in stream.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("{"):
            flush()
            try:
                record = json.loads(line)
                reason = record.get("reason")
                if reason == "compiler-message":
                    target = record.get("target")
                    if not isinstance(target, dict):
                        raise ValueError("invalid cargo target")
                    target_identity = {
                        "manifest": _path(record["manifest_path"], root)
                        if isinstance(record.get("manifest_path"), str)
                        else None,
                        "name": target.get("name"),
                        "kind": target.get("kind"),
                        "source": _path(target["src_path"], root)
                        if isinstance(target.get("src_path"), str)
                        else None,
                    }
                    finding = _rustc(record.get("message"), root, target_identity)
                    state["compiler_seen"] = True
                    if finding:
                        state["findings"].append(finding)
                elif record.get("$message_type") == "diagnostic":
                    finding = _rustc(record, root, None)
                    state["compiler_seen"] = True
                    if finding:
                        state["findings"].append(finding)
                elif reason == "build-finished":
                    if type(record.get("success")) is not bool:
                        raise ValueError("invalid build result")
                    state["build_results"].append(record["success"])
                elif reason not in {"compiler-artifact", "build-script-executed"}:
                    state["unknown"] += 1
            except (ValueError, TypeError, AttributeError):
                state["malformed"] += 1
            continue
        header = _HEADER.match(line)
        doc_header = _DOC_HEADER.match(line)
        if header or doc_header:
            flush()
            if header:
                path = header[2]
                identity = ("unittests:" if header[1] else "integration:") + _path(path, root)
                headers.append(identity if path.endswith(".rs") else "")
            else:
                headers.append("doctests:" + doc_header[1])
            continue
        running = _RUNNING.match(line)
        if running:
            flush()
            suite = _suite(headers[-1] if headers else None, int(running[1]))
            suites.append(suite)
            failure_detail = False
            continue
        test = _TEST.match(line)
        if test:
            flush()
            if suite is None:
                suite = _suite(headers[-1] if headers else None)
                suites.append(suite)
            name, outcome = test.groups()
            suite[{"ok": "passed", "FAILED": "failed", "ignored": "ignored"}[outcome]].append(name)
            continue
        result = _RESULT.match(line)
        if result:
            flush()
            if suite is None:
                suite = _suite(headers[-1] if headers else None)
                suites.append(suite)
            outcome, passed, failed, ignored, measured, filtered = result.groups()
            counts = [int(passed), int(failed), int(ignored), int(measured)]
            names = suite["passed"] + suite["failed"] + suite["ignored"]
            consistent = (
                counts[:3] == [len(suite[k]) for k in ("passed", "failed", "ignored")]
                and sum(counts) == suite["expected"]
                and counts[3] == 0
                and len(names) == len(set(names))
                and ((outcome == "ok") == (counts[1] == 0))
            )
            suite["filtered"] = int(filtered)
            suite["complete"] = consistent and int(filtered) == 0 and bool(names)
            if not consistent:
                suite["uncertainty"].append("inconsistent_suite_results")
            if int(filtered):
                suite["uncertainty"].append("filtered_tests")
            suite = None
            failure_detail = False
            continue
        diag = _DIAGNOSTIC.match(line)
        if diag:
            flush()
            state["compiler_seen"] = True
            if not _is_summary(diag[3]):
                pending = _finding("compiler", diag[1], diag[2], _local_text(diag[3], root), [])
            continue
        if pending is not None:
            location = _LOCATION.match(line)
            source = _SOURCE.match(line)
            if location:
                pending["locations"].append(
                    {
                        "path": _path(location[1], root),
                        "line": int(location[2]),
                        "column": int(location[3]),
                        "source": "",
                        "label": None,
                    }
                )
                continue
            if source and pending["locations"]:
                existing = pending["locations"][-1]["source"]
                pending["locations"][-1]["source"] = (
                    existing + ("\n" if existing else "") + _local_text(source[1], root)
                )
                continue
            code = re.search(r"#\[(?:warn|deny|forbid)\(([^)]+)\)\]", line)
            if code:
                pending["code"] = code[1]
                continue
            if re.match(r"^\s*(?:\||=|:::)", line):
                continue
        if line == "failures:" or re.match(r"^---- .* stdout ----$", line):
            if suite is not None and suite["failed"]:
                failure_detail = True
            else:
                state["unknown"] += 1
            continue
        if failure_detail:
            continue
        if re.match(r"^\s*Finished\b", line):
            flush()
            state["build_results"].append(True)
            continue
        if re.match(r"^\s*(?:Compiling|Checking|Updating|Downloading|Downloaded|Blocking)\b", line):
            flush()
            continue
        if re.match(
            r"^(?:note:|For more information about this error|Some errors have detailed explanations)",
            line,
        ):
            continue
        state["unknown"] += 1
    flush()
    return suites, headers


def parse_diagnostics(
    stdout: str, stderr: str, *, project_root: Path, context: DiagnosticContext, execution: dict
) -> dict:
    """Return a JSON-serializable schema_version=1 report, independent of gate success.

    Keys: context (profile, variant, original argv list, root-normalized
    comparison_argv list, scope dict, tool_versions dict, configuration_digest),
    execution {valid, completed, successful: bool, reasons: list[str]}, findings,
    domains {compiler, test}, uncertainty: list[str]. No raw output is copied.
    Execution input is the existing check record: returncode:int, timed_out:bool,
    output_limit_exceeded:bool, source_unchanged:bool, passed:bool; optional error
    and interrupted invalidate interrupted/error evidence. Unknown facts are unsafe.

    Each finding has fingerprint:str, domain:compiler|test, severity:str,
    code:str|null, message:str, locations:list[{path:str, line:int|null,
    column:int|null, source:str, label:str|null, optional end_line/end_column}],
    target:dict|null, suite:str|null, test_name:str|null, occurrences:int.
    Locations retain each occurrence; identity ignores line/column offsets.
    Structured rustc spans require integer byte offsets >=0 and line/column
    coordinates >=1, with ordered start/end bounds; booleans are rejected.
    A required code field may explicitly be null, but omission is malformed.
    Compiler targets use normalized manifest/source paths, name and kind, never
    local package_id URLs. External absolute paths retain their full identity.

    Each domain contains observed:bool, complete:bool, uncertainty:list[str].
    Test also has suites:list[{identity:str|null, expected:int|null, passed,
    failed, ignored:list[str], complete:bool, filtered:int|null, uncertainty}].
    Only unambiguous named libtest suites supply negative/pass evidence. Cargo
    stderr headers correlate with stdout suites only when counts agree exactly.
    A skipped check can be parsed with empty streams and execution={}; it cannot
    resolve findings. The caller owns original full logs and their recovery paths.
    """
    root = Path(os.path.abspath(project_root))
    captured = asdict(context)
    captured["argv"] = list(context.argv)
    captured["comparison_argv"] = [_local_text(arg, root) for arg in context.argv]
    result_execution = _execution(execution)
    state = {
        "findings": [],
        "compiler_seen": False,
        "build_results": [],
        "unknown": 0,
        "malformed": 0,
    }
    stdout_suites, stdout_headers = _parse_stream(stdout, root, state)
    stderr_suites, stderr_headers = _parse_stream(stderr, root, state)
    # Preserve stream ordering without inventing cross-stream interleaving.
    for suites, own_headers, other_headers in (
        (stdout_suites, stdout_headers, stderr_headers),
        (stderr_suites, stderr_headers, stdout_headers),
    ):
        if not own_headers and len(suites) == len(other_headers):
            for suite, identity in zip(suites, other_headers):
                suite["identity"] = identity or None
    uncertainty = []
    if state["malformed"]:
        uncertainty.append(f"malformed_records:{state['malformed']}")
    if state["unknown"]:
        uncertainty.append(f"unrecognized_lines:{state['unknown']}")
    if False in state["build_results"]:
        uncertainty.append("unsuccessful_build_observed")
    suites = stdout_suites + stderr_suites
    identities = [s["identity"] for s in suites]
    all_headers = stdout_headers + stderr_headers
    for suite in suites:
        identity = suite["identity"]
        if (
            not identity
            or identities.count(identity) != 1
            or all_headers.count(identity) != 1
            or len(all_headers) != len(suites)
        ):
            suite["identity"] = None
            suite["complete"] = False
            suite["uncertainty"].append("missing_or_ambiguous_suite_identity")
        suite["complete"] = suite["complete"] and result_execution["successful"] and not uncertainty
        for name in suite["failed"]:
            state["findings"].append(
                _finding(
                    "test",
                    "error",
                    None,
                    "test failed: " + name,
                    [],
                    suite=suite["identity"],
                    test_name=name,
                )
            )
    # Text finding identity is finalized after reading its location/anchor/code.
    findings = {}
    for item in state["findings"]:
        item["fingerprint"] = _finding(
            item["domain"],
            item["severity"],
            item["code"],
            item["message"],
            item["locations"],
            target=item["target"],
            suite=item["suite"],
            test_name=item["test_name"],
        )["fingerprint"]
        fingerprint = item["fingerprint"]
        if fingerprint in findings:
            findings[fingerprint]["occurrences"] += 1
            findings[fingerprint]["locations"].extend(item["locations"])
        else:
            findings[fingerprint] = item
    command = _command(context.argv)
    compiler_complete = (
        command is not None
        and result_execution["successful"]
        and not uncertainty
        and False not in state["build_results"]
    )
    test_uncertainty = uncertainty + [r for s in suites for r in s["uncertainty"]]
    if not suites:
        test_uncertainty.append("no_test_execution_observed")
    return {
        "schema_version": 1,
        "context": captured,
        "execution": result_execution,
        "findings": list(findings.values()),
        "uncertainty": uncertainty,
        "domains": {
            "compiler": {
                "observed": state["compiler_seen"]
                or bool(state["build_results"])
                or compiler_complete,
                "complete": compiler_complete,
                "uncertainty": list(uncertainty),
            },
            "test": {
                "observed": bool(suites),
                "complete": bool(suites) and all(s["complete"] for s in suites),
                "uncertainty": test_uncertainty,
                "suites": suites,
            },
        },
    }


def _comparison_reasons(current: dict, baseline: dict | None) -> list[str]:
    if baseline is None:
        return ["no_baseline"]
    if current.get("schema_version") != 1 or baseline.get("schema_version") != 1:
        return ["unsupported_schema"]
    reasons = []
    command = _command(tuple(current.get("context", {}).get("argv", [])))
    if command is None:
        reasons.append("unknown_command")
    required_tools = {"rustc"}
    if command != "rustc":
        required_tools.add("cargo")
    if command == "clippy":
        required_tools.add("clippy")
    if command == "test" and "--doc" in current.get("context", {}).get("argv", []):
        required_tools.add("rustdoc")
    for key in (
        "profile",
        "variant",
        "comparison_argv",
        "scope",
        "tool_versions",
        "configuration_digest",
    ):
        left = current.get("context", {}).get(key)
        right = baseline.get("context", {}).get(key)
        known = bool(left) and bool(right)
        if key == "tool_versions":
            known = known and all(
                isinstance(v, str) and v.strip() for d in (left, right) for v in d.values()
            )
            known = known and all(required_tools <= d.keys() for d in (left, right))
        if not known:
            reasons.append("unknown_context:" + key)
        elif left != right:
            reasons.append("changed_context:" + key)
    return reasons


def _observed(report: dict, finding: dict) -> bool:
    domain = report.get("domains", {}).get(finding["domain"], {})
    if finding["domain"] == "compiler":
        return domain.get("complete") is True
    return any(
        s["identity"] == finding.get("suite") and s["identity"] and s["complete"]
        for s in domain.get("suites", [])
    )


def compare_diagnostics(current: dict, baseline: dict | None) -> dict:
    """Return schema_version, comparable, reasons, counts, and five classification lists.

    Lists are new/persistent/resolved/unclassified/unobserved_previous, with each
    entry a deep-copied finding plus reason:str. Counts count grouped findings,
    not occurrences. Unknown/different context invalidates all matching; known
    matching fingerprints stay persistent despite partial current execution.
    New requires complete baseline domain/suite observation. Missing compiler
    findings resolve only on valid successful complete current compiler evidence.
    Missing test failures require a corresponding named explicit pass in a valid,
    successful, unfiltered, unambiguous complete current suite. Absence is unsafe.
    """
    reasons = _comparison_reasons(current, baseline)
    result = {
        "schema_version": 1,
        "comparable": not reasons,
        "reasons": reasons,
        **{name: [] for name in _CLASSES},
    }
    previous = {f["fingerprint"]: f for f in (baseline or {}).get("findings", [])}
    seen = set()

    def add(kind: str, finding: dict, reason: str):
        result[kind].append(dict(copy.deepcopy(finding), reason=reason))

    for finding in current.get("findings", []):
        fingerprint = finding["fingerprint"]
        identity_known = finding["domain"] != "test" or bool(finding.get("suite"))
        if reasons or not identity_known:
            add("unclassified", finding, "incomparable_context" if reasons else "unknown_suite")
        elif fingerprint in previous:
            seen.add(fingerprint)
            add("persistent", finding, "matching_fingerprint")
        elif _observed(baseline, finding):
            add("new", finding, "absent_from_observed_baseline")
        else:
            add("unclassified", finding, "baseline_domain_not_observed")
    for fingerprint, finding in previous.items():
        if fingerprint in seen:
            continue
        can_resolve = not reasons and _observed(current, finding)
        if finding["domain"] == "test":
            can_resolve = can_resolve and any(
                s["identity"] == finding.get("suite") and finding["test_name"] in s["passed"]
                for s in current.get("domains", {}).get("test", {}).get("suites", [])
            )
        add(
            "resolved" if can_resolve else "unobserved_previous",
            finding,
            "explicit_test_pass"
            if can_resolve and finding["domain"] == "test"
            else "completed_successful_compiler_check"
            if can_resolve
            else "incomparable_context"
            if reasons
            else "current_domain_not_observed",
        )
    result["counts"] = {name: len(result[name]) for name in _CLASSES}
    return result


def summarize_diagnostics(report: dict, *, max_items: int = 20) -> dict:
    """Bound evidence for prompts without modifying the full report.

    Returns schema_version, counts, findings (at most min(max_items, 100)),
    omitted_findings, uncertainty, and omitted_uncertainty. Uses report.comparison
    when present, otherwise its parsed findings as unclassified. Finding projection
    keeps classification/fingerprint/domain/severity/code/message/occurrences, one
    location, suite/test_name and reason. Strings are capped at 240 characters;
    uncertainty is capped at 20 entries. Projection permits only documented
    scalar fields; numeric locations/occurrences must fit unsigned 64-bit values.
    Full report/log references belong to caller.
    """
    if max_items < 0:
        raise ValueError("max_items must be non-negative")
    comparison = report.get("comparison")
    findings = []
    if comparison:
        for name in _CLASSES:
            findings.extend(dict(f, classification=name) for f in comparison.get(name, []))
        counts = copy.deepcopy(comparison["counts"])
    else:
        findings = [dict(f, classification="unclassified") for f in report.get("findings", [])]
        counts = {name: len(findings) if name == "unclassified" else 0 for name in _CLASSES}
    bounded = []
    keys = (
        "classification",
        "fingerprint",
        "domain",
        "severity",
        "code",
        "message",
        "occurrences",
        "suite",
        "test_name",
        "reason",
    )
    for finding in findings[: min(max_items, 100)]:
        item = {}
        for key in keys:
            value = finding.get(key)
            if isinstance(value, str):
                item[key] = value[:240]
            elif key == "occurrences" and type(value) is int and 0 <= value < 2**64:
                item[key] = value
        if finding.get("locations"):
            location = finding["locations"][0]
            item["location"] = {}
            if isinstance(location.get("path"), str):
                item["location"]["path"] = location["path"][:240]
            for key in ("line", "column"):
                value = location.get(key)
                if type(value) is int and 1 <= value < 2**64:
                    item["location"][key] = value
        bounded.append(item)
    uncertainty = list(report.get("uncertainty", []))
    for domain in report.get("domains", {}).values():
        uncertainty.extend(domain.get("uncertainty", []))
    if comparison:
        uncertainty.extend(comparison.get("reasons", []))
    uncertainty = list(dict.fromkeys(uncertainty))
    return {
        "schema_version": 1,
        "counts": counts,
        "findings": bounded,
        "omitted_findings": len(findings) - len(bounded),
        "uncertainty": [u[:240] for u in uncertainty[:20]],
        "omitted_uncertainty": max(0, len(uncertainty) - 20),
    }
