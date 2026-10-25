"""Offline agents with REAL Cargo checks. Run after installing this package.

This is a test fixture, not a Claude/Codex session. It leaves a reviewable run
inside the example project without changing its source.
"""

from pathlib import Path

from bb_workflow.backends import BackendResult
from bb_workflow.config import load_config
from bb_workflow.engine import Workflow


def fixture_agent(**kwargs):
    if kwargs["role"] == "implementation":
        source = kwargs["cwd"] / "src" / "lib.rs"
        source.write_text(
            source.read_text(encoding="utf-8")
            .replace("value + 1", "value.saturating_add(1)")
            .replace(
                "    #[test]",
                "    #[test]\n    fn saturates_at_maximum() {\n"
                "        assert_eq!(increment(u32::MAX), u32::MAX);\n    }\n\n    #[test]",
                1,
            ),
            encoding="utf-8",
        )
    return BackendResult(
        payload={
            "status": "completed",
            "summary": "OFFLINE FIXTURE: not a model review",
            "findings": [],
            "decisions": [],
        },
        cost_usd=0.0,
        usage={},
        model="offline-fixture",
    )


if __name__ == "__main__":
    project = Path(__file__).resolve().parent / "rust-counter"
    workflow = Workflow(
        project,
        "Make increment saturate at u32::MAX and add a regression test",
        load_config(),
        "low",
        invoke_fn=fixture_agent,
    )
    run = workflow.run()
    print(f"OFFLINE AGENTS, REAL CARGO: {workflow.state['status']} — {run}")
    raise SystemExit(0 if workflow.state["status"] == "succeeded" else 1)
