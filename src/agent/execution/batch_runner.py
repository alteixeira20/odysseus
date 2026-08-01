"""Sequential execution and projection of one agent tool batch."""

from dataclasses import dataclass
from enum import Enum
import json
import logging
import re
import secrets
from typing import Any, AsyncIterator, Callable, Optional

from src.agent.events import run_status_event
from src.agent.execution.executor import ToolExecutionHandle
from src.agent.execution.observation_ledger import ObservationLedger
from src.agent.execution.result_adapters import (
    DOCUMENT_TOOL_NAMES,
    note_list_summary,
    project_tool_result,
)
from src.agent.providers.adapters.odysseus_qwen import (
    terminal_tool_summary,
)
from src.agent.rounds.document_stream import normalize_odysseus_qwen_text
from src.agent.runtime_v2.contracts import (
    AgentExecutionContext,
    NormalizedToolCall,
    ToolResult,
    ToolResultStatus,
)
from src.agent.runtime_v2.events import encode_runtime_sse
from src.agent.runtime_v2.executor import execute_normalized_tool_call
from src.agent.runtime_v2.approvals import (
    ApprovalRecordState,
    EFFECT_APPROVALS,
)
from src.agent.runtime_v2.state import RunState
from src.agent.tools.bootstrap import TOOL_REGISTRY


logger = logging.getLogger(__name__)


class BatchDisposition(str, Enum):
    CONTINUE = "continue"
    BUDGET_EXHAUSTED = "budget_exhausted"
    AWAIT_USER = "await_user"
    AWAIT_APPROVAL = "await_approval"
    DOCUMENT_CREATE_COMPLETE = "document_create_complete"
    DOCUMENT_TOOL_COMPLETE = "document_tool_complete"
    DETERMINISTIC_COMPLETE = "deterministic_complete"


@dataclass
class ToolBatchState:
    messages: list[dict]
    full_response: str
    total_tool_calls: int
    tool_events: list[dict]
    relevant_tools: Optional[set[str]]
    effectful_used: bool = False


@dataclass(frozen=True)
class ToolBatchRequest:
    tool_blocks: list[Any]
    converted_calls: list[Any]
    used_native: bool
    round_response: str
    round_reasoning: str
    round_number: int
    max_tool_calls: int
    session_id: Optional[str]
    owner: Optional[str]
    workspace: Optional[str]
    disabled_tools: set[str]
    allowed_tools: set[str]
    execution_mode: str = "disabled"
    normalized_calls: tuple[NormalizedToolCall, ...] = ()
    execution_context: Optional[AgentExecutionContext] = None
    tool_policy: Any = None
    odysseus_qwen_finetune: bool = False
    odysseus_notes_finetune: bool = False
    odysseus_doc_finetune: bool = False
    odysseus_doc_stream_create: bool = False
    observation_ledger: Optional[ObservationLedger] = None


@dataclass(frozen=True)
class ToolBatchOutcome:
    disposition: BatchDisposition
    tool_results: tuple[str, ...]
    tool_result_texts: tuple[str, ...]


class ToolBatchRunner:
    """Own execution, SSE projection, persistence, and result threading."""

    def __init__(
        self,
        request: ToolBatchRequest,
        state: ToolBatchState,
        *,
        execute_tool: Callable[..., Any],
        format_result: Callable[[str, dict[str, Any]], str],
        append_results: Callable[..., None],
        strip_tool_blocks: Callable[..., str],
        bash_timeout_for_block: Callable[[Any], Optional[float]],
        effectful_tools: set[str] | frozenset[str],
        invocation_id: Callable[[int], str] = secrets.token_urlsafe,
        execution_handle: type[ToolExecutionHandle] = ToolExecutionHandle,
    ) -> None:
        self.request = request
        self.state = state
        self.execute_tool = execute_tool
        self.format_result = format_result
        self.append_results = append_results
        self.strip_tool_blocks = strip_tool_blocks
        self.bash_timeout_for_block = bash_timeout_for_block
        self.effectful_tools = effectful_tools
        self.invocation_id = invocation_id
        self.execution_handle = execution_handle
        self.outcome: Optional[ToolBatchOutcome] = None

    async def stream(self) -> AsyncIterator[str]:
        request = self.request
        state = self.state
        tool_results: list[str] = []
        tool_result_texts: list[str] = []
        budget_hit = False
        awaiting_user = False
        awaiting_approval = False
        doc_stream_create_completed = False
        doc_tool_completed = False
        deterministic_tool_completed = False

        for block_index, block in enumerate(request.tool_blocks):
            if (
                request.max_tool_calls > 0
                and state.total_tool_calls >= request.max_tool_calls
            ):
                yield self._event(
                    {
                        "type": "budget_exceeded",
                        "limit": request.max_tool_calls,
                        "used": state.total_tool_calls,
                    }
                )
                budget_hit = True
                break

            state.total_tool_calls += 1
            normalized_call = (
                request.normalized_calls[block_index]
                if block_index < len(request.normalized_calls)
                else None
            )
            canonical_tool_name = (
                normalized_call.canonical_name
                if normalized_call is not None
                else block.tool_type
            )
            definition = TOOL_REGISTRY.resolve(
                canonical_tool_name,
                request.execution_context,
            )
            runtime_v2 = bool(
                normalized_call is not None
                and request.execution_context is not None
                and definition is not None
                and definition.runtime_v2
            )
            is_document_tool = canonical_tool_name in DOCUMENT_TOOL_NAMES
            full_command = block.content.strip()
            call_id = (
                normalized_call.call_id
                if normalized_call is not None
                else self.invocation_id(18)
            )
            bash_timeout = self.bash_timeout_for_block(block)
            command = (
                block.content.split("\n")[0].strip()[:80]
                if is_document_tool
                else full_command
            )
            runtime_result: Optional[ToolResult] = None

            clamped_tool_allowed = (
                request.odysseus_notes_finetune
                and canonical_tool_name
                in {"manage_notes", "manage_calendar", "manage_tasks"}
            )
            if (
                not runtime_v2
                and
                request.tool_policy
                and (
                    request.tool_policy.blocks(block.tool_type)
                    or request.tool_policy.blocks(canonical_tool_name)
                )
                and not clamped_tool_allowed
            ):
                description = f"{canonical_tool_name}: BLOCKED"
                result = {
                    "error": request.tool_policy.reason_for(
                        canonical_tool_name
                    ),
                    "exit_code": 1,
                    "blocked": True,
                }
                logger.info(
                    "Tool blocked before start by policy: %s",
                    canonical_tool_name,
                )
            else:
                yield run_status_event(
                    "executing_tool",
                    f"Executing {canonical_tool_name}",
                    tool=canonical_tool_name,
                    round=request.round_number,
                )
                start_event = {
                    "type": "tool_start",
                    "tool": canonical_tool_name,
                    "command": command,
                    "full_command": full_command,
                    "round": request.round_number,
                    "invocation_id": call_id,
                }
                if bash_timeout is not None:
                    start_event["timeout_seconds"] = bash_timeout
                if runtime_v2:
                    yield encode_runtime_sse(
                        request.execution_context.event_factory.create(
                            "tool_started",
                            {
                                "call_id": call_id,
                                "canonical_name": canonical_tool_name,
                                "raw_name": normalized_call.raw_name,
                                "round": request.round_number,
                                "command": command,
                            },
                            caused_by=call_id,
                        )
                    )
                else:
                    yield self._event(start_event)

                async def run_tool(push_progress):
                    if runtime_v2:
                        return (
                            canonical_tool_name,
                            await execute_normalized_tool_call(
                                normalized_call,
                                request.execution_context,
                                progress_cb=push_progress,
                            ),
                        )
                    return await self.execute_tool(
                        block,
                        session_id=request.session_id,
                        disabled_tools=request.disabled_tools,
                        allowed_tools=set(request.allowed_tools),
                        tool_policy=request.tool_policy,
                        owner=request.owner,
                        progress_cb=push_progress,
                        workspace=request.workspace,
                        execution_mode=request.execution_mode,
                        invocation_id=call_id,
                        execution_context=request.execution_context,
                        normalized_call=normalized_call,
                    )

                async with self.execution_handle(run_tool) as execution:
                    async for progress in execution.progress():
                        yield self._event(
                            {
                                "type": "tool_progress",
                                "tool": canonical_tool_name,
                                "round": request.round_number,
                                "invocation_id": call_id,
                                **progress,
                            }
                        )
                    description, result = await execution.result()

                if runtime_v2:
                    runtime_result = result
                    description = (
                        f"{canonical_tool_name}: {runtime_result.status.value}"
                    )
                    result = runtime_result.legacy_projection()
                    yield encode_runtime_sse(
                        request.execution_context.event_factory.create(
                            "tool_result",
                            {
                                "result": runtime_result.as_dict(),
                                "round": request.round_number,
                                "command": command,
                            },
                            caused_by=call_id,
                        )
                    )
                    if runtime_result.status is ToolResultStatus.APPROVAL_REQUIRED:
                        approval = runtime_result.data.get("approval") or {}
                        approval_id = str(approval.get("approval_id") or "")
                        if not approval_id:
                            awaiting_approval = True
                        else:
                            yield encode_runtime_sse(
                                request.execution_context.event_factory.create(
                                    "run_state",
                                    {
                                        "state": RunState.WAITING_APPROVAL.value,
                                        "reason": "waiting_for_exact_effect_approval",
                                        "resumable": True,
                                        "terminal": False,
                                        "approval_id": approval_id,
                                        "call_id": call_id,
                                        "canonical_name": canonical_tool_name,
                                        "effects": list(
                                            runtime_result.data.get("effects") or []
                                        ),
                                    },
                                    caused_by=call_id,
                                )
                            )
                            decision = await EFFECT_APPROVALS.wait(
                                approval_id,
                                context=request.execution_context,
                            )
                            yield encode_runtime_sse(
                                request.execution_context.event_factory.create(
                                    "run_state",
                                    {
                                        "state": RunState.RUNNING.value,
                                        "reason": f"effect_approval_{decision.value}",
                                        "resumable": False,
                                        "terminal": False,
                                        "approval_id": approval_id,
                                        "call_id": call_id,
                                        "canonical_name": canonical_tool_name,
                                    },
                                    caused_by=call_id,
                                )
                            )
                            if decision is ApprovalRecordState.GRANTED:
                                yield encode_runtime_sse(
                                    request.execution_context.event_factory.create(
                                        "tool_resumed",
                                        {
                                            "call_id": call_id,
                                            "canonical_name": canonical_tool_name,
                                            "approval_id": approval_id,
                                            "round": request.round_number,
                                        },
                                        caused_by=call_id,
                                    )
                                )
                            runtime_result = await execute_normalized_tool_call(
                                normalized_call,
                                request.execution_context,
                                approval_id=approval_id,
                            )
                            description = (
                                f"{canonical_tool_name}: {runtime_result.status.value}"
                            )
                            result = runtime_result.legacy_projection()
                            yield encode_runtime_sse(
                                request.execution_context.event_factory.create(
                                    "tool_result",
                                    {
                                        "result": runtime_result.as_dict(),
                                        "round": request.round_number,
                                        "command": command,
                                        "approval_id": approval_id,
                                    },
                                    caused_by=call_id,
                                )
                            )

            if request.observation_ledger is not None:
                observation_notice = request.observation_ledger.note_tool_result(
                    tool=canonical_tool_name,
                    content=block.content,
                    result=result,
                )
                if observation_notice:
                    result = {
                        **result,
                        "repeated_observation": True,
                        "observation_notice": observation_notice,
                    }

            self._unlock_skill_tools(block, result)
            projection = project_tool_result(
                tool_name=block.tool_type,
                command=command,
                description=description,
                result=result,
                invocation_id=call_id,
                round_number=request.round_number,
                current_response=state.full_response,
            )
            if projection.question_delta:
                state.full_response += projection.question_delta
            if projection.awaiting_user:
                awaiting_user = True
            for event in projection.before_summary_events:
                if runtime_v2 and event.get("type") == "tool_output":
                    continue
                yield self._event(event)

            notes_text = self._notes_result_text(block, result)
            if notes_text:
                clean = self.strip_tool_blocks(
                    state.full_response
                ).strip()
                if notes_text not in clean:
                    prefix = "\n\n" if clean else ""
                    state.full_response = (
                        clean + prefix + notes_text
                    ).strip()
                    yield self._event({"delta": prefix + notes_text})
                deterministic_tool_completed = True

            tasks_text = self._tasks_result_text(block, result)
            if tasks_text:
                clean = self.strip_tool_blocks(
                    state.full_response
                ).strip()
                if tasks_text not in clean:
                    prefix = "\n\n" if clean else ""
                    state.full_response = (
                        clean + prefix + tasks_text
                    ).strip()
                    yield self._event({"delta": prefix + tasks_text})
                deterministic_tool_completed = True

            if (
                request.odysseus_qwen_finetune
                and not result.get("error")
            ):
                summary = terminal_tool_summary(
                    {
                        "tool": block.tool_type,
                        "desc": description,
                        "command": block.content,
                        "output": (
                            result.get("output")
                            or result.get("response")
                            or result.get("results")
                            or result.get("content")
                            or projection.output_text
                            or ""
                        ),
                    }
                )
                if summary:
                    summary = normalize_odysseus_qwen_text(
                        summary
                    ).strip()
                    clean = self.strip_tool_blocks(
                        state.full_response
                    ).strip()
                    state.full_response = summary
                    if summary not in clean:
                        yield self._event({"delta": summary})
                    deterministic_tool_completed = True

            for event in projection.after_summary_events:
                if (
                    projection.note_anchor
                    and event.get("delta") == projection.note_anchor
                ):
                    state.full_response = (
                        state.full_response.rstrip()
                        + projection.note_anchor
                    ).strip()
                yield self._event(event)

            state.tool_events.append(projection.tool_event)
            if canonical_tool_name in self.effectful_tools or (
                runtime_result is not None and runtime_result.committed_effects
            ):
                state.effectful_used = True

            formatted = (
                json.dumps(runtime_result.as_dict(), ensure_ascii=False, default=str)
                if runtime_result is not None
                else self.format_result(description, result)
            )
            tool_results.append(formatted)
            tool_result_texts.append(formatted)
            if (
                request.odysseus_doc_stream_create
                and block.tool_type == "create_document"
                and result.get("action") == "create"
            ):
                doc_stream_create_completed = True
            if (
                request.odysseus_doc_finetune
                and block.tool_type
                in {
                    "create_document",
                    "update_document",
                    "edit_document",
                    "suggest_document",
                }
                and not result.get("error")
            ):
                doc_tool_completed = True

        disposition = BatchDisposition.CONTINUE
        if budget_hit:
            disposition = BatchDisposition.BUDGET_EXHAUSTED
        elif awaiting_approval:
            disposition = BatchDisposition.AWAIT_APPROVAL
        elif awaiting_user:
            disposition = BatchDisposition.AWAIT_USER
        elif doc_stream_create_completed:
            disposition = BatchDisposition.DOCUMENT_CREATE_COMPLETE
            if not state.full_response.strip():
                state.full_response = "Done."
                yield self._event({"delta": "Done."})
        elif doc_tool_completed:
            disposition = BatchDisposition.DOCUMENT_TOOL_COMPLETE
            if (
                not state.full_response.strip()
                or state.full_response.strip().startswith("```")
            ):
                state.full_response = "Done."
                yield self._event({"delta": "Done."})
        elif (
            (
                request.odysseus_notes_finetune
                or request.odysseus_qwen_finetune
            )
            and deterministic_tool_completed
        ):
            disposition = BatchDisposition.DETERMINISTIC_COMPLETE

        if disposition is BatchDisposition.CONTINUE:
            self.append_results(
                state.messages,
                request.round_response,
                request.converted_calls,
                tool_results,
                tool_result_texts,
                request.used_native,
                request.round_number,
                round_reasoning=request.round_reasoning,
            )
            yield self._event(
                {
                    "type": "agent_step",
                    "round": request.round_number + 1,
                }
            )
            state.full_response += "\n\n"

        self.outcome = ToolBatchOutcome(
            disposition=disposition,
            tool_results=tuple(tool_results),
            tool_result_texts=tuple(tool_result_texts),
        )

    def _unlock_skill_tools(self, block: Any, result: dict) -> None:
        selected = self.state.relevant_tools
        if (
            block.tool_type != "manage_skills"
            or selected is None
            or result.get("error")
        ):
            return
        arguments: dict[str, Any] = {}
        raw = str(block.content or "").strip()
        if raw.startswith("{"):
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    arguments = decoded
            except json.JSONDecodeError:
                pass
        name = str(arguments.get("name") or "").strip()
        if not name or arguments.get("action") not in ("view", "view_ref"):
            return
        try:
            from services.memory.skills import SkillsManager
            from src.constants import DATA_DIR
            from src.tool_policy import known_tool_names

            known = known_tool_names()
            for skill in SkillsManager(DATA_DIR).load(
                owner=self.request.owner
            ):
                if skill.get("name") != name:
                    continue
                unlocked = {
                    tool
                    for tool in (skill.get("requires_toolsets") or [])
                    if (
                        tool in known
                        and tool in self.request.allowed_tools
                        and tool not in selected
                    )
                }
                if unlocked:
                    selected.update(unlocked)
                    logger.info(
                        "[tool-rag] skill '%s' unlocked tools for "
                        "next round: %s",
                        name,
                        sorted(unlocked),
                    )
                break
        except Exception as exc:
            logger.debug(
                "skill requires_toolsets unlock skipped: %s",
                exc,
            )

    @staticmethod
    def _notes_result_text(block: Any, result: dict) -> str:
        if block.tool_type != "manage_notes":
            return ""
        action = ToolBatchRunner._json_action(block.content)
        if result.get("error"):
            return ""
        if action in {"list", "search", "find", "view", "lis"}:
            return note_list_summary(
                result.get("output")
                or result.get("results")
                or result.get("content")
                or ""
            )
        if action not in {"add", "update", "delete", "toggle_item"}:
            return ""
        text = str(
            result.get("response")
            or result.get("output")
            or result.get("results")
            or ""
        ).strip()
        if text.startswith("AI: "):
            text = text[4:].strip()
        if text and not re.match(
            r"^(done|note|item|deleted)\b",
            text,
            re.IGNORECASE,
        ):
            text = f"Done — {text}"
        return text

    @staticmethod
    def _tasks_result_text(block: Any, result: dict) -> str:
        if block.tool_type != "manage_tasks" or result.get("error"):
            return ""
        action = ToolBatchRunner._json_action(block.content)
        text = str(
            result.get("response")
            or result.get("output")
            or result.get("results")
            or ""
        ).strip()
        if text.startswith("AI: "):
            text = text[4:].strip()
        if (
            action != "list"
            and text
            and not re.match(
                r"^(done|created|updated|deleted|task)\b",
                text,
                re.IGNORECASE,
            )
        ):
            text = f"Done — {text}"
        return text

    @staticmethod
    def _json_action(raw: str) -> str:
        try:
            arguments = json.loads(raw or "{}")
        except Exception:
            return ""
        return (
            str(arguments.get("action") or "").lower()
            if isinstance(arguments, dict)
            else ""
        )

    @staticmethod
    def _event(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload)}\n\n"
