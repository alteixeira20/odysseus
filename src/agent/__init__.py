"""Typed boundaries for the incremental Odysseus agent-runtime refactor.

The production runtime remains in :mod:`src.agent_loop` while responsibilities
are extracted behind compatibility exports.  Modules in this package must not
depend on HTTP route modules.
"""

from .contracts import (
    ActiveContexts,
    AgentLimits,
    AgentRunRequest,
    AgentRunState,
    ModelOptions,
    NormalizedToolCall,
    RoundOutcome,
    RoundTermination,
    ToolPolicySnapshot,
    Usage,
    UsageAccumulator,
)
from .events import AgentEvent, encode_legacy_sse

__all__ = [
    "ActiveContexts",
    "AgentEvent",
    "AgentLimits",
    "AgentRunRequest",
    "AgentRunState",
    "ModelOptions",
    "NormalizedToolCall",
    "RoundOutcome",
    "RoundTermination",
    "ToolPolicySnapshot",
    "Usage",
    "UsageAccumulator",
    "encode_legacy_sse",
]
