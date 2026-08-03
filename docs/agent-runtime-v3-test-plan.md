# Runtime V3 test plan

This foundation is not considered ready until the branch passes:

- Python compilation of every changed runtime module.
- Durable ledger tests for monotonic events, idempotent effects, illegal state
  transitions, process interruption, and unknown effect classification.
- Context projection tests that protect system instructions, latest-user intent,
  and native tool-call/result pairing.
- MCP isolation tests for secret non-inheritance, exact package pinning, call
  deadlines, and bounded outputs.
- Orchestrator tests for terminal-state correctness when a provider stream exits
  without a terminal event.
- JavaScript syntax validation after timeout and recovery UX changes.
- Repository diff whitespace validation.

The temporary PR builder applies deterministic cross-cutting edits in a complete
Git checkout, runs the checks above, and commits only a verified integration
diff. The builder and codemod are removed before final review.
