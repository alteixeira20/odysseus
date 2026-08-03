from pathlib import Path

path = Path("scripts/runtime_v3_workspace_journal_patch.py")
text = path.read_text(encoding="utf-8")
old_pattern = r'''def _commit_file_transaction\(prepared: List\[Dict\[str, Any\]\]\) -> None:.*?\n\nclass ListDirTool:'''
new_pattern = r'''def _commit_file_transaction\(prepared: List\[Dict\[str, Any\]\]\) -> None:.*?\n\ndef _pagination_args'''
old_replacement = '''def _commit_file_transaction(prepared: List[Dict[str, Any]]):
    """Commit a crash-recoverable multi-file transaction.

    Visibility is sequential while the process is alive, but a crash or restart
    deterministically converges to all-old or all-new before traffic is served.
    """
    return get_workspace_journal().commit(prepared)


class ListDirTool:'''
new_replacement = '''def _commit_file_transaction(prepared: List[Dict[str, Any]]):
    """Commit a crash-recoverable multi-file transaction.

    Visibility is sequential while the process is alive, but a crash or restart
    deterministically converges to all-old or all-new before traffic is served.
    """
    return get_workspace_journal().commit(prepared)


def _pagination_args'''
for old, new in ((old_pattern, new_pattern), (old_replacement, new_replacement)):
    if text.count(old) != 1:
        raise RuntimeError(f"workspace patch anchor fixer expected one match, found {text.count(old)}")
    text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
print("Runtime V3 workspace journal codemod anchor corrected")
