"""Detect when the person explicitly asks for an internet search (English, Hindi, Hinglish).

Used by the live bridge as a safety net: if the model answers such a request without calling web_search,
the server asks it to search. Requires a search verb near a "place" word, so topics like
"Internet of Things" or "online learning" alone don't count. A bare "net" only counts in phrases like
"net pe dekho" or "on the net" (so "net force" or "neural net" don't), and a negated request
("don't search online", "internet pe mat dekho") never counts.
"""

import re

_VERBS = (r"(?:search|searching|look\s*up|look|check|find|research|google|googling|browse|gather|verify|"
          r"dekho|dekh|dhundo|dhoondo|dhundho|batao|bataao|khojo|pata\s*karo)")
_PLACES = r"(?:internet|web|online|google)"
_GAP = r"\W+(?:\w+\W+){0,6}?"  # up to six words in between
_PATTERNS = [
    re.compile(rf"\b{_VERBS}{_GAP}{_PLACES}\b", re.IGNORECASE),
    re.compile(rf"\b{_PLACES}{_GAP}{_VERBS}\b", re.IGNORECASE),
    re.compile(r"\bweb\s*search\b|\bgoogle\s+(?:it|karo|kar\b)|\b(?:internet|net|online)\s+(?:pe|par|se)\b"
               r"|\bon\s+the\s+net\b", re.IGNORECASE),
    # Devanagari: a place word followed by a search verb ("इंटरनेट पर देखो"), not a bare mention.
    re.compile(r"(?:इंटरनेट|ऑनलाइन|गूगल|वेब)\s*(?:पर|पे|में|से)?\s*(?:देख|सर्च|खोज|चेक|ढूंढ|ढूँढ)|वेब\s*सर्च"),
]
# Phrases that contain a place word but are topics, not search requests.
_TOPICS = re.compile(
    r"\binternet\s+of\s+things\b"
    r"|\binternet\s+(?:layer|protocol|protocols|access|service|services|address|addresses)\b"
    r"|\bweb\s+(?:page|pages|site|sites|app|apps|development|of|browser|browsers|server|servers|services"
    r"|search\s+wala)\b"
    r"|\bonline\s+(?:learning|banking|payment|payments|shopping|course|courses|education|exam|exams|class|classes"
    r"|mode|transaction|transactions|system|systems|game|games)\b"
    r"|\bgoogle\s+(?:docs|drive|sheets|slides|maps|colab|scholar|cloud|classroom|search\s+engine)\b",
    re.IGNORECASE)
# "Don't search", "no need to google it", "internet pe mat dekho", "मत देखो": the person asked NOT to search.
_NEGATED = re.compile(
    r"\b(?:don'?t|do\s+not|no\s+need|without|never)\b\W+(?:\w+\W+){0,3}?"
    r"(?:search|look|check|google|browse|dekh\w*|dhund\w*|khoj\w*)"
    r"|\b(?:internet|web|online|google|net)\b\W+(?:\w+\W+){0,2}?mat\b"
    r"|\bmat\W+(?:\w+\W+){0,2}?(?:dekh\w*|dhund\w*|khoj\w*|search\w*|google\w*)"
    r"|(?:मत|नहीं|ना)\s*(?:देख|सर्च|खोज)",
    re.IGNORECASE)


def wants_web_search(text: str) -> bool:
    if _NEGATED.search(text or ""):
        return False
    cleaned = _TOPICS.sub(" ", text or "")
    return any(p.search(cleaned) for p in _PATTERNS)
