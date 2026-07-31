from src.agent.prompting.builder import assemble_prompt_messages


def test_agent_prompt_merges_after_initial_trusted_system_messages():
    original = [
        {"role": "system", "content": "application policy"},
        {"role": "user", "content": "hello"},
    ]

    assembled = assemble_prompt_messages(
        original,
        agent_prompt="agent tools",
    )

    assert assembled == [
        {
            "role": "system",
            "content": "application policy\n\nagent tools",
        },
        {"role": "user", "content": "hello"},
    ]
    assert original[0]["content"] == "application policy"


def test_protected_system_message_keeps_its_sequence_boundary():
    protected = {
        "role": "system",
        "content": "protected",
        "_protected": True,
    }

    assembled = assemble_prompt_messages(
        [protected, {"role": "user", "content": "hello"}],
        agent_prompt="agent tools",
    )

    assert assembled[:2] == [
        protected,
        {"role": "system", "content": "agent tools"},
    ]


def test_context_groups_keep_order_immediately_before_latest_user():
    document = {"role": "user", "content": "document"}
    email = {"role": "user", "content": "email"}
    skills = {"role": "user", "content": "skills"}

    assembled = assemble_prompt_messages(
        [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "latest"},
        ],
        agent_prompt="agent tools",
        context_messages=(document, None, email, skills),
    )

    assert [message["content"] for message in assembled] == [
        "agent tools",
        "earlier",
        "answer",
        "document",
        "email",
        "skills",
        "latest",
    ]
