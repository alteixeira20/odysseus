from src import agent_loop
from src.agent.routing.tool_domains import (
    DOMAIN_RULES,
    DOMAIN_TOOL_MAP,
    WORKSPACE_TERMINUS_TOOLS,
    domain_rules_for_tools,
)


def test_every_domain_mapping_has_rules_and_legacy_identity():
    assert set(DOMAIN_TOOL_MAP) <= set(DOMAIN_RULES)
    assert agent_loop._DOMAIN_TOOL_MAP is DOMAIN_TOOL_MAP
    assert agent_loop._DOMAIN_RULES is DOMAIN_RULES
    assert agent_loop._WORKSPACE_TERMINUS_TOOLS is WORKSPACE_TERMINUS_TOOLS
    assert agent_loop._domain_rules_for_tools is domain_rules_for_tools


def test_rule_selection_preserves_domain_declaration_order():
    rules = domain_rules_for_tools({"api_call", "web_search"})

    assert rules == [DOMAIN_RULES["web"], DOMAIN_RULES["integrations"]]
