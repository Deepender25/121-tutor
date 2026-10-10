import pytest

from tutor.intent import wants_web_search

# Real requests from a session where the model skipped web_search.
SEARCH_REQUESTS = [
    "Okay, can you did you can you search this on internet and tell me from there?",
    "This definition do not feels that good to me. Can you search on internet and find the better definition?",
    "Go on internet and search about the different eras of cognitive different eras of computing.",
    "Aapko batao internet.",
    "Go ahead and try to gather information on internet.",
    "You are not using web search.",
    "Check the web for anything newer or different from what my sources say.",
    "google it please",
    "internet pe dekho ki latest kya hai",
    "इंटरनेट पर देखो",
    "look this up online",
]

NOT_SEARCH = [
    "Explain the Internet of Things section.",
    "What does my note say about online learning?",
    "Teach me the section on web pages and web development.",
    "So, what is the first topic?",
    "Can you explain it better?",
    "Find the definition of cognitive computing in my notes.",
]


@pytest.mark.parametrize("text", SEARCH_REQUESTS)
def test_detects_search_requests(text):
    assert wants_web_search(text)


@pytest.mark.parametrize("text", NOT_SEARCH)
def test_ignores_topics_and_normal_questions(text):
    assert not wants_web_search(text)
