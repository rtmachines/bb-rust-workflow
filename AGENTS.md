# bb-rust-workflow

Python 3.11+ CLI orchestrating Claude Code and Codex for Rust projects; runtime uses stdlib only.

- Start with the affected module in `src/bb_workflow/` and its `tests/test_<module>.py`.
  Read `docs/architecture.md` only for stage, snapshot or apply changes.
- Module map: `backends` = provider CLI/contracts; `process` = limits/cleanup;
  `engine` = stages/prompts/audit; `protocol` = response validation/triage;
  `workspace` = snapshots/apply; `config` = routing; `usage` = counters; `cli` = commands.
- Keep stage order, independent reviews, frozen-source checks and explicit apply guarantees.
  Prompt changes must retain evidence recovery and structured response validation.
- Prefer scoped reads; do not load `dist/`, run artifacts or every skill/document as context.
  Use fixture backends for local tests; live runs consume provider budget.
- Tests without installation: `PYTHONPATH=src python3 -m unittest discover -s tests -q`.
  Focused example: `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_backends.py' -q`.
  Check `ruff check src tests examples` and `ruff format --check src tests examples`.
- Use fixture providers for regressions; paid provider sessions need an explicit request.
  If reviewdog is available, use its local reporter with raw Ruff diagnostics; see README.
- Keep full audit logs on disk; distinguish prompt size from measured provider tokens and
  unknown usage from zero. No model switches to reduce cost without a configuration change.
