from src import agent_loop
from src.agent.prompting.contexts.uploads import (
    uploaded_files_context_message,
)
from src.agent.prompting.contexts.workspace import (
    local_computer_rules,
    looks_like_local_computer_request,
    looks_like_workspace_coding_request,
    workspace_coding_rules,
)


def test_legacy_prompt_context_helpers_are_aliases():
    assert (
        agent_loop._uploaded_files_context_message
        is uploaded_files_context_message
    )
    assert (
        agent_loop._looks_like_workspace_coding_request
        is looks_like_workspace_coding_request
    )
    assert (
        agent_loop._looks_like_local_computer_request
        is looks_like_local_computer_request
    )
    assert agent_loop._local_computer_rules is local_computer_rules
    assert agent_loop._workspace_coding_rules is workspace_coding_rules


def test_upload_manifest_is_bounded_and_marks_context_untrusted():
    uploads = [
        {"id": str(index), "name": f"file-{index}.txt"}
        for index in range(22)
    ]

    message = uploaded_files_context_message(uploads)

    assert message["role"] == "user"
    assert "file-0.txt" in message["content"]
    assert "file-20.txt" not in message["content"]
    assert "2 more upload(s)" in message["content"]


def test_workspace_rules_only_advertise_available_tools():
    rules = workspace_coding_rules(
        "/tmp/repo",
        {"workspace_context", "search_text", "patch_workspace", "run_sandbox_command"},
    )

    assert "`/tmp/repo`" in rules
    assert "`search_text`" in rules
    assert "`patch_workspace`" in rules
    assert "call `plan`" not in rules
