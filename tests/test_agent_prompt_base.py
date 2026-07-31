from src import agent_loop
from src.agent.prompting import base


def test_legacy_prompt_constants_reexport_static_base_text():
    assert agent_loop._AGENT_PREAMBLE == base.AGENT_PREAMBLE
    assert agent_loop._AGENT_RULES == base.AGENT_RULES
    assert agent_loop._API_AGENT_RULES == base.API_AGENT_RULES


def test_legacy_prompt_override_remains_runtime_patchable(monkeypatch):
    monkeypatch.setattr(
        agent_loop,
        "get_builtin_overrides",
        lambda: {
            "bash": "```bash\n<command>\n```\nOVERRIDDEN BASH SECTION"
        },
    )

    prompt = agent_loop._assemble_prompt({"bash", "python"})

    assert "OVERRIDDEN BASH SECTION" in prompt
    assert agent_loop.TOOL_SECTIONS["python"] in prompt
    assert agent_loop.TOOL_SECTIONS["bash"] not in prompt
