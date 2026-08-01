"""Coverage for the concise shell-literacy / action-commitment prompt
fragment (src/agent/prompting/contexts/shell_guidance.py). Combined with
the pure routing rules in src/agent_loop.py's _domain_rules_with_shell_
guidance (not inside src/agent/routing/tool_domains.py itself — routing is
a lower architectural layer than prompting, see
test_agent_package_respects_dependency_boundaries in
tests/test_agent_architecture.py), so it is appended only when a canonical process tool is
in this turn's effective tool set.
"""

from src.agent.prompting.contexts.shell_guidance import (
    HOST_SHELL_AUTHORITY,
    SANDBOX_SHELL_AUTHORITY,
    SHELL_GUIDANCE,
    execution_authority_guidance,
    shell_guidance_if_available,
)
from src.agent.routing.tool_domains import domain_rules_for_tools
from src.agent_loop import _domain_rules_with_shell_guidance


def test_guidance_present_only_when_process_tool_available():
    without_bash = _domain_rules_with_shell_guidance({"read_files", "search_text", "find_files"})
    with_bash = _domain_rules_with_shell_guidance({"read_files", "search_text", "run_sandbox_command"})

    assert not any("Shell rules" in rule for rule in without_bash)
    assert any("Shell rules" in rule for rule in with_bash)


def test_routing_layer_itself_never_includes_shell_guidance():
    # Routing must stay a pure, lower-layer module — the prompt-text
    # fragment is combined in only by the agent_loop facade.
    rules = domain_rules_for_tools({"run_sandbox_command", "read_files"})
    assert not any("Shell rules" in rule for rule in rules)


def test_helper_mirrors_domain_rules_gate():
    assert shell_guidance_if_available({"run_sandbox_command"}) == SHELL_GUIDANCE
    assert shell_guidance_if_available({"read_files"}) is None
    assert shell_guidance_if_available(set()) is None
    assert shell_guidance_if_available(None) is None


def test_guidance_describes_the_enforced_process_sandbox():
    assert "isolated Linux namespace" in SANDBOX_SHELL_AUTHORITY
    assert "no network" in SANDBOX_SHELL_AUTHORITY
    assert "read-only host root" in SANDBOX_SHELL_AUTHORITY
    assert "only writable root" in SANDBOX_SHELL_AUTHORITY


def test_authority_guidance_distinguishes_host_from_sandbox():
    assert execution_authority_guidance("sandboxed") == SANDBOX_SHELL_AUTHORITY
    assert execution_authority_guidance("host") == HOST_SHELL_AUTHORITY
    assert execution_authority_guidance("disabled") is None
    assert "exact ExecutionRoot" in HOST_SHELL_AUTHORITY
    assert "not a sandbox" in HOST_SHELL_AUTHORITY


def test_guidance_states_action_commitment_rule():
    lowered = SHELL_GUIDANCE.lower()
    assert "emit the tool call immediately" in lowered
    assert "once you decide to act" in lowered


def test_guidance_covers_find_and_sed_with_safety_notes():
    assert "**find**" in SHELL_GUIDANCE
    assert "-print0" in SHELL_GUIDANCE
    assert "-delete" in SHELL_GUIDANCE
    assert "**sed**" in SHELL_GUIDANCE
    assert "sed -n" in SHELL_GUIDANCE
    assert "bounded read-only inspection" in SHELL_GUIDANCE


def test_guidance_covers_rg_jq_xargs_git():
    assert "**rg/grep**" in SHELL_GUIDANCE
    assert "**jq**" in SHELL_GUIDANCE
    assert "**xargs**" in SHELL_GUIDANCE
    assert "**git**" in SHELL_GUIDANCE
    for forbidden in ("reset", "restore", "clean", "rebase", "merge", "commit", "push"):
        assert forbidden in SHELL_GUIDANCE


def test_guidance_prohibits_destructive_git_and_secrets():
    assert "explicit authorization" in SHELL_GUIDANCE or "explicitly authorized" in SHELL_GUIDANCE
    assert "passwords" in SHELL_GUIDANCE.lower()


def test_guidance_size_is_bounded():
    # A concise fragment for routine inclusion — not a full manual. Bounded
    # generously above the current length so implementation tweaks don't
    # make this brittle, while still catching runaway growth.
    assert len(SHELL_GUIDANCE) < 4000


def test_guidance_never_advertises_a_tool_outside_the_turns_set():
    # The fragment itself only names tools that are foundational/always
    # legitimate to mention (bash, read_file, grep, glob,
    # edit_file, apply_patch) rather than any domain-specific tool that
    # could be absent this turn.
    advertised_tools = {
        "read_files",
        "search_text",
        "find_files",
        "patch_workspace",
    }
    for name in advertised_tools:
        assert f"`{name}`" in SHELL_GUIDANCE


def test_rule_block_appended_after_domain_rules_preserving_order():
    # Canonical process tools live in the "files" domain, so its generic file-tool rule
    # block must still appear, with the dedicated shell fragment appended
    # after it — never replacing or reordering the existing domain rules.
    rules = _domain_rules_with_shell_guidance({"run_sandbox_command", "read_files"})
    from src.agent.routing.tool_domains import DOMAIN_RULES

    files_idx = rules.index(DOMAIN_RULES["files"])
    shell_idx = next(i for i, r in enumerate(rules) if "Shell rules" in r)
    assert files_idx < shell_idx
