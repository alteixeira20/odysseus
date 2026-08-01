"""Authoritative Runtime V2 definitions and handlers for the coding slice."""

from __future__ import annotations

import json
import time
from typing import Any, Mapping, Optional

from src.agent.planning.service import markdown_checklist_to_steps
from src.agent.tools.registry import (
    ToolAutonomy,
    ToolCategory,
    ToolDefinition,
    ToolIdempotency,
    ToolRisk,
)
from src.execution_policy import ExecutionMode

from .contracts import (
    AgentExecutionContext,
    Capability,
    Effect,
    ToolError,
    ToolResult,
    ToolResultStatus,
)
from .effect_policy import (
    DefinitionApprovalPolicy,
    resolve_command_effects,
    resolve_python_effects,
    resolve_workspace_patch_effects,
    resolve_workspace_read_effects,
)
from .process_service import (
    PROCESS_SERVICE,
    ProcessSandboxUnavailable,
    ProcessServiceError,
)
from .workspace_service import WORKSPACE_SERVICE, WorkspaceError


_RESULT_SCHEMA = {
    "type": "object",
    "required": [
        "call_id",
        "canonical_name",
        "status",
        "data",
        "error",
        "committed_effects",
        "artifacts",
        "truncation",
        "continuation",
        "backend",
        "duration_ms",
    ],
    "properties": {
        "call_id": {"type": "string", "minLength": 1},
        "canonical_name": {"type": "string", "minLength": 1},
        "status": {
            "enum": [
                "success",
                "error",
                "denied",
                "approval_required",
                "cancelled",
                "timed_out",
                "incomplete",
            ]
        },
        "data": {"type": "object"},
        "error": {"type": ["object", "null"]},
        "committed_effects": {"type": "array"},
        "artifacts": {"type": "array"},
        "truncation": {"type": ["object", "null"]},
        "continuation": {"type": ["object", "null"]},
        "backend": {"type": "string"},
        "duration_ms": {"type": "number", "minimum": 0},
    },
}


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("tool arguments must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def _adapt_ask_user(raw: Any, raw_name: str) -> Mapping[str, Any]:
    arguments = _json_object(raw)
    options = arguments.get("options")
    if isinstance(options, list):
        arguments["options"] = [
            {"label": item} if isinstance(item, str) else item
            for item in options
        ][:6]
    return arguments


def _adapt_plan(raw: Any, raw_name: str) -> Mapping[str, Any]:
    if raw_name == "update_plan" and not isinstance(raw, Mapping):
        text = str(raw or "").strip()
        try:
            arguments = _json_object(text)
        except ValueError:
            arguments = {"plan": text}
    else:
        arguments = _json_object(raw)
    if raw_name == "update_plan":
        markdown = str(arguments.get("plan") or "")
        steps = markdown_checklist_to_steps(markdown)
        if not steps:
            raise ValueError("plan must contain at least one checklist step")
        return {
            "action": "replace",
            "steps": steps,
        }
    if raw_name == "todowrite":
        return {
            "action": "replace",
            "steps": [
                {
                    "content": str(item.get("content") or item.get("text") or ""),
                    "status": str(item.get("status") or "pending"),
                    "priority": str(item.get("priority") or "medium"),
                }
                for item in arguments.get("todos") or []
                if isinstance(item, Mapping)
            ],
        }
    if "action" not in arguments and "plan" in arguments:
        arguments = {"action": "replace", "steps": arguments.get("plan") or []}
    return arguments


def _adapt_empty(raw: Any, raw_name: str) -> Mapping[str, Any]:
    return {}


def _adapt_find(raw: Any, raw_name: str) -> Mapping[str, Any]:
    if raw_name == "ls" and not isinstance(raw, Mapping):
        text = str(raw or "").strip()
        try:
            arguments = _json_object(text)
        except ValueError:
            # Fenced legacy ``ls`` calls historically accepted a bare path.
            # Normalize that compatibility spelling here, before validation.
            arguments = {"path": text}
    else:
        arguments = _json_object(raw)
    if raw_name == "ls":
        path = arguments.get("path") or "."
        return {
            "path": path,
            "patterns": ["*"],
            "max_results": arguments.get("max_results") or 200,
            "offset": arguments.get("offset") or 0,
        }
    if raw_name == "glob":
        pattern = arguments.get("pattern")
        if not pattern:
            raise ValueError("pattern is required")
        return {
            "path": arguments.get("path") or ".",
            "patterns": [pattern] if isinstance(pattern, str) else pattern,
            "exclude": arguments.get("exclude") or [],
            "max_results": arguments.get("max_results") or 200,
            "offset": arguments.get("offset") or 0,
        }
    return arguments


def _adapt_search(raw: Any, raw_name: str) -> Mapping[str, Any]:
    arguments = _json_object(raw)
    if "glob" in arguments and "include" not in arguments:
        arguments["include"] = [arguments.pop("glob")]
    if "ignore_case" in arguments and "case_sensitive" not in arguments:
        arguments["case_sensitive"] = not bool(arguments.pop("ignore_case"))
    if not str(arguments.get("pattern") or ""):
        raise ValueError("pattern is required")
    return arguments


def _adapt_read(raw: Any, raw_name: str) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        arguments = dict(raw)
    else:
        text = str(raw or "").strip()
        if text.startswith("{"):
            arguments = _json_object(text)
        else:
            arguments = {"path": text}
    if "requests" in arguments:
        return arguments
    offset = int(arguments.get("offset") or 0)
    limit = int(arguments.get("limit") or 0)
    request: dict[str, Any] = {
        "path": str(arguments.get("path") or ""),
        "start_line": max(offset, 1) if offset else 1,
    }
    if limit:
        request["end_line"] = request["start_line"] + limit - 1
    if arguments.get("expected_sha256"):
        request["expected_sha256"] = arguments["expected_sha256"]
    return {
        "requests": [request],
        "max_output_chars": arguments.get("max_output_chars") or 40_000,
        **(
            {"expected_workspace_revision": arguments["expected_workspace_revision"]}
            if arguments.get("expected_workspace_revision")
            else {}
        ),
    }


def _adapt_patch(raw: Any, raw_name: str) -> Mapping[str, Any]:
    arguments = _json_object(raw) if isinstance(raw, Mapping) or str(raw or "").strip().startswith("{") else {}
    if raw_name == "write_file":
        if not arguments:
            text = str(raw or "")
            path, _, content = text.partition("\n")
            arguments = {"path": path, "content": content}
        operation = {
            "type": "write",
            "path": arguments.get("path"),
            "content": arguments.get("content", ""),
        }
        if arguments.get("expected_sha256"):
            operation["expected_sha256"] = arguments["expected_sha256"]
        return {"operations": [operation]}
    if raw_name == "edit_file":
        return {
            "operations": [
                {
                    "type": "replace",
                    "path": arguments.get("path"),
                    "old": arguments.get("old_string", ""),
                    "new": arguments.get("new_string", ""),
                    "replace_all": bool(arguments.get("replace_all")),
                    **(
                        {"expected_sha256": arguments["expected_sha256"]}
                        if arguments.get("expected_sha256")
                        else {}
                    ),
                }
            ]
        }
    if raw_name == "apply_patch":
        patch_text = (
            arguments.get("patch_text")
            or arguments.get("patch")
            or str(raw or "")
        )
        return {
            "operations": [{"type": "structured_patch", "patch_text": patch_text}],
            **(
                {"expected_sha256": arguments["expected_sha256"]}
                if arguments.get("expected_sha256")
                else {}
            ),
        }
    return arguments


def _adapt_command(raw: Any, raw_name: str) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        arguments = dict(raw)
        command = arguments.get("command") or arguments.get("cmd") or arguments.get("code")
        if command is not None:
            arguments["command"] = str(command)
        arguments.pop("cmd", None)
        arguments.pop("code", None)
        return arguments
    text = str(raw or "")
    if text.lstrip().startswith("{"):
        return _adapt_command(_json_object(text), raw_name)
    return {"command": text}


def _adapt_python(raw: Any, raw_name: str) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        arguments = dict(raw)
        if "code" not in arguments and "command" in arguments:
            arguments["code"] = arguments.pop("command")
        return arguments
    text = str(raw or "")
    if text.lstrip().startswith("{"):
        return _adapt_python(_json_object(text), raw_name)
    return {"code": text}


def _empty_effects(
    arguments: Mapping[str, Any], context: AgentExecutionContext
) -> tuple[Effect, ...]:
    return ()


def _root_read_effects(
    arguments: Mapping[str, Any], context: AgentExecutionContext
) -> tuple[Effect, ...]:
    return (
        Effect(
            "filesystem.read",
            context.execution_root.path,
            Capability.WORKSPACE_READ,
        ),
    )


def _success(
    name: str,
    data: Mapping[str, Any],
    *,
    backend: str,
    duration_ms: float,
    artifacts=(),
    continuation: Optional[Mapping[str, Any]] = None,
    truncation: Optional[Mapping[str, Any]] = None,
) -> ToolResult:
    return ToolResult(
        call_id="pending",
        canonical_name=name,
        status=ToolResultStatus.SUCCESS,
        data=data,
        artifacts=tuple(artifacts),
        continuation=continuation,
        truncation=truncation,
        backend=backend,
        duration_ms=duration_ms,
    )


def _failure(
    name: str,
    code: str,
    message: str,
    *,
    backend: str,
    duration_ms: float,
    data: Optional[Mapping[str, Any]] = None,
    timed_out: bool = False,
) -> ToolResult:
    return ToolResult(
        call_id="pending",
        canonical_name=name,
        status=(ToolResultStatus.TIMED_OUT if timed_out else ToolResultStatus.ERROR),
        data=data or {},
        error=ToolError(code, message),
        backend=backend,
        duration_ms=duration_ms,
    )


async def _ask_user(arguments: Mapping[str, Any], context: AgentExecutionContext) -> ToolResult:
    started = time.perf_counter()
    options = [
        {
            "label": str(item.get("label") or "").strip(),
            "description": str(item.get("description") or "").strip(),
        }
        for item in arguments.get("options") or []
        if isinstance(item, Mapping) and str(item.get("label") or "").strip()
    ][:6]
    payload = {
        "question": str(arguments.get("question") or "").strip(),
        "options": options,
        "multi": bool(arguments.get("multi")),
    }
    labels = "\n".join(f"- {item['label']}" for item in options)
    return _success(
        "ask_user",
        {
            "ask_user": payload,
            "text": (
                f"{payload['question']}\n{labels}\nAwaiting the user's selection."
            ).strip(),
        },
        backend="interaction",
        duration_ms=(time.perf_counter() - started) * 1000,
    )


async def _plan(arguments: Mapping[str, Any], context: AgentExecutionContext) -> ToolResult:
    from src.agent_tools.planning_tools import ManagePlanTool

    started = time.perf_counter()
    raw_result = await ManagePlanTool().execute(
        json.dumps(dict(arguments)),
        {"owner": context.owner_id, "session_id": context.session_id},
    )
    if isinstance(raw_result, tuple):
        raw_result = raw_result[-1]
    if not isinstance(raw_result, Mapping):
        return _failure(
            "plan",
            "invalid_plan_result",
            "plan backend returned an invalid result",
            backend="plan_service",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    if raw_result.get("error"):
        return _failure(
            "plan",
            str(raw_result.get("error_type") or "plan_error"),
            str(raw_result["error"]),
            backend="plan_service",
            duration_ms=(time.perf_counter() - started) * 1000,
            data=dict(raw_result),
        )
    data = dict(raw_result)
    plan = data.get("plan")
    if isinstance(plan, Mapping) and plan.get("steps"):
        markdown = "\n".join(
            f"- [{'x' if step.get('status') == 'completed' else ' '}] {step.get('content', '')}"
            for step in plan["steps"]
        )
        data["plan_update"] = {"plan": markdown}
    data.setdefault("text", str(data.get("output") or "Plan updated."))
    return _success(
        "plan",
        data,
        backend="plan_service",
        duration_ms=(time.perf_counter() - started) * 1000,
    )


async def _workspace_context(arguments: Mapping[str, Any], context: AgentExecutionContext) -> ToolResult:
    return _success(
        "workspace_context",
        {
            "path": context.execution_root.path,
            "source": context.execution_root.source.value,
            "writable": context.execution_root.writable,
            "workspace_revision": WORKSPACE_SERVICE.revision(context.execution_root.path),
            "execution_mode": context.execution_mode.value,
            "text": (
                f"Execution root: {context.execution_root.path} "
                f"({context.execution_root.source.value}, {context.execution_mode.value})"
            ),
        },
        backend="workspace_service",
        duration_ms=0.0,
    )


async def _find_files(arguments: Mapping[str, Any], context: AgentExecutionContext) -> ToolResult:
    started = time.perf_counter()
    try:
        data = WORKSPACE_SERVICE.find_files(context.execution_root.path, arguments)
    except WorkspaceError as exc:
        return _failure(
            "find_files", exc.code, str(exc), backend="workspace_service",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    data["text"] = "\n".join(item["path"] for item in data["files"]) or "(no files)"
    return _success(
        "find_files", data, backend=str(data["backend"]),
        duration_ms=(time.perf_counter() - started) * 1000,
        continuation=data.get("continuation"),
    )


async def _search_text(arguments: Mapping[str, Any], context: AgentExecutionContext) -> ToolResult:
    started = time.perf_counter()
    try:
        data = WORKSPACE_SERVICE.search_text(context.execution_root.path, arguments)
    except (WorkspaceError, ValueError) as exc:
        return _failure(
            "search_text", getattr(exc, "code", "search_error"), str(exc),
            backend="workspace_service", duration_ms=(time.perf_counter() - started) * 1000,
        )
    data["text"] = "\n".join(
        f"{item['path']}:{item['line']}:{item['column']}:{item['text']}"
        for item in data["matches"]
    ) or "(no matches)"
    return _success(
        "search_text", data, backend=str(data["backend"]),
        duration_ms=(time.perf_counter() - started) * 1000,
        continuation=data.get("continuation"),
    )


async def _read_files(arguments: Mapping[str, Any], context: AgentExecutionContext) -> ToolResult:
    started = time.perf_counter()
    try:
        data = WORKSPACE_SERVICE.read_files(context.execution_root.path, arguments)
    except (WorkspaceError, ValueError) as exc:
        return _failure(
            "read_files", getattr(exc, "code", "read_error"), str(exc),
            backend="workspace_service", duration_ms=(time.perf_counter() - started) * 1000,
        )
    return _success(
        "read_files", data, backend=str(data["backend"]),
        duration_ms=(time.perf_counter() - started) * 1000,
        continuation=data.get("continuation"),
        truncation={"truncated": bool(data.get("continuation")), "output_chars": data["output_chars"]},
    )


async def _patch_workspace(arguments: Mapping[str, Any], context: AgentExecutionContext) -> ToolResult:
    started = time.perf_counter()
    try:
        data = WORKSPACE_SERVICE.patch_workspace(context.execution_root.path, arguments)
    except (WorkspaceError, ValueError, UnicodeError) as exc:
        return _failure(
            "patch_workspace", getattr(exc, "code", "patch_error"), str(exc),
            backend="workspace_service", duration_ms=(time.perf_counter() - started) * 1000,
        )
    artifacts = tuple(
        {
            "type": "workspace_diff",
            "path": item.get("path") or item.get("destination") or "",
            "diff": item.get("diff") or "",
        }
        for item in data.get("operations") or []
    )
    data["text"] = str(data.get("summary") or "Workspace patch complete.")
    return _success(
        "patch_workspace", data, backend="workspace_service",
        duration_ms=(time.perf_counter() - started) * 1000,
        artifacts=artifacts,
    )


async def _run_command_named(
    name: str,
    arguments: Mapping[str, Any],
    context: AgentExecutionContext,
    *,
    progress_cb=None,
    invocation_id: Optional[str] = None,
) -> ToolResult:
    started = time.perf_counter()
    command = str(arguments.get("command") or "")
    lines = command.splitlines()
    if lines and lines[0].strip().casefold() in {"#!bg", "# bg", "# background", "#background"}:
        background_command = "\n".join(lines[1:]).strip()
        try:
            record = PROCESS_SERVICE.launch_background(background_command, context)
        except (ProcessServiceError, RuntimeError, ValueError) as exc:
            return _failure(
                name, "background_launch_error", str(exc), backend="background_job",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        return _success(
            name,
            {
                "text": f"Started background job {record['id']}.",
                "job_id": record["id"],
                "execution_mode": context.execution_mode.value,
                "execution_root": context.execution_root.path,
            },
            backend="background_job",
            duration_ms=(time.perf_counter() - started) * 1000,
            artifacts=({"type": "background_job", "id": record["id"]},),
        )
    try:
        data = await PROCESS_SERVICE.run_command(
            arguments,
            context,
            progress_cb=progress_cb,
            invocation_id=invocation_id,
        )
    except (ProcessSandboxUnavailable, ProcessServiceError) as exc:
        return _failure(
            name, exc.code, str(exc), backend="process_service",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    duration = (time.perf_counter() - started) * 1000
    if data.get("timed_out"):
        return _failure(
            name, "command_timeout", f"command timed out after {data['timeout_seconds']:g}s",
            backend=str(data["backend"]), duration_ms=duration, data=data, timed_out=True,
        )
    if int(data.get("exit_code") or 0) != 0:
        return _failure(
            name, "process_exit_nonzero", f"command exited with status {data['exit_code']}",
            backend=str(data["backend"]), duration_ms=duration, data=data,
        )
    return _success(
        name, data, backend=str(data["backend"]), duration_ms=duration,
        truncation=data.get("truncation"),
    )


async def _run_sandbox(
    arguments: Mapping[str, Any],
    context: AgentExecutionContext,
    *,
    progress_cb=None,
    invocation_id: Optional[str] = None,
) -> ToolResult:
    return await _run_command_named(
        "run_sandbox_command",
        arguments,
        context,
        progress_cb=progress_cb,
        invocation_id=invocation_id,
    )


async def _run_host(
    arguments: Mapping[str, Any],
    context: AgentExecutionContext,
    *,
    progress_cb=None,
    invocation_id: Optional[str] = None,
) -> ToolResult:
    return await _run_command_named(
        "run_host_command",
        arguments,
        context,
        progress_cb=progress_cb,
        invocation_id=invocation_id,
    )


async def _run_python(
    arguments: Mapping[str, Any],
    context: AgentExecutionContext,
    *,
    progress_cb=None,
    invocation_id: Optional[str] = None,
) -> ToolResult:
    started = time.perf_counter()
    try:
        data = await PROCESS_SERVICE.run_python(
            arguments,
            context,
            progress_cb=progress_cb,
        )
    except (ProcessSandboxUnavailable, ProcessServiceError) as exc:
        return _failure(
            "run_python", exc.code, str(exc), backend="process_service",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    duration = (time.perf_counter() - started) * 1000
    if data.get("timed_out"):
        return _failure(
            "run_python", "python_timeout", f"Python timed out after {data['timeout_seconds']:g}s",
            backend=str(data["backend"]), duration_ms=duration, data=data, timed_out=True,
        )
    if int(data.get("exit_code") or 0) != 0:
        return _failure(
            "run_python", "python_exception", str(data.get("stderr") or "Python execution failed"),
            backend=str(data["backend"]), duration_ms=duration, data=data,
        )
    return _success(
        "run_python", data, backend=str(data["backend"]), duration_ms=duration,
        truncation=data.get("truncation"),
    )


def _expose_sandbox(context: Optional[AgentExecutionContext]) -> bool:
    return context is None or (
        context.execution_mode is ExecutionMode.SANDBOXED
        and context.authority_grant.allows(Capability.PROCESS_SANDBOX)
    )


def _expose_host(context: Optional[AgentExecutionContext]) -> bool:
    return bool(
        context is not None
        and context.execution_mode is ExecutionMode.HOST
        and context.authority_grant.host_authorization_id
        and context.authority_grant.allows(Capability.PROCESS_HOST)
    )


def _expose_python(context: Optional[AgentExecutionContext]) -> bool:
    return context is None or bool(
        context.execution_mode.enabled
        and (
            context.authority_grant.allows(Capability.PROCESS_SANDBOX)
            or context.authority_grant.allows(Capability.PROCESS_HOST)
        )
    )


def _definition(
    *,
    name: str,
    description: str,
    schema: Mapping[str, Any],
    handler,
    category: ToolCategory,
    risk: ToolRisk,
    aliases=(),
    required=frozenset(),
    effects=_empty_effects,
    approval=DefinitionApprovalPolicy.CAPABILITY_ONLY,
    exposure=None,
    adapter=_json_object,
    mutates=False,
    destructive=False,
    progress=False,
    timeout=30.0,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=schema,
        handler=handler,
        category=category,
        risk=risk,
        autonomy=(
            ToolAutonomy.CONFIRM_EVERY_CALL
            if approval is DefinitionApprovalPolicy.ALWAYS_APPROVE
            else ToolAutonomy.AUTONOMOUS
        ),
        idempotency=(ToolIdempotency.NON_IDEMPOTENT if mutates else ToolIdempotency.IDEMPOTENT),
        aliases=tuple(aliases),
        foundational_for=frozenset(
            {"loop_primitive"} if name in {"ask_user", "plan"} else
            {"shell"} if name in {"run_sandbox_command", "run_host_command", "run_python"} else
            {"workspace"}
        ),
        requires_workspace=name not in {"ask_user", "plan"},
        requires_shell_enabled=name in {"run_sandbox_command", "run_host_command", "run_python"},
        mutates_state=mutates,
        destructive=destructive,
        supports_progress=progress,
        result_schema=_RESULT_SCHEMA,
        frontend_event_types=("tool_started", "tool_result"),
        required_capabilities=frozenset(required),
        effect_resolver=effects,
        approval_policy=approval,
        exposure_policy=exposure,
        timeout_seconds=timeout,
        argument_adapter=adapter,
        runtime_v2=True,
    )


def build_runtime_v2_definitions() -> dict[str, ToolDefinition]:
    object_schema = lambda properties, required=(): {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }
    pagination = {
        "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
        "continuation": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0, "description": "Deprecated alias pagination offset"},
        "expected_workspace_revision": {"type": "string"},
    }
    command_schema = object_schema(
        {
            "command": {"type": "string", "minLength": 1},
            "timeout_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 1800,
                "default": 120,
            },
        },
        ("command",),
    )
    definitions = [
        _definition(
            name="ask_user",
            description="Ask the user a structured question and wait for their response.",
            schema=object_schema(
                {
                    "question": {"type": "string", "minLength": 1},
                    "options": {"type": "array", "minItems": 2, "maxItems": 6, "items": object_schema({"label": {"type": "string", "minLength": 1}, "description": {"type": "string"}}, ("label",))},
                    "multi": {"type": "boolean"},
                },
                ("question", "options"),
            ),
            handler=_ask_user,
            category=ToolCategory.USER_INTERACTION,
            risk=ToolRisk.READ_ONLY,
            adapter=_adapt_ask_user,
        ),
        _definition(
            name="plan",
            description="Create, read, or update the run-scoped structured plan.",
            schema=object_schema(
                {
                    "action": {"type": "string", "enum": ["create", "read", "replace", "add_step", "update_step", "complete_step", "block_step", "skip_step", "remove_step", "reorder", "clear", "archive"]},
                    "steps": {"type": "array", "items": {"type": "object"}},
                    "title": {"type": "string"}, "summary": {"type": "string"},
                    "content": {"type": "string"}, "step_id": {"type": "string"},
                    "step_ids": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string"}, "priority": {"type": "string"},
                    "notes": {"type": "string"},
                    "evidence": {"type": ["string", "object"]},
                    "expected_version": {"type": "integer"},
                },
                ("action",),
            ),
            handler=_plan,
            category=ToolCategory.PLANNING,
            risk=ToolRisk.LOCAL_WRITE,
            aliases=("update_plan", "manage_plan", "todowrite"),
            adapter=_adapt_plan,
            mutates=True,
        ),
        _definition(
            name="workspace_context",
            description="Return the exact execution root, source, writability, revision, and bound execution mode.",
            schema=object_schema({}), handler=_workspace_context,
            category=ToolCategory.INSPECTION, risk=ToolRisk.READ_ONLY,
            aliases=("get_workspace",), required={Capability.WORKSPACE_READ},
            effects=_root_read_effects, adapter=_adapt_empty,
        ),
        _definition(
            name="find_files",
            description="Find workspace files by canonical relative path with stable continuation.",
            schema=object_schema({
                "path": {"type": "string"},
                "patterns": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                **pagination,
            }), handler=_find_files, category=ToolCategory.SEARCH, risk=ToolRisk.READ_ONLY,
            aliases=("glob", "ls"), required={Capability.WORKSPACE_READ},
            effects=resolve_workspace_read_effects, adapter=_adapt_find,
        ),
        _definition(
            name="search_text",
            description="Search workspace text with ripgrep or a disclosed Python fallback and stable continuation.",
            schema=object_schema({
                "pattern": {"type": "string", "minLength": 1},
                "path": {"type": "string"}, "fixed_string": {"type": "boolean"},
                "case_sensitive": {"type": "boolean"},
                "include": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 20},
                **pagination,
            }, ("pattern",)), handler=_search_text, category=ToolCategory.SEARCH, risk=ToolRisk.READ_ONLY,
            aliases=("grep", "rg"), required={Capability.WORKSPACE_READ},
            effects=resolve_workspace_read_effects, adapter=_adapt_search,
        ),
        _definition(
            name="read_files",
            description="Read one or more line ranges with per-file errors and one total output budget.",
            schema=object_schema({
                "requests": {"type": "array", "minItems": 1, "items": object_schema({
                    "path": {"type": "string", "minLength": 1},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "expected_sha256": {"type": "string"},
                }, ("path",))},
                "max_output_chars": {"type": "integer", "minimum": 1, "maximum": 200000},
                "expected_workspace_revision": {"type": "string"},
            }, ("requests",)), handler=_read_files, category=ToolCategory.INSPECTION, risk=ToolRisk.READ_ONLY,
            aliases=("read_file",), required={Capability.WORKSPACE_READ},
            effects=resolve_workspace_read_effects, adapter=_adapt_read,
        ),
        _definition(
            name="patch_workspace",
            description="Preview or commit journaled staged workspace creates, exact replacements, structured patches, moves, and approved deletes.",
            schema=object_schema({
                "operations": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["type"], "properties": {
                    "type": {"enum": ["create", "write", "replace", "structured_patch", "move", "delete"]},
                    "path": {"type": "string"}, "source": {"type": "string"}, "destination": {"type": "string"},
                    "content": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                    "replace_all": {"type": "boolean"}, "patch_text": {"type": "string"},
                    "expected_sha256": {"type": "string"},
                }}},
                "expected_workspace_revision": {"type": "string"},
                "expected_sha256": {"type": "object", "additionalProperties": {"type": "string"}},
                "dry_run": {"type": "boolean"},
            }, ("operations",)), handler=_patch_workspace, category=ToolCategory.EDITING, risk=ToolRisk.LOCAL_WRITE,
            aliases=("write_file", "edit_file", "apply_patch"),
            required={Capability.WORKSPACE_WRITE}, effects=resolve_workspace_patch_effects,
            approval=DefinitionApprovalPolicy.SENSITIVE_EFFECTS, adapter=_adapt_patch,
            mutates=True,
        ),
        _definition(
            name="run_sandbox_command",
            description="Run a command in the bound Bubblewrap workspace with no network, a sanitized environment, resource limits, cancellation, and no host fallback.",
            schema=command_schema, handler=_run_sandbox, category=ToolCategory.EXECUTION, risk=ToolRisk.PRIVILEGED,
            aliases=("bash",), required={Capability.PROCESS_SANDBOX}, effects=resolve_command_effects,
            approval=DefinitionApprovalPolicy.SENSITIVE_EFFECTS, exposure=_expose_sandbox,
            adapter=_adapt_command, mutates=True, progress=True, timeout=1800,
        ),
        _definition(
            name="run_host_command",
            description="Run a justified command at the server-bound host root. Opaque or sensitive effects require additional approval.",
            schema=command_schema, handler=_run_host, category=ToolCategory.EXECUTION, risk=ToolRisk.PRIVILEGED,
            required={Capability.PROCESS_HOST}, effects=resolve_command_effects,
            approval=DefinitionApprovalPolicy.SENSITIVE_EFFECTS, exposure=_expose_host,
            adapter=_adapt_command, mutates=True, progress=True, timeout=1800,
        ),
        _definition(
            name="run_python",
            description="Execute Python directly through the run-bound ProcessService without shell parsing.",
            schema=object_schema({
                "code": {"type": "string", "minLength": 1},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 3600},
            }, ("code",)), handler=_run_python, category=ToolCategory.EXECUTION, risk=ToolRisk.PRIVILEGED,
            aliases=("python",), effects=resolve_python_effects,
            approval=DefinitionApprovalPolicy.SENSITIVE_EFFECTS, exposure=_expose_python,
            adapter=_adapt_python, mutates=True, progress=True, timeout=3600,
        ),
    ]
    return {definition.name: definition for definition in definitions}


MIGRATED_CANONICAL_NAMES = frozenset(
    {
        "ask_user", "plan", "workspace_context", "find_files", "search_text",
        "read_files", "patch_workspace", "run_sandbox_command", "run_host_command", "run_python",
    }
)
MIGRATED_LEGACY_NAMES = frozenset(
    {
        "get_workspace", "glob", "ls", "grep", "rg", "read_file", "write_file",
        "edit_file", "apply_patch", "update_plan", "manage_plan", "todowrite", "bash", "python",
    }
)
