"""Determine whether visible application context belongs to this turn."""

import re
from typing import Mapping


def is_email_document(active_document) -> bool:
    if active_document is None:
        return False
    raw_document = getattr(active_document, "current_content", "") or ""
    title = (getattr(active_document, "title", "") or "").strip().lower()
    return (
        getattr(active_document, "language", None) == "email"
        or title in {"new email", "new mail", "new message"}
        or (
            "To:" in raw_document[:400]
            and "Subject:" in raw_document[:400]
            and "\n---\n" in raw_document
        )
    )


def turn_targets_active_document(
    intent: Mapping[str, object],
    last_user: str,
    active_document,
) -> bool:
    if active_document is None:
        return False
    if "documents" in (intent.get("domains") or set()):
        return True
    text = str(last_user or "").strip().lower()
    if not text:
        return False
    if is_email_document(active_document) and re.search(
        r"\b(email|mail|reply|respond|response|draft|compose|send|"
        r"tell them|tell her|tell him|say|write|make it say|japanese|japan|"
        r"polite|formal|tone|style)\b",
        text,
    ):
        return True
    if re.search(
        r"\b(?:make|change|update|fix|edit|rewrite|rework|revise|replace|"
        r"remove|delete|add|append|insert|set|turn)\b.{0,80}\b(?:day\s*\d+|"
        r"row|rows|column|columns|table|section|chapter|part|paragraph|line|"
        r"lines|title|heading|body|intro|introduction|conclusion|schedule|"
        r"itinerary|draft|content)\b",
        text,
    ):
        return True
    if re.search(
        r"\b(?:day\s*\d+|row|rows|column|columns|table|section|chapter|part|"
        r"paragraph|line|lines|title|heading|body|intro|introduction|"
        r"conclusion|schedule|itinerary)\b.{0,80}\b(?:make|change|update|fix|"
        r"edit|rewrite|rework|revise|replace|remove|delete|add|append|insert|"
        r"set|turn)\b",
        text,
    ):
        return True
    if re.search(
        r"\b(?:add|insert|include|apply|put)\b.+\b(?:to it|to this|there|"
        r"in it|in this|in the text|in the document)\b",
        text,
    ):
        return True
    if re.search(
        r"\b(?:make it|make this|expand it|expand this|extend it|extend this|"
        r"continue it|continue this)\b.*\b(?:longer|shorter|bigger|smaller|"
        r"more detailed|more concise|expanded|extended)?\b",
        text,
    ):
        return True
    return bool(
        re.search(
            r"\b(document|doc|draft|text|poem|story|essay|outline|letter|"
            r"paragraph|stanza|line|title|heading|section|sentence|word|caps|"
            r"uppercase|lowercase|rewrite|reword|style|tone|suggest|"
            r"suggestions|feedback|improve|edit|change|remove|delete|replace|"
            r"add another|append|original text|in the document|the document|"
            r"this document)\b",
            text,
        )
    )
