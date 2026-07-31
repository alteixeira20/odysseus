"""Prepare the exact provider tool-schema pack for one agent round."""

from dataclasses import dataclass
import json
from typing import Iterable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class PreparedToolSchemas:
    schemas: tuple[Mapping, ...]
    names: tuple[Optional[str], ...]
    encoded_bytes: int

    def as_provider_list(self) -> list[Mapping]:
        return list(self.schemas)


def prepare_tool_schemas(
    *,
    force_answer: bool,
    is_api_model: bool,
    relevant_tools: Optional[set[str]],
    function_schemas: Sequence[Mapping],
    mcp_schemas: Sequence[Mapping],
    odysseus_qwen_finetune: bool,
    disabled_tools: Optional[set[str]],
    latest_user_text: str,
    mcp_keywords: Iterable[str],
) -> PreparedToolSchemas:
    """Return the behavior-compatible schema selection for one round."""

    if force_answer:
        schemas: list[Mapping] = []
    elif is_api_model:
        if relevant_tools is not None:
            schema_names = set(relevant_tools)
            schemas = [
                schema
                for schema in function_schemas
                if schema.get("function", {}).get("name") in schema_names
            ]
            schemas.extend(
                schema
                for schema in mcp_schemas
                if schema.get("function", {}).get("name") in relevant_tools
            )
        else:
            schemas = []
        if odysseus_qwen_finetune:
            schemas = []
        if disabled_tools:
            schemas = [
                schema
                for schema in schemas
                if schema.get("function", {}).get("name")
                not in disabled_tools
                and schema.get("name") not in disabled_tools
            ]
    else:
        lowered = (latest_user_text or "").lower()
        wants_mcp = any(keyword in lowered for keyword in mcp_keywords)
        schemas = list(mcp_schemas) if wants_mcp and mcp_schemas else []

    names = tuple(
        schema.get("function", {}).get("name")
        for schema in schemas
        if schema.get("function")
    )
    encoded_bytes = len(
        json.dumps(schemas, separators=(",", ":")).encode("utf-8")
    )
    return PreparedToolSchemas(
        schemas=tuple(schemas),
        names=names,
        encoded_bytes=encoded_bytes,
    )
