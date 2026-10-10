"""Detect when the person explicitly asks for an internet search (English, Hindi, Hinglish).

Used by the live bridge as a safety net: if the model answers such a request without calling web_search,
the server asks it to search. Requires a search verb near a "place" word, so topics like
"Internet of Things" or "online learning" alone don't count.
"""

import re

_VERBS = (r"(?:search|searching|look\s*up|look|check|find|research|google|googling|browse|gather|verify|"
          r"dekho|dekh|dhundo|dhoondo|dhundho|batao|bataao|khojo|pata\s*karo)")
_PLACES = r"(?:internet|web|online|net|google)"
_GAP = r"\W+(?:\w+\W+){0,6}?"  # up to six words in between
_PATTERNS = [
    re.compile(rf"\b{_VERBS}{_GAP}{_PLACES}\b", re.IGNORECASE),
    re.compile(rf"\b{_PLACES}{_GAP}{_VERBS}\b", re.IGNORECASE),
    re.compile(r"\bweb\s*search\b|\bgoogle\s+(?:it|karo|kar\b)|\b(?:internet|net|online)\s+(?:pe|par|se)\b",
               re.IGNORECASE),
    re.compile(r"इंटरनेट|ऑनलाइन|गूगल|वेब\s*सर्च"),
]
# Phrases that contain a place word but are topics, not search requests.
_TOPICS = re.compile(r"\binternet\s+of\s+things\b|\bweb\s+(?:page|pages|site|sites|app|apps|development|of)\b",
                     re.IGNORECASE)


def wants_web_search(text: str) -> bool:
    cleaned = _TOPICS.sub(" ", text or "")
    return any(p.search(cleaned) for p in _PATTERNS)
