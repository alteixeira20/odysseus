from src.agent.rounds.schema_preparation import prepare_tool_schemas


def _schema(name):
    return {
        "type": "function",
        "function": {"name": name, "parameters": {}},
    }


def _prepare(**overrides):
    values = {
        "force_answer": False,
        "is_api_model": True,
        "relevant_tools": {"bash", "mcp__demo__read"},
        "function_schemas": [_schema("bash"), _schema("python")],
        "mcp_schemas": [_schema("mcp__demo__read")],
        "odysseus_qwen_finetune": False,
        "disabled_tools": set(),
        "latest_user_text": "",
        "mcp_keywords": ("mcp", "demo"),
    }
    values.update(overrides)
    return prepare_tool_schemas(**values)


def test_api_schema_pack_filters_builtin_and_mcp_by_retrieval():
    prepared = _prepare()

    assert prepared.names == ("bash", "mcp__demo__read")
    assert [item["function"]["name"] for item in prepared.schemas] == [
        "bash",
        "mcp__demo__read",
    ]
    assert prepared.encoded_bytes > 0


def test_none_relevant_tools_means_no_api_schemas():
    assert _prepare(relevant_tools=None).schemas == ()


def test_empty_relevant_tools_means_no_api_schemas():
    assert _prepare(relevant_tools=set()).schemas == ()


def test_force_answer_and_odysseus_qwen_send_no_schemas():
    assert _prepare(force_answer=True).schemas == ()
    assert _prepare(odysseus_qwen_finetune=True).schemas == ()


def test_disabled_tools_are_removed_from_api_pack():
    prepared = _prepare(disabled_tools={"bash"})

    assert prepared.names == ("mcp__demo__read",)


def test_local_models_receive_only_keyword_requested_mcp_schemas():
    absent = _prepare(
        is_api_model=False,
        latest_user_text="ordinary chat",
    )
    requested = _prepare(
        is_api_model=False,
        latest_user_text="use the demo server",
    )

    assert absent.schemas == ()
    assert requested.names == ("mcp__demo__read",)


def test_local_schema_behavior_enforces_disabled_tool_semantics():
    prepared = _prepare(
        is_api_model=False,
        latest_user_text="use mcp",
        disabled_tools={"mcp__demo__read"},
    )

    assert prepared.names == ()


def test_local_schema_explicit_activation_does_not_require_keyword_match():
    prepared = _prepare(
        is_api_model=False,
        latest_user_text="Use Serena",
        mcp_keywords=("mcp", "demo"),
        mcp_explicit_activation=True,
    )

    assert prepared.names == ("mcp__demo__read",)


def test_provider_schema_pack_deduplicates_names_with_local_precedence():
    local = _schema("bash")
    local["function"]["description"] = "local implementation"
    duplicate = _schema("bash")
    duplicate["function"]["description"] = "duplicate implementation"

    prepared = _prepare(
        relevant_tools={"bash"},
        function_schemas=[local, duplicate],
        mcp_schemas=[duplicate],
    )

    assert prepared.names == ("bash",)
    assert len(prepared.schemas) == 1
    assert prepared.schemas[0]["function"]["description"] == "local implementation"
