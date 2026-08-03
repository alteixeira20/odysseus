from pathlib import Path

path = Path("src/agent/runtime_v3/ledger.py")
text = path.read_text(encoding="utf-8")
old = '''        limits = load_runtime_v3_limits()
        now = time.time()
'''
new = '''        limits = load_runtime_v3_limits()
        if len(encoded) > limits.max_replay_bytes:
            raise ValueError(
                f"event payload exceeds total replay byte budget {limits.max_replay_bytes}"
            )
        now = time.time()
'''
if text.count(old) != 1:
    raise RuntimeError("generated replay-retention anchor changed")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Single events larger than total durable replay budget are now rejected")
