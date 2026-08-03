from pathlib import Path

path = Path("src/agent/runtime_v3/ledger.py")
text = path.read_text(encoding="utf-8")
old = '''    def _migrate(self) -> None:
        with self._tx() as db:
            db.executescript("""'''
new = '''    def _migrate(self) -> None:
        # sqlite3.executescript() controls its own transaction boundary. Running
        # it inside _tx() can leave the outer COMMIT with no active transaction.
        with self._lock:
            self._conn.executescript("""'''
if text.count(old) != 1:
    raise RuntimeError("ledger migration anchor changed")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Runtime V3 pre-test fixes applied")
