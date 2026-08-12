from pathlib import Path

path = Path("tests/test_agent_loop.py")
text = path.read_text(encoding="utf-8")
needle = "    'src.agent_tools',\n"
if text.count(needle) != 1:
    raise SystemExit(f"expected exactly one stale src.agent_tools mock, found {text.count(needle)}")
replacement = (
    "    # Keep src.agent_tools real: Runtime V2 imports package submodules such as\n"
    "    # src.agent_tools.subprocess_tools while src.agent_loop is imported.\n"
)
path.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
