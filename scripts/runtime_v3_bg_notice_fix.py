from pathlib import Path

path = Path("src/bg_monitor.py")
text = path.read_text(encoding="utf-8")
old = '''    async def notice_source():
        yield 'data: {"type":"run_state","state":"completed","terminal":true,"reason":"background_notice"}\\n\\n'
        yield "data: [DONE]\\n\\n"
'''
new = '''    async def notice_source():
        # Let the run manager construct the semantic terminal. A hand-authored
        # Runtime V2 event would lack run identity and sequence fields.
        yield 'data: {"delta":""}\\n\\n'
        yield "data: [DONE]\\n\\n"
'''
if text.count(old) != 1:
    raise RuntimeError("background notice source anchor changed")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Runtime V3 background notice protocol fix applied")
