from pathlib import Path

path = Path("scripts/runtime_v3_mcp_deployment_patch.py")
text = path.read_text(encoding="utf-8")

old = '''replace_once(
    "setup.py",
    ''' + "'''def configure_environment():\n'''" + ''',
    ''' + "'''def install_node_runtime():" + '''
'''
new = '''replace_once(
    "setup.py",
    ''' + "'''def main():\n'''" + ''',
    ''' + "'''def install_node_runtime():" + '''
'''
if text.count(old) != 1:
    raise RuntimeError("setup function insertion anchor in deployment patch changed")
text = text.replace(old, new, 1)

old = '''\n\ndef configure_environment():\n''' + "'''," + '''
)'''
new = '''\n\ndef main():\n''' + "'''," + '''
)'''
if text.count(old) != 1:
    raise RuntimeError("setup function continuation anchor in deployment patch changed")
text = text.replace(old, new, 1)

old = '''replace_once(
    "setup.py",
    ''' + "'''    # Create necessary directories\n    create_directories()\n\n    # Configure environment\n'''" + ''',
    ''' + "'''    # Create necessary directories\n    create_directories()\n\n    # Install the exact Node MCP runtime. This is setup-time network access;\n    # application startup never downloads executable code.\n    install_node_runtime()\n\n    # Configure environment\n'''" + ''',
)'''
new = '''replace_once(
    "setup.py",
    ''' + "'''    print(\"1. Creating directories...\")\n    create_dirs()\n\n    print(\"\\n2. Environment file...\")\n'''" + ''',
    ''' + "'''    print(\"1. Creating directories...\")\n    create_dirs()\n\n    print(\"\\n2. Installing lockfile-pinned browser MCP runtime...\")\n    install_node_runtime()\n\n    print(\"\\n3. Environment file...\")\n'''" + ''',
)
replace_once("setup.py", 'print("\\n3. Checking dependencies...")', 'print("\\n4. Checking dependencies...")')
replace_once("setup.py", 'print("\\n4. Initializing database...")', 'print("\\n5. Initializing database...")')
replace_once("setup.py", 'print("\\n5. Creating initial admin...")', 'print("\\n6. Creating initial admin...")')'''
if text.count(old) != 1:
    raise RuntimeError("setup main-flow anchor in deployment patch changed")
text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
print("Runtime V3 MCP deployment setup anchors corrected")
