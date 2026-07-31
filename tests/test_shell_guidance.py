"""Coverage for the concise shell-literacy / action-commitment prompt
fragment (src/agent/prompting/contexts/shell_guidance.py). Combined with
the pure routing rules in src/agent_loop.py's _domain_rules_with_shell_
guidance (not inside src/agent/routing/tool_domains.py itself — routing is
a lower architectural layer than prompting, see
test_agent_package_respects_dependency_boundaries in
tests/test_agent_architecture.py), so it is appended only when `bash` is
in this turn's effective tool set.
"""

from src.agent.prompting.contexts.shell_guidance import (
    SHELL_GUIDANCE,
    shell_guidance_if_available,
)
from src.agent.routing.tool_domains import domain_rules_for_tools
from src.agent_loop import _domain_rules_with_shell_guidance


def test_guidance_present_only_when_bash_available():
    without_bash = _domain_rules_with_shell_guidance({"read_file", "grep", "glob"})
    with_bash = _domain_rules_with_shell_guidance({"read_file", "grep", "bash"})

    assert not any("Shell rules" in rule for rule in without_bash)
    assert any("Shell rules" in rule for rule in with_bash)


def test_routing_layer_itself_never_includes_shell_guidance():
    # Routing must stay a pure, lower-layer module — the prompt-text
    # fragment is combined in only by the agent_loop facade.
    rules = domain_rules_for_tools({"bash", "read_file"})
    assert not any("Shell rules" in rule for rule in rules)


def test_helper_mirrors_domain_rules_gate():
    assert shell_guidance_if_available({"bash"}) == SHELL_GUIDANCE
    assert shell_guidance_if_available({"read_file"}) is None
    assert shell_guidance_if_available(set()) is None
    assert shell_guidance_if_available(None) is None


def test_guidance_labels_host_shell_truthfully_not_a_sandbox():
    assert "NOT a sandbox" in SHELL_GUIDANCE
    assert "full host shell" in SHELL_GUIDANCE


def test_guidance_states_action_commitment_rule():
    lowered = SHELL_GUIDANCE.lower()
    assert "emit the tool call immediately" in lowered
    assert "never end a turn with" in lowered


def test_guidance_covers_find_and_sed_with_safety_notes():
    assert "**find**" in SHELL_GUIDANCE
    assert "-print0" in SHELL_GUIDANCE
    assert "-delete" in SHELL_GUIDANCE
    assert "**sed**" in SHELL_GUIDANCE
    assert "sed -n" in SHELL_GUIDANCE
    assert "blind tree-wide" in SHELL_GUIDANCE


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
    # legitimate to mention (bash, get_workspace, read_file, grep, glob,
    # edit_file, apply_patch) rather than any domain-specific tool that
    # could be absent this turn.
    advertised_tools = {
        "bash",
        "get_workspace",
        "read_file",
        "grep",
        "glob",
        "ls",
        "edit_file",
        "apply_patch",
    }
    for name in advertised_tools:
        assert f"`{name}`" in SHELL_GUIDANCE


def test_rule_block_appended_after_domain_rules_preserving_order():
    # bash lives in the "files" domain, so its generic file-tool rule
    # block must still appear, with the dedicated shell fragment appended
    # after it — never replacing or reordering the existing domain rules.
    rules = _domain_rules_with_shell_guidance({"bash", "read_file"})
    from src.agent.routing.tool_domains import DOMAIN_RULES

    files_idx = rules.index(DOMAIN_RULES["files"])
    shell_idx = next(i for i, r in enumerate(rules) if "Shell rules" in r)
    assert files_idx < shell_idx
