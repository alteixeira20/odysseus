from pathlib import Path

FILES = (
    "scripts/postmerge_runtime_v3_fix.py",
    "tests/runtime_v3/test_mcp_deployment.py",
    "tests/runtime_v3/test_postmerge_audit.py",
)

for filename in FILES:
    path = Path(filename)
    text = path.read_text(encoding="utf-8")
    count = text.count("mcp-server-playwright")
    if count < 1:
        raise RuntimeError(f"{filename}: expected stale Playwright bin reference")
    path.write_text(text.replace("mcp-server-playwright", "playwright-mcp"), encoding="utf-8")

print("Aligned generated deployment and tests with @playwright/mcp bin=playwright-mcp")
