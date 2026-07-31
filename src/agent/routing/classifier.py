"""Deterministic request-domain and continuation classification."""

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from src.agent.conversation import recent_context_for_retrieval


LOW_SIGNAL_RE = re.compile(r"^[\W_]*$", re.UNICODE)
CASUAL_OPENING_RE = re.compile(
    r"^\s*(?:h+i+|hey+|hello+|yo+|sup+|what'?s up|wass?up|hiya|howdy|"
    r"lol|lmao|haha+|hehe+|thanks?|thank you|ty|idk|dunno|meh|bruh|bro)"
    r"\b(?P<tail>.*)$",
    re.IGNORECASE,
)
CASUAL_BLOCKLIST_RE = re.compile(
    r"\b(?:cookbook|serve|serving|launch|start|vllm|sglang|llama\.?cpp|"
    r"ollama|download|model|email|document|doc|note|calendar|task|search|"
    r"web|research|file|folder|repo|git|settings?|endpoint|api|token|mcp)\b",
    re.IGNORECASE,
)
EXPLICIT_CONTINUATION_RE = re.compile(
    r"^\s*(?:"
    r"yes|y|yeah|yep|ok|okay|sure|do it|go ahead|continue|carry on|"
    r"run it|launch it|start it|use that|that one|same|the same|"
    r"first|second|third|the first one|the second one|the third one|"
    r"[123]|[abc]"
    r")\s*(?:[.!?]+\s*)?$",
    re.IGNORECASE,
)
RETRY_CONTINUATION_RE = re.compile(
    r"\b(?:try again|retry|again|rerun|re-run|run it again|launch it again|"
    r"start it again|failed|fails?|died|crashed|broke|insta|instantly)\b",
    re.IGNORECASE,
)
COOKBOOK_CONTEXT_RE = re.compile(
    r"\b(?:cookbook|serve|serving|served|launch|start|preset|vllm|sglang|"
    r"llama\.?cpp|ollama|download|cached models?|model servers?|"
    r"running models?|gpu box|workstation|server|qwen|gemma|llama|"
    r"mistral|minimax)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoutingDecision:
    latest_user_text: str
    continuation: bool
    low_signal: bool
    domains: frozenset[str]
    retrieval_query: str
    decision_reasons: tuple[str, ...] = ()

    def as_legacy_dict(self) -> dict[str, object]:
        return {
            "low_signal": self.low_signal,
            "continuation": self.continuation,
            "domains": set(self.domains),
            "retrieval_query": self.retrieval_query,
        }


def is_explicit_continuation(text: str) -> bool:
    return bool(EXPLICIT_CONTINUATION_RE.match(str(text or "").strip()))


def is_casual_low_signal(text: str) -> bool:
    value = str(text or "").strip()
    match = CASUAL_OPENING_RE.match(value)
    if not match:
        return False
    tail = match.group("tail") or ""
    if CASUAL_BLOCKLIST_RE.search(tail):
        return False
    tail_words = re.findall(r"[A-Za-z0-9_'-]+", tail)
    return len(tail_words) <= 2


def is_contextual_retry_continuation(
    messages: Sequence[Mapping[str, Any]],
    text: str,
) -> bool:
    latest = str(text or "").strip()
    if not latest or not RETRY_CONTINUATION_RE.search(latest):
        return False
    recent = recent_context_for_retrieval(
        messages,
        max_user=5,
        max_chars=1200,
    )
    return bool(COOKBOOK_CONTEXT_RE.search(recent))


def assistant_requested_followup(
    messages: Sequence[Mapping[str, Any]],
) -> bool:
    seen_latest_user = False
    for message in reversed(messages):
        role = message.get("role")
        if role == "user" and not seen_latest_user:
            seen_latest_user = True
            continue
        if not seen_latest_user:
            continue
        if role != "assistant":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
            )
        text = str(content or "").lower()
        if "?" not in text:
            return False
        return bool(
            re.search(
                r"\b(what would you like|what should|what do you want|"
                r"which one|which model|what.+(?:todo|to-do|list|document|"
                r"email|model|server|item)|any specific|give me|tell me)\b",
                text,
            )
        )
    return False


def classify_routing_decision(
    messages: Sequence[Mapping[str, Any]],
    last_user: str,
) -> RoutingDecision:
    text = str(last_user or "").strip()
    retry = is_contextual_retry_continuation(messages, text)
    explicit = is_explicit_continuation(text)
    requested = assistant_requested_followup(messages)
    continuation = explicit or requested or retry
    retrieval_query = (
        recent_context_for_retrieval(messages)
        if continuation
        else text
    )
    query = retrieval_query.lower()
    reasons: list[str] = []
    if explicit:
        reasons.append("explicit_continuation")
    if requested:
        reasons.append("assistant_requested_followup")
    if retry:
        reasons.append("contextual_retry")

    if (
        not text
        or bool(LOW_SIGNAL_RE.match(text))
        or is_casual_low_signal(text)
    ):
        return RoutingDecision(
            latest_user_text=text,
            continuation=False,
            low_signal=True,
            domains=frozenset(),
            retrieval_query=text,
            decision_reasons=("low_signal",),
        )

    domains: set[str] = set()

    def has(*patterns: str) -> bool:
        return any(re.search(pattern, query) for pattern in patterns)

    if has(
        r"\b(cookbook|serve|serving|served|launch|start|preset|vllm|"
        r"sglang|llama\.?cpp|ollama|download|downloading|pull|cached models?|"
        r"running models?|model servers?|models? (?:are )?running|what models?|"
        r"model picker|gpu box|workstation|server|qwen|gemma|llama|mistral|"
        r"minimax)\b"
    ):
        domains.add("cookbook")
    if has(
        r"\b(emails?|mails?|gmail|inbox|reply|forward|cc|bcc|send email|"
        r"compose email|draft email|message chris|message him|message her)\b"
    ):
        domains.add("email")
    if has(
        r"\b(notes?|todos?|to-dos?|checklists?|tasks?|task list|remind me|"
        r"reminders?|buy|pickup|pick up)\b",
        r"\b(every day|every morning|every evening|recurring|automatically|"
        r"cron|scheduled task|background task)\b",
        r"\b(calendar|event|meeting|appointment|schedule)\b",
    ):
        domains.add("notes_calendar_tasks")
    if has(
        r"\b(documents?|docs?|draft|compose|poem|story|essay|outline|letter|"
        r"edit|rewrite|proofread|suggest|feedback|review this|make a file)\b"
    ):
        domains.add("documents")
    if "notes_calendar_tasks" not in domains and has(r"\bwrite\b"):
        domains.add("documents")
    if has(
        r"\b(search|web|google|look up|latest|news|current|weather|forecast|"
        r"stock price|price of|website|url|https?://|www\.)\b",
        r"\b(wyszukaj|wyszukać|wyszukac)\b.*"
        r"\b(internet|internecie|online|web)\b",
        r"\b(sprawd[zź]|znajd[zź])\b.*"
        r"\b(internet|internecie|online|web)\b",
        r"\b(aktualn\w*|bieżąc\w*|biezac\w*|dzisiaj|teraz)\b.*"
        r"\b(pogod\w*|temperatur\w*)\b",
        r"\b(research|deep dive|investigate|look into)\b",
    ):
        domains.add("web")
    if has(
        r"\b(open|show|toggle|turn on|turn off|disable|enable|switch model|"
        r"change model|settings|theme|panel)\b"
    ):
        domains.add("ui")
    if has(
        r"\b(session|chat history|rename chat|delete chat|archive chat|"
        r"fork chat|list chats)\b"
    ):
        domains.add("sessions")
    if has(
        r"\b(file|folder|directory|repo|git|grep|find in files|read file|"
        r"edit file|shell|terminal|bash)\b",
        r"\b(run|execute|test|debug|fix|save|create|edit|read|open)\b"
        r".{0,40}\b(python|javascript|typescript|java|c\+\+|cpp|c#|csharp|"
        r"rust|go|golang|ruby|php|swift|kotlin|bash|shell|html|css|sql|"
        r"code|script|program|game)\b",
        r"\b(python|javascript|typescript|java|c\+\+|cpp|c#|csharp|rust|go|"
        r"golang|ruby|php|swift|kotlin|bash|shell|html|css|sql)\b"
        r".{0,40}\b(file|script|program|app)\b",
    ):
        domains.add("files")
    if (
        has(r"\b(background|bg)\s+(jobs?|task)\b")
        or has(
            r"\b(kill|stop|cancel|terminate|check|tail|show|list)\b"
            r".{0,16}\bjobs?\b"
        )
        or has(
            r"\bjobs?\b.{0,16}\b(output|status|done|finished|running)\b"
        )
    ):
        domains.add("files")
    if has(
        r"\b(endpoint|api token|mcp|webhook|preference|configure|config|"
        r"setting)\b"
    ):
        domains.add("settings")
    if has(r"\b(contact|contacts|phone|phone number|address book|vcard)\b"):
        domains.add("contacts")
    if has(
        r"\bapi[ _]call\b",
        r"\bintegrations?\b",
        r"\b(?:home ?assistant|miniflux|gitea|linkding|jellyfin)\b",
    ):
        domains.add("integrations")

    if domains:
        reasons.extend(f"domain:{domain}" for domain in sorted(domains))
    low_signal = not continuation and not domains
    if low_signal:
        reasons.append("no_domain_match")
    return RoutingDecision(
        latest_user_text=text,
        continuation=continuation,
        low_signal=low_signal,
        domains=frozenset(domains),
        retrieval_query=retrieval_query,
        decision_reasons=tuple(reasons),
    )


def classify_agent_request(
    messages: Sequence[Mapping[str, Any]],
    last_user: str,
) -> dict[str, object]:
    return classify_routing_decision(messages, last_user).as_legacy_dict()
