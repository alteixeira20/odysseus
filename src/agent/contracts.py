"""Pure runtime contracts shared by future agent components."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class AgentLimits:
    """Per-run limits, distinct from provider and background-job lifetimes."""

    max_rounds: int = 50
    max_tool_calls: int = 0
    context_length: int = 0
    max_tokens: int = 4096


@dataclass(frozen=True)
class ActiveContexts:
    """Application context selected for one agent run."""

    active_document: Any = None
    active_email: Optional[Mapping[str, str]] = None
    workspace: Optional[str] = None
    uploaded_files: Optional[Sequence[Mapping[str, Any]]] = None


@dataclass(frozen=True)
class ToolPolicySnapshot:
    """The tool-selection inputs carried by a run request."""

    disabled_tools: Optional[frozenset[str]] = None
    relevant_tools: Optional[frozenset[str]] = None
    forced_tools: Optional[frozenset[str]] = None
    shell_enabled: Optional[bool | str] = None
    execution_mode: Optional[str] = None
    policy: Any = None


@dataclass(frozen=True)
class ModelOptions:
    """Provider-facing options that should not leak into orchestration state."""

    headers: Optional[Mapping[str, str]] = None
    temperature: float = 0.3
    prompt_type: Optional[str] = None
    fallbacks: Optional[Sequence[tuple]] = None


@dataclass(frozen=True)
class AgentRunRequest:
    """Typed representation of the legacy ``stream_agent_loop`` arguments."""

    endpoint_url: str
    model: str
    messages: list[dict]
    session_id: Optional[str] = None
    owner: Optional[str] = None
    limits: AgentLimits = field(default_factory=AgentLimits)
    contexts: ActiveContexts = field(default_factory=ActiveContexts)
    policy: ToolPolicySnapshot = field(default_factory=ToolPolicySnapshot)
    model_options: ModelOptions = field(default_factory=ModelOptions)
    plan_mode: bool = False
    approved_plan: Optional[str] = None
    workload: str = "foreground"
    is_teacher_run: bool = False

    @classmethod
    def from_legacy_arguments(
        cls,
        endpoint_url: str,
        model: str,
        messages: list[dict],
        headers: Optional[Mapping[str, str]] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        prompt_type: Optional[str] = None,
        max_rounds: int = 50,
        max_tool_calls: int = 0,
        context_length: int = 0,
        active_document: Any = None,
        active_email: Optional[Mapping[str, str]] = None,
        session_id: Optional[str] = None,
        disabled_tools: Optional[set[str]] = None,
        owner: Optional[str] = None,
        relevant_tools: Optional[set[str]] = None,
        fallbacks: Optional[Sequence[tuple]] = None,
        plan_mode: bool = False,
        approved_plan: Optional[str] = None,
        tool_policy: Any = None,
        workspace: Optional[str] = None,
        forced_tools: Optional[set[str]] = None,
        uploaded_files: Optional[Sequence[Mapping[str, Any]]] = None,
        workload: str = "foreground",
        _is_teacher_run: bool = False,
        shell_enabled: Optional[bool | str] = None,
        execution_mode: Optional[str] = None,
    ) -> "AgentRunRequest":
        return cls(
            endpoint_url=endpoint_url,
            model=model,
            messages=messages,
            session_id=session_id,
            owner=owner,
            limits=AgentLimits(
                max_rounds=max_rounds,
                max_tool_calls=max_tool_calls,
                context_length=context_length,
                max_tokens=max_tokens,
            ),
            contexts=ActiveContexts(
                active_document=active_document,
                active_email=active_email,
                workspace=workspace,
                uploaded_files=(
                    tuple(uploaded_files)
                    if uploaded_files is not None
                    else None
                ),
            ),
            policy=ToolPolicySnapshot(
                disabled_tools=(
                    frozenset(disabled_tools)
                    if disabled_tools is not None
                    else None
                ),
                relevant_tools=(
                    frozenset(relevant_tools)
                    if relevant_tools is not None
                    else None
                ),
                forced_tools=(
                    frozenset(forced_tools)
                    if forced_tools is not None
                    else None
                ),
                shell_enabled=shell_enabled,
                execution_mode=execution_mode,
                policy=tool_policy,
            ),
            model_options=ModelOptions(
                headers=headers,
                temperature=temperature,
                prompt_type=prompt_type,
                fallbacks=(
                    tuple(fallbacks)
                    if fallbacks is not None
                    else None
                ),
            ),
            plan_mode=plan_mode,
            approved_plan=approved_plan,
            workload=workload,
            is_teacher_run=_is_teacher_run,
        )


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class UsageAccumulator:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    def add(self, usage: Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cached_tokens += usage.cached_tokens

    def snapshot(self) -> Usage:
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens,
        )


@dataclass
class AgentRunState:
    messages: list[dict]
    response_text: str = ""
    tool_events: list[dict] = field(default_factory=list)
    round_texts: list[str] = field(default_factory=list)
    usage: UsageAccumulator = field(default_factory=UsageAccumulator)
    round_number: int = 0


@dataclass(frozen=True)
class NormalizedToolCall:
    name: str
    arguments: Mapping[str, Any]
    call_id: Optional[str] = None


class RoundTermination(str, Enum):
    COMPLETED = "completed"
    TOOL_CALLS = "tool_calls"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXHAUSTED = "exhausted"


class SupervisorAction(str, Enum):
    CONTINUE = "continue"
    FINISH = "finish"
    FORCE_ANSWER = "force_answer"
    RETRY_WITH_INSTRUCTION = "retry_with_instruction"
    AWAIT_USER = "await_user"
    EXHAUSTED = "exhausted"


class RunDisposition(str, Enum):
    """Semantic outcome of a run, distinct from its stream ending.

    Every terminal path must select one of these values.  In particular,
    hitting an orchestration/provider limit is never represented as a
    successful completion merely because no more events will be emitted.
    """

    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ROUNDS_EXHAUSTED = "rounds_exhausted"
    AWAITING_INPUT = "awaiting_input"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class SupervisorDecision:
    action: SupervisorAction
    reason: str
    instruction: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoundOutcome:
    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[NormalizedToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    emitted_events: tuple[Any, ...] = ()
    termination: RoundTermination = RoundTermination.COMPLETED
