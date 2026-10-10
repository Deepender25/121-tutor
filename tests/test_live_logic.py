from google.genai import errors

from tutor.server.live import WebSearchGuard, join_text
from tutor.voice.gemini_live import FATAL, STALE_HANDLE, TRANSIENT, classify_error


def api_error(code, message):
    return errors.APIError(code, {"error": {"message": message}}, None)


def test_expired_resumption_handle_starts_fresh_instead_of_failing():
    # Real sequence from a session: two 1011s, then the resume was rejected as expired. This used to be
    # misreported as "model not available" and ended the session.
    assert classify_error(api_error(1011, "Internal error encountered."), resuming=True) == TRANSIENT
    assert classify_error(api_error(1008, "BidiGenerateContent session not found or expired"), resuming=True) == STALE_HANDLE


def test_fatal_errors_stop_retrying():
    assert classify_error(api_error(1011, "You exceeded your current quota"), resuming=False) == FATAL
    assert classify_error(api_error(1007, "API key not valid."), resuming=False) == FATAL
    assert classify_error(api_error(1008, "models/x-live is not found for API version"), resuming=False) == FATAL
    assert classify_error(api_error(1006, "Abnormal closure."), resuming=False) == TRANSIENT


def test_guard_nudges_once_when_search_was_skipped():
    g = WebSearchGuard()
    g.user_input("Can you search on internet and find the better definition?")
    g.model_output()
    assert g.needs_nudge() is True
    assert g.needs_nudge() is False  # only once per request


def test_guard_quiet_when_model_searched_or_not_asked():
    g = WebSearchGuard()
    g.user_input("search the web for the latest version")
    g.tool_called("web_search")
    g.model_output()
    assert g.needs_nudge() is False
    g.user_input("explain the next section")  # new request after the model spoke
    g.model_output()
    assert g.needs_nudge() is False


def test_join_text_adds_missing_space_after_tool_call():
    assert join_text("Let me check that for you.", "The history of AI") == "Let me check that for you. The history of AI"
    assert join_text("The history", " of AI") == "The history of AI"
    assert join_text("nineteen fifty", "-six") == "nineteen fifty-six"


def test_history_keeps_citations_but_not_passage_text():
    from tutor.server.live import without_text

    passage = {"kind": "passage", "doc_id": "d", "page": 3, "snippet": "abc", "text": "long passage"}
    search = {"kind": "search", "query": "q", "refs": [{"doc_id": "d", "page": 1, "snippet": "s", "text": "chunk"}]}
    assert without_text(passage) == {"kind": "passage", "doc_id": "d", "page": 3, "snippet": "abc"}
    assert without_text(search)["refs"] == [{"doc_id": "d", "page": 1, "snippet": "s"}]
    assert "text" in search["refs"][0]  # the live event itself is untouched
