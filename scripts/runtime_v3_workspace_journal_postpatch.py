from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one post-patch anchor, found {count}: {old[:140]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/agent/runtime_v3/workspace_journal.py",
    '''        manifest["manifest_sha256"] = self._manifest_digest(manifest)
        return manifest

    def _serializable_manifest''',
    '''        return manifest

    def _serializable_manifest''',
)

replace_once(
    "src/agent/runtime_v3/workspace_journal.py",
    '''            except BaseException:
                try:
                    self._rollback(manifest_path, manifest)
''',
    '''            except BaseException:
                if manifest.get("phase") == "aborted_concurrent_modification":
                    raise
                try:
                    self._rollback(manifest_path, manifest)
''',
)

print("Runtime V3 workspace journal post-patch corrections applied")
