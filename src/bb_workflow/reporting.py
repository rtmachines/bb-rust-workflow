"""Readable views of current, historical and partial workflow audit evidence."""

from __future__ import annotations

import json


def _evidence(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def render_report(state: dict, plan: dict, requirements: dict | None) -> str:
    """Render audit evidence without creating or implying approval for partial runs."""
    acceptance = state.get("acceptance", {})
    approved = state.get("status") == "succeeded" and bool(state.get("acceptance_report"))
    cost = f"{state.get('known_cost_usd', 0):.4f} USD reported"
    if state.get("unknown_cost_calls"):
        cost += f"; {state['unknown_cost_calls']} call(s) without cost data; total unknown"
    config = state.get("config", {})
    lines = [
        "# Workflow report",
        "",
        f"Status: **{state.get('status', 'unknown')}**",
        "",
        state.get("reason", ""),
        "",
        f"Complexity: {state.get('complexity', 'unknown')}",
        f"Acceptance mode: {acceptance.get('mode', 'unknown')}",
        f"Legacy compatibility: {acceptance.get('compatibility_mode', 'unknown')}",
        f"Requirements: {acceptance.get('requirements_id')}",
        f"Verification plan: {acceptance.get('verification_plan_id')}",
        f"Fixer invocations: {state.get('cycles', 0)}/{config.get('max_cycles', 'unknown')}",
        f"Agent calls: {state.get('agent_calls', 0)}/{config.get('max_agent_calls', 'unknown')}",
        f"Cost: {cost}",
        "",
        "Original project files were not automatically updated.",
        "",
        "Final approval: recorded in acceptance.json for the final candidate."
        if approved
        else "No final approval. Evidence below may be incomplete or historical.",
    ]
    if acceptance.get("mode") == "legacy":
        lines.extend(["", "Legacy mode: criteria absent. No requirements guarantee is made."])
    if approved:
        lines.append(f"Approval binding: {_evidence(state['acceptance_report']['binding'])}")
    lines.extend(
        [
            "",
            "## Gate scope",
            "",
            f"Profile: {plan.get('profile', 'unknown')}; package: {plan.get('package')}; "
            f"test filter: {plan.get('test_filter')} (full gate requires both absent).",
            "Planned variants: " + ", ".join(plan.get("variants", [])),
            "",
            "### Planned checks",
            "",
        ]
    )
    for check in plan.get("checks", []):
        lines.append(
            f"- {check['id']} [{check['profile']}/{check['variant']}]: " + " ".join(check["argv"])
        )
    checks = state.get("checks", [])
    phases = dict.fromkeys(c["phase"] for c in checks)
    for phase in phases:
        executed = list(
            dict.fromkeys(c.get("variant", "unknown") for c in checks if c["phase"] == phase)
        )
        lines.extend(
            ["", f"### Execution: {phase}", "", "Executed variants: " + ", ".join(executed)]
        )
        phase_results = [c for c in checks if c["phase"] == phase]
        planned_ids = {c["id"] for c in plan.get("checks", [])}
        executed_ids = {c.get("id") for c in phase_results}
        if not phase.endswith("-prepare"):
            lines.append(
                "Unexecuted planned checks: "
                + (", ".join(sorted(planned_ids - executed_ids)) or "none")
            )
        for check in phase_results:
            lines.append(
                f"- {'PASS' if check.get('passed') else 'FAIL'} [{check.get('variant', 'unknown')}]: "
                + " ".join(check["command"])
                + f"; logs: {check.get('artifacts', 'unavailable')}"
            )
            lines.append(
                f"  Candidate: {check.get('snapshot_id')}; result: {check.get('result_snapshot_id')}; "
                f"source unchanged: {check.get('source_unchanged')}"
            )
            if check.get("error"):
                lines.append("  Error: " + check["error"])
            if check.get("diagnostics"):
                lines.append("  Diagnostic classifications: " + _evidence(check["diagnostics"]))
            if check.get("diagnostics_file"):
                lines.append("  Diagnostics: " + check["diagnostics_file"])
    lines.extend(["", "## Diagnostic comparisons", ""])
    for phase, diagnostics in state.get("diagnostics", {}).items():
        lines.append(
            f"- {phase}: {_evidence(diagnostics.get('counts', {}))}; "
            f"full comparison: {diagnostics.get('file', 'unavailable')}"
        )
    descriptions = {c["id"]: c for c in (requirements or {}).get("criteria", [])}
    lines.extend(["", "## Acceptance criteria and rounds", ""])
    for criterion in descriptions.values():
        lines.append(
            f"- {criterion['id']}: {criterion['description']} ({criterion['kind']}); "
            f"verification: {criterion['verification']}"
        )
    for round_record in acceptance.get("rounds", []):
        is_final = approved and round_record.get("cycle") == state.get("cycles")
        lines.extend(
            [
                "",
                f"### Cycle {round_record.get('cycle')}: "
                + (
                    "final approved evidence"
                    if is_final
                    else "historical/partial evidence; does not certify the current candidate"
                ),
                "",
                "Binding: " + _evidence(round_record.get("binding", {})),
            ]
        )
        results = [*round_record.get("mechanical", []), *round_record.get("semantic", [])]
        for result in results:
            lines.append(
                f"- {result['criterion_id']}: **{result['verdict']}**; "
                f"evidence: {_evidence(result.get('evidence'))}; "
                f"binding: {_evidence(result.get('binding'))}"
            )
            if result.get("rationale"):
                lines.append("  Rationale: " + result["rationale"])
        missing = descriptions.keys() - {r["criterion_id"] for r in results}
        if missing:
            lines.append("Missing criterion results: " + ", ".join(sorted(missing)))
        if round_record.get("audit_error"):
            lines.append("Partial audit error: " + round_record["audit_error"])
    lines.extend(["", "## Independent reviews and triage", ""])
    for review in state.get("reviews", []):
        is_final = approved and review.get("cycle") == state.get("cycles")
        lines.extend(
            [
                "",
                f"### Candidate {review.get('snapshot_id')} "
                + ("(final approved review)" if is_final else "(historical/partial review)"),
                "",
            ]
        )
        for role, response in zip(("reviewer_a", "reviewer_b"), review.get("reviews", [])):
            lines.append(f"- {role}: {response.get('status')}; {response.get('summary')}")
            for finding in response.get("findings", []):
                lines.append(
                    f"  Finding {finding['id']} [{finding['severity']}] {finding['path']}:{finding['line']}: "
                    f"{finding['title']}; {finding['detail']}; evidence: {finding['evidence']}"
                )
            for assessment in response.get("criterion_assessments", []):
                lines.append(
                    f"  Criterion {assessment['criterion_id']}: {assessment['verdict']}; "
                    f"evidence: {assessment['evidence']}; binding: {_evidence(response.get('binding'))}"
                )
        triage = review.get("triage")
        if triage:
            lines.append(
                f"- Triage: {triage.get('status')}; {triage.get('summary')}; "
                f"binding: {_evidence(triage.get('binding'))}"
            )
            for decision in triage.get("decisions", []):
                lines.append(
                    f"  Finding {decision['finding_id']}: {decision['disposition']}; "
                    f"duplicate of: {decision['duplicate_of']}; rationale: {decision['rationale']}"
                )
            for decision in triage.get("criterion_decisions", []):
                lines.append(
                    f"  Criterion {decision['criterion_id']}: {decision['verdict']}; "
                    f"rationale: {decision['rationale']}"
                )
        for finding in [*review.get("confirmed", []), *review.get("unresolved", [])]:
            lines.append(
                f"- Remaining {finding['severity']}: {finding['path']}:{finding['line']} — {finding['title']}"
            )
    lines.extend(
        [
            "",
            "## Tokens and prompt size",
            "",
            "Prompt characters/UTF-8 bytes measure submitted text only, not provider tokens "
            "or the CLI's instructions, tools and later reads.",
        ]
    )
    for backend, usage in state.get("token_usage", {}).get("by_backend", {}).items():
        measured = ", ".join(
            f"{name}={value if value is not None else 'unknown'}"
            for name, value in usage["known_totals"].items()
        )
        lines.append(
            f"- {backend}: measured subtotals {measured}; "
            f"{usage['complete_calls']}/{usage['calls']} calls with known total, "
            f"{usage['partial_calls']} partial, {usage['unknown_calls']} unknown. "
            "Cache counters are part of input, not additional tokens."
        )
    lines.extend(["", "## Agent summaries", ""])
    for agent in state.get("agents", []):
        lines.append(
            f"- {agent['role']} ({agent['selection']['model']}): "
            + agent.get("summary", agent.get("error", agent["status"]))
            + f" — prompt {agent['prompt_chars']} chars / {agent['prompt_bytes']} bytes; "
            + f"audit: {agent.get('artifacts', 'unavailable')}"
        )
        if agent.get("response") and agent["role"] in {"reviewer_a", "reviewer_b", "triage"}:
            # A cycle record may predate a blocked response; suppress fallback only when
            # this exact role response was incorporated into that rendered review.
            represented = any(
                review.get("cycle") == agent.get("cycle")
                and (
                    review.get("triage") == agent["response"]
                    if agent["role"] == "triage"
                    else len(review.get("reviews", []))
                    > (0 if agent["role"] == "reviewer_a" else 1)
                    and review["reviews"][0 if agent["role"] == "reviewer_a" else 1]
                    == agent["response"]
                )
                for review in state.get("reviews", [])
            )
            if not represented:
                lines.append(
                    "  Returned historical/partial evidence; no final approval: "
                    + _evidence(agent["response"])
                )
    lines.extend(
        [
            "",
            "Full evidence, routing, findings, triage and raw log paths: state.json.",
            "",
            "A succeeded result records the configured full gate and independent reviews for "
            "the bound candidate. Declared criteria, when present, have current recorded evidence; "
            "this is not proof that every requirement or defect was covered. Local digests detect "
            "inconsistencies, not coordinated rewriting of all audit artifacts.",
        ]
    )
    return "\n".join(lines) + "\n"
