from pathlib import Path

path = Path("static/js/chat.js")
text = path.read_text(encoding="utf-8")
old = "cutoff.slice(-500)';"
new = "cutoff.slice(-500);"
count = text.count(old)
if count != 2:
    raise RuntimeError(f"expected two generated recovery-expression anchors, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
print("Runtime V3 post-patch JavaScript fix applied")
