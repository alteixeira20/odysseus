"""Canonical deterministic domain-to-tool and domain-rule mappings."""

from src.tool_policy import WEB_TOOL_NAMES


LINK_RULES = """\
## Link conventions
When referencing app entities by id, use clickable markdown anchors:
- Sessions: `[Name](#session-<id>)`
- Documents: `[Title](#document-<id>)`
- Notes: `[Title](#note-<id>)`
- Emails: `[Subject](#email-<uid>)`
- Calendar events: `[Summary](#event-<uid>)`
- Tasks: `[Task name](#task-<id>)`
- Skills: `[skill-name](#skill-<name>)`
- Research jobs: `[Topic](#research-<session_id>)`
"""

DOMAIN_RULES = {
    "web": """\
## Web rules
- For web lookup/search/latest/current requests, use `web_search` or `web_fetch`.
- Do not use shell, Python, curl, requests, or scraping code for web lookup unless web tools are unavailable or already failed.
- "Research X" means `trigger_research`, not a one-off `web_search`, unless the user explicitly asks for a quick lookup.""",
    "documents": """\
## Document rules
- For long code/content (>15 lines), use `create_document` instead of pasting into chat.
- If an active document is open, "fix this", "add X", "change Y", etc. usually refers to that document.
- Use `edit_document` for targeted changes. Use `update_document` only for genuine full rewrites.
- For feedback/review/suggestions on an open document, use `suggest_document`.""",
    "email": """\
## Email rules
- Email UIDs are the values after `UID:` in tool output, never list row numbers.
- For latest/newest email, list with `max_results: 1`, `unread_only: false`, then read the returned UID if needed.
- For named mailboxes/accounts, call `list_email_accounts` if needed and pass the exact `account` value.
- Bulk email actions use `bulk_email` once with explicit UIDs; do not loop one message at a time.
- "Write/draft a reply saying X" means open a pre-filled draft via `ui_control open_email_reply ... <body>` / structured `body`; only `reply_to_email` when the user clearly wants to send now.""",
    "cookbook": """\
## Cookbook/model-serving rules
- Cookbook is the LLM-serving subsystem.
- "What's running/serving" starts with `list_served_models`. "What's downloading" uses `list_downloads`.
- Launch known models manually by checking `list_serve_presets` before raw `serve_model`.
- Downloads/serves run on a Cookbook server; pass the named `host` when the user names one.
- Do not launch model servers manually with bash/ssh/tmux. Use `serve_model`/`serve_preset` so the UI can track and stop them.
- After a successful serve, verify with `list_served_models`; if an external server is running but invisible, use `adopt_served_model`.""",
    "notes_calendar_tasks": """\
## Notes/calendar/tasks rules
- Notes/todos/reminders use `manage_notes`, not memory.
- Calendar create/update/delete should call `manage_calendar` with `action=list_calendars` first.
- Recurring/automatic/scheduled requests create a `manage_tasks` task; do not just perform the action once.""",
    "ui": """\
## UI rules
- "Open/show <panel>" uses `ui_control open_panel <name>`.
- Tool toggles like "turn off shell/search/research" use `ui_control toggle <name> <on|off>`, not memory.""",
    "sessions": """\
## Chat/session rules
- Odysseus chats are sessions. Use `list_sessions`/`manage_session`; do not shell out looking for chat files.
- Preserve clickable session links from tool output in your final answer.""",
    "files": """\
## File rules
- Use file tools for real disk files. Use document tools only for editor documents.
- Prefer `grep`, `glob`, and `ls` over shell equivalents when available.
- Use `edit_file`/`write_file` for writes; avoid shell redirection/heredocs for editing files.""",
    "settings": """\
## Settings/API rules
- Use `manage_settings` for preferences and tool enable/disable.
- Use named tools over `app_api` when a named wrapper exists.
- `app_api` is only for safe UI/API actions without a named tool; do not use it for shell, package installs, engine rebuilds, or sensitive auth/admin paths.""",
    "contacts": """\
## Contacts rules
- Use `resolve_contact` to look up a contact's email or phone number by name. Searches the CardDAV address book and sent email history.
- Use `manage_contact` to list, add, update, or delete contacts in the address book.
- Do NOT use `manage_memory` for contact lookups — contact details live in the address book, not memory.""",
    "integrations": """\
## Integration/API rules
- To query or control a configured service integration (Home Assistant, Miniflux, Gitea, Linkding, Jellyfin, or any other registered service), use `api_call` with the integration name, HTTP method, path, and optional JSON body.
- Do not use shell, curl, or `app_api` to reach a user's connected integration when `api_call` is available.""",
}

DOMAIN_TOOL_MAP = {
    "web": set(WEB_TOOL_NAMES),
    "documents": {
        "create_document",
        "edit_document",
        "update_document",
        "suggest_document",
        "manage_documents",
    },
    "email": {
        "list_email_accounts",
        "list_emails",
        "read_email",
        "scan_email_unsubscribes",
        "unsubscribe_email",
        "send_email",
        "reply_to_email",
        "bulk_email",
        "archive_email",
        "delete_email",
        "mark_email_read",
        "resolve_contact",
        "manage_contact",
    },
    "cookbook": {
        "download_model",
        "serve_model",
        "serve_preset",
        "list_serve_presets",
        "list_served_models",
        "stop_served_model",
        "tail_serve_output",
        "list_downloads",
        "cancel_download",
        "search_hf_models",
        "list_cached_models",
        "list_cookbook_servers",
        "adopt_served_model",
    },
    "notes_calendar_tasks": {
        "manage_notes",
        "manage_calendar",
        "manage_tasks",
    },
    "ui": {"ui_control"},
    "sessions": {
        "create_session",
        "list_sessions",
        "manage_session",
        "send_to_session",
        "search_chats",
    },
    "files": {
        "bash",
        "python",
        "read_file",
        "write_file",
        "edit_file",
        "apply_patch",
        "todowrite",
        "grep",
        "glob",
        "ls",
        "get_workspace",
        "manage_bg_jobs",
    },
    "settings": {
        "manage_settings",
        "manage_endpoints",
        "manage_mcp",
        "manage_webhooks",
        "manage_tokens",
        "app_api",
    },
    "contacts": {"resolve_contact", "manage_contact"},
    "integrations": {"api_call"},
}

WORKSPACE_TERMINUS_TOOLS = (
    DOMAIN_TOOL_MAP["files"]
    | {
        "manage_skills",
        "ask_teacher",
        "web_search",
        "web_fetch",
        "ask_user",
        "update_plan",
    }
)


def domain_rules_for_tools(tool_names: set[str]) -> list[str]:
    names = set(tool_names or set())
    rules = [
        DOMAIN_RULES[domain]
        for domain, domain_tools in DOMAIN_TOOL_MAP.items()
        if names & domain_tools
    ]
    if names & {
        "create_session",
        "list_sessions",
        "manage_session",
        "manage_documents",
        "manage_notes",
        "manage_calendar",
        "manage_tasks",
        "manage_skills",
        "manage_research",
    }:
        rules.append(LINK_RULES)
    return rules
