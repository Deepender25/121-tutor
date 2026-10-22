import asyncio
import json
import threading
from types import SimpleNamespace

from google.genai import errors
from websockets.exceptions import ConnectionClosedOK

from tutor import config
from tutor.server import live
from tutor.server.live import TurnRecorder, WebSearchGuard, join_text
from tutor.voice import gemini_live
from tutor.voice.gemini_live import FATAL, STALE_HANDLE, TRANSIENT, GeminiLiveSession, classify_error


def api_error(code, message):
    return errors.APIError(code, {"error": {"message": message}}, None)


# ---- fakes ------------------------------------------------------------------------------------------------


class FakeNotebooks:
    def __init__(self):
        self.history = []  # one list of entries per append_history call
        self.nb = SimpleNamespace(name="Notes", sources=["d1"], active_source="d1")

    def get(self, notebook_id):
        return self.nb

    def update(self, notebook_id, **changes):
        return self.nb

    def append_history(self, notebook_id, entries):
        self.history.append(entries)


class FakeState:
    def __init__(self, ready=True):
        self.notebooks = FakeNotebooks()
        self.cache = None
        self.embedder = object()
        self.embedder_error = None
        self.embedder_ready = threading.Event()
        if ready:
            self.embedder_ready.set()
        self.style = {"encouragement": "balanced", "expression": "natural", "teaching": "direct"}

    def has_gemini_key(self):
        return True

    def settings(self):
        return {"voice": "Kore", "style": dict(self.style)}

    def require_embedder(self, timeout=180):
        if not self.embedder_ready.wait(timeout):
            raise RuntimeError("The embedding model is still loading. Try again in a moment.")
        return self.embedder


class FakeWS:
    """The browser end of the live WebSocket: `incoming` feeds ws.receive(), `sent` collects JSON frames."""

    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def accept(self):
        pass

    async def receive(self):
        return await self.incoming.get()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        pass

    async def close(self):
        self.closed = True

    def text(self, data):
        self.incoming.put_nowait({"type": "websocket.receive", "text": data if isinstance(data, str) else json.dumps(data)})

    def leave(self):
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1001})


class FakeSession:
    """Stands in for GeminiLiveSession inside live_endpoint (no network)."""

    made = []

    def __init__(self, **kwargs):
        self.kwargs, self.texts, self.closed = kwargs, [], asyncio.Event()
        FakeSession.made.append(self)

    async def run(self):
        await self.closed.wait()

    async def send_text(self, text):
        self.texts.append(text)

    async def send_audio(self, pcm):
        pass

    async def end_audio(self):
        pass

    async def close(self):
        self.closed.set()


def fake_endpoint(monkeypatch):
    FakeSession.made = []
    monkeypatch.setattr(live, "GeminiLiveSession", FakeSession)
    monkeypatch.setattr(live, "build_system_prompt", lambda nb, cache, style=None: f"prompt {json.dumps(style)}")
    monkeypatch.setattr(config, "gemini_api_key", lambda: "test-key")


async def until(predicate, seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def gemini_session(build_prompt=lambda: "prompt"):
    events = []

    async def emit(e):
        events.append(e)

    async def run_tool(name, args):
        return {}, None

    s = GeminiLiveSession(api_key="test-key", model="m", voice="Kore", build_prompt=build_prompt,
                          run_tool=run_tool, emit=emit)
    return s, events


def no_wait(monkeypatch):
    """Record backoff sleeps instead of waiting, and skip the idle watchdog."""
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *a, **k):
        sleeps.append(seconds)
        await real_sleep(0)

    async def no_watchdog(self):
        pass

    monkeypatch.setattr(gemini_live.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(GeminiLiveSession, "_idle_watchdog", no_watchdog)
    return sleeps


# ---- Gemini session -----------------------------------------------------------------------------------------


def test_prompt_failure_is_fatal_without_retrying(monkeypatch):
    sleeps = no_wait(monkeypatch)

    def broken():
        raise KeyError("doc-123")

    s, events = gemini_session(broken)
    asyncio.run(s.run())
    errors_ = [e for e in events if e.kind == "error"]
    assert len(errors_) == 1 and errors_[0].data["fatal"] and "Couldn't prepare the tutor" in errors_[0].data["message"]
    assert sleeps == []
    assert events[-1].kind == "status" and events[-1].data["state"] == "closed"


def test_first_connect_gives_up_after_two_attempts(monkeypatch):
    sleeps = no_wait(monkeypatch)
    attempts = []

    def connect(**kwargs):
        attempts.append(kwargs)
        raise OSError("getaddrinfo failed")

    s, events = gemini_session()
    s._client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect)))
    asyncio.run(s.run())
    errors_ = [e for e in events if e.kind == "error"]
    assert len(attempts) == 2 and len(sleeps) == 1
    assert len(errors_) == 1 and errors_[0].data["fatal"] and "Couldn't reach Gemini" in errors_[0].data["message"]


def test_a_connect_that_never_answers_counts_as_a_failed_attempt(monkeypatch):
    """While the internet is down, looking up Google's address can hang for minutes: each attempt has a deadline,
    so the person gets "Couldn't reach Gemini" instead of nothing."""
    from tutor.voice import gemini_live

    no_wait(monkeypatch)
    monkeypatch.setattr(gemini_live, "CONNECT_TIMEOUT_S", 0.2)
    attempts = []

    class Hangs:
        async def __aenter__(self):
            attempts.append(1)
            await asyncio.Event().wait()

        async def __aexit__(self, *exc):
            return False

    s, events = gemini_session()
    s._client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=lambda **kw: Hangs())))
    asyncio.run(asyncio.wait_for(s.run(), 10))
    errors_ = [e for e in events if e.kind == "error"]
    assert len(attempts) == 2
    assert len(errors_) == 1 and "Couldn't reach Gemini" in errors_[0].data["message"]


def test_text_sent_while_closing_is_kept_for_the_next_connection():
    class Closing:
        async def send_client_content(self, **kwargs):
            raise ConnectionClosedOK(None, None)

    async def go():
        s, _ = gemini_session()
        s._session = Closing()
        s._ready.set()
        await s.send_text("hello")  # must not raise
        assert s._pending_text == ["hello"]
        await s._flush_pending_text()  # still closing: put back, in order
        assert s._pending_text == ["hello"]

    asyncio.run(go())


def test_sessions_share_one_client():
    a, _ = gemini_session()
    b, _ = gemini_session()
    assert a._client is b._client


# ---- history ----------------------------------------------------------------------------------------------


def test_text_typed_during_a_reply_is_paired_with_the_next_answer():
    state = FakeState()
    r = TurnRecorder(state, "nb")
    r.user_text("Count to ten.")
    r.model_text("one, two")
    r.typed("Stop")
    r.model_text(" three")  # Gemini still finishing the old turn
    r.flush()  # the "interrupted" path
    assert state.notebooks.history[0] == [{"role": "user", "text": "Count to ten."},
                                         {"role": "model", "text": "one, two three"}]
    r.model_text("OK")
    r.flush()
    assert state.notebooks.history[1] == [{"role": "user", "text": "Stop"}, {"role": "model", "text": "OK"}]


def test_typed_text_with_no_reply_in_progress_is_a_plain_user_message():
    state = FakeState()
    r = TurnRecorder(state, "nb")
    r.typed("Hello")
    assert r.user == "Hello" and r.pending == ""
    r.model_text("Hi")
    r.typed("Next")
    r.finish()  # session ends: both exchanges are saved
    assert state.notebooks.history == [[{"role": "user", "text": "Hello"}, {"role": "model", "text": "Hi"}],
                                       [{"role": "user", "text": "Next"}]]


# ---- the WebSocket bridge -----------------------------------------------------------------------------------


def test_cold_start_says_why_and_keeps_early_texts(monkeypatch):
    fake_endpoint(monkeypatch)
    state = FakeState(ready=False)

    async def go():
        ws = FakeWS()
        task = asyncio.create_task(live.live_endpoint(ws, "nb", state))
        await until(lambda: ws.sent)
        assert ws.sent[0] == {"type": "notice", "text": live.SEARCH_LOADING}
        ws.incoming.put_nowait({"type": "websocket.receive", "bytes": b"\0\0"})  # mic audio: dropped
        ws.text({"type": "text", "text": 'Teach me the section "X" (section id s001).', "display": 'Teach me "X"'})
        await asyncio.sleep(0.05)
        assert not FakeSession.made  # still waiting for search
        state.embedder_ready.set()
        await until(lambda: FakeSession.made and FakeSession.made[0].texts)
        session = FakeSession.made[0]
        assert session.texts == ['Teach me the section "X" (section id s001).']  # Gemini gets the full text
        ws.leave()
        await asyncio.wait_for(task, 5)
        assert state.notebooks.history == [[{"role": "user", "text": 'Teach me "X"'}]]  # history the short one

    asyncio.run(go())


def test_page_leaving_during_cold_start_ends_at_once(monkeypatch, caplog):
    fake_endpoint(monkeypatch)
    state = FakeState(ready=False)

    async def go():
        ws = FakeWS()
        task = asyncio.create_task(live.live_endpoint(ws, "nb", state))
        await until(lambda: ws.sent)
        ws.leave()
        await asyncio.wait_for(task, 2)  # returns (freeing the session slot) while search is still loading
        assert not state.embedder_ready.is_set()
        state.embedder_ready.set()  # let the waiting thread finish (asyncio.run waits for it)

    caplog.set_level("INFO", logger="live")
    asyncio.run(go())
    assert not FakeSession.made
    assert "The page left while search was loading" in caplog.text


def test_no_notice_when_search_is_ready_and_bad_frames_are_skipped(monkeypatch, caplog):
    fake_endpoint(monkeypatch)
    state = FakeState(ready=True)

    async def go():
        ws = FakeWS()
        task = asyncio.create_task(live.live_endpoint(ws, "nb", state))
        await until(lambda: FakeSession.made)
        session = FakeSession.made[0]
        ws.text("not json")
        ws.text("[1, 2]")
        ws.text({"type": "text", "text": "Still here?"})
        await until(lambda: session.texts)
        assert not task.done()  # the bad frames didn't end the session
        assert session.texts == ["Still here?"]
        # The manner and voice are fixed for the conversation, even if Settings change meanwhile.
        state.style["encouragement"] = "warm"
        assert '"encouragement": "balanced"' in session.kwargs["build_prompt"]()
        assert session.kwargs["voice"] == "Kore"
        ws.text({"type": "close"})
        await asyncio.wait_for(task, 5)
        return ws

    caplog.set_level("DEBUG", logger="live")
    ws = asyncio.run(go())
    assert "isn't JSON" in caplog.text
    assert not any(m.get("type") == "notice" for m in ws.sent)


def test_missing_key_message_matches_the_ui():
    assert "GEMINI_API_KEY" in live.NO_KEY and "Voice Tutor - API" in live.NO_KEY and "run.bat again" in live.NO_KEY


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


def test_embedding_crash_explanations(monkeypatch):
    from tutor.retrieval import worker

    monkeypatch.setattr(worker, "memory_mb", lambda: (8000, 5000))
    info = {"cpu": "Intel(R) Celeron(R) N4020"}
    illegal = worker.describe_crash(-1073741795, info)  # 0xC000001D as a signed exit code
    assert "Celeron" in illegal and "illegal-instruction" in illegal and "embedder-crash.log" in illegal
    assert "ran out of memory" in worker.describe_crash(0xC0000017, info)
    assert "0xC0000005" in worker.describe_crash(-1073741819, info)  # access violation, plenty of memory
    monkeypatch.setattr(worker, "memory_mb", lambda: (4000, 600))
    assert "ran out of memory" in worker.describe_crash(-1073741819, info)  # same crash, almost no free memory


def _tone(db: float, n: int = 1600, seed: int = 0) -> bytes:
    import numpy as np
    rms = 32767 * 10 ** (db / 20)
    return np.clip(np.random.default_rng(seed).normal(0, rms, n), -32768, 32767).astype(np.int16).tobytes()


def test_mic_meter_tells_how_long_after_they_went_quiet_the_reply_came():
    from tutor.server.live import MicMeter
    m = MicMeter()
    t = 0.0
    for _ in range(5):  # a mic that starts with zeros: not the room's level
        t += 0.1
        m.chunk(bytes(3200), t)
    assert m.floor is None
    for _ in range(20):  # two seconds of a quiet room
        t += 0.1
        m.chunk(_tone(-60), t)
    for _ in range(15):  # talking
        t += 0.1
        m.chunk(_tone(-25), t)
    talked_until = t
    for i in range(30):  # quiet again: the room level didn't drift up to the voice
        t += 0.1
        m.chunk(_tone(-60, seed=i), t)
        assert not m.stuck(t) or t - talked_until >= MicMeter.STUCK_S
    assert abs(m.floor - -60) < 3
    assert m.stuck(t + 3) and not m.stuck(t + 4)  # 5 s after they went quiet, said once
    note = m.replied(talked_until + 1.2)
    assert "you went quiet 1.2s before" in note and "room -6" in note
    assert m.replied(t) == ""  # no talking since that reply


def test_mic_meter_ignores_the_tutors_voice_and_slow_tools():
    from tutor.server.live import MicMeter
    m = MicMeter()
    t = 0.0
    for _ in range(10):
        t += 0.1
        m.chunk(_tone(-60), t)
    m.tutor_audio(48000 * 3, t)  # three seconds of reply audio: the mic hears it come back
    for _ in range(20):
        t += 0.1
        m.chunk(_tone(-30), t)
    assert not m.spoke
    m.tutor_stopped(t)  # they interrupted: what the mic hears now is them
    t += 0.6
    m.chunk(_tone(-30), t)
    assert m.spoke
    m.taken()  # a web search started: a long wait for the reply now isn't a stuck turn
    assert not m.stuck(t + 10)


def test_teaching_guard_holds_back_reading_on_until_the_part_is_taught():
    from tutor.server.live import TeachingGuard
    g = TeachingGuard()
    assert g.hold("read_passage", {"target": "start"}) is None
    g.read({"section": "1. What is Cognitive Computing?", "pages": "3", "text": "x" * 1500})
    g.model_output("Continuing on, let's explore how these systems operate and differ from previous generations.")
    held = g.hold("read_passage", {"target": "next"})
    assert held and "haven't taught" in held["not_read_yet"] and "page 3" in held["not_read_yet"]
    assert g.hold("read_passage", {"target": "s007"}) is None  # a jump elsewhere isn't reading on
    assert g.hold("search_sources", {"query": "x"}) is None
    g.model_output("Cognition means the mental processes of knowing: perceiving, learning, reasoning and deciding. " * 3)
    assert g.hold("read_passage", {"target": "next"}) is None


def test_teaching_guard_lets_the_student_skip_and_never_stalls():
    from tutor.server.live import TeachingGuard
    g = TeachingGuard()
    g.read({"section": "Cover", "pages": "1", "text": "COGNITIVE COMPUTING Unit 1"})  # a cover page: little to say
    assert g.hold("read_passage", {}) is None
    g.read({"section": "Long part", "pages": "4", "text": "x" * 2000})
    g.user_input()  # "skip this, next"
    assert g.hold("read_passage", {"target": "next"}) is None
    g.read({"section": "Long part", "pages": "5", "text": "x" * 2000})
    assert g.hold("read_passage", {"target": "next"}) and g.hold("read_passage", {"target": "next"})
    assert g.hold("read_passage", {"target": "next"}) is None  # after two holds it goes ahead rather than stall


def test_a_turn_that_ends_announcing_a_step_it_didnt_take_is_noticed():
    from tutor.server.live import TeachingGuard, announces_next_step
    assert announces_next_step("This unit covers the basics. Let's dive right into the first topic, which is defining "
                               "what cognitive computing really is.")
    assert announces_next_step("One moment. Let me read that section for you.")
    for fine in ("Where would you like to start?", "Let me know if you'd like to continue.",
                 "Whenever you're ready, let's continue.", "That's the end of the section. Well done!",
                 "If you'd like, we'll look at the next part.", ""):
        assert not announces_next_step(fine), fine
    g = TeachingGuard()
    g.model_output("Let's move to the next part.")
    g.tool_called()  # it did take the step
    g.model_output("The intern reads thousands of case files, the corpus, and proposes diagnoses with evidence.")
    assert not g.needs_nudge()
    g.model_output(" Now let's look at the three eras of computing.")
    assert "ended your turn without doing it" in g.needs_nudge() and g.needs_nudge() is None  # once
    g.user_input()
    g.model_output("Let's begin with the history.")
    assert g.needs_nudge()


def test_a_turn_that_stops_in_the_middle_of_a_section_is_carried_on():
    from tutor.server.live import TeachingGuard
    g = TeachingGuard()
    part = {"section": "1. What is Cognitive Computing?", "pages": "3", "text": "x" * 1500,
            "is_section_end": False, "parts_left_in_section": 2}
    g.read(part)
    g.model_output("It is trained on data, learns patterns, builds a model and scores hypotheses with evidence. " * 3)
    nudge = g.needs_nudge()
    assert nudge and "2 parts" in nudge and "target next" in nudge and g.needs_nudge() is None
    g.read({**part, "parts_left_in_section": 1})
    g.model_output("The intern reads the corpus and proposes diagnoses. " * 5 + "Can you say why that's a hypothesis?")
    assert g.needs_nudge() is None  # a question for them: their turn
    g.read({**part, "is_section_end": True, "parts_left_in_section": 0})
    g.model_output("Augmented intelligence, not artificial replacement. " * 5)
    assert g.needs_nudge() is None  # the section is done


def test_teaching_guard_keeps_it_from_skipping_the_rest_of_a_section_on_its_own():
    from tutor.server.live import TeachingGuard
    g = TeachingGuard()
    part = {"section": "1. What is Cognitive Computing?", "pages": "3", "text": "x" * 1500,
            "is_section_end": False, "parts_left_in_section": 2}
    g.read(part)
    g.model_output("Cognition means the mental processes of knowing: perceiving, learning, reasoning, deciding. " * 3)
    held = g.hold("read_passage", {"target": "s008"})
    assert held and "2 more parts" in held["not_read_yet"] and "target next" in held["not_read_yet"]
    assert g.hold("read_passage", {"target": "previous"}) is None
    g.user_input()  # "skip to the history part"
    assert g.hold("read_passage", {"target": "s008"}) is None
    g.read({**part, "is_section_end": True, "parts_left_in_section": 0})
    g.model_output("x" * 300)
    assert g.hold("read_passage", {"target": "s008"}) is None  # the section is done: moving on is fine
    g.read({"section": "Cover", "pages": "1", "text": "COGNITIVE COMPUTING", "is_section_end": False,
            "parts_left_in_section": 1})
    assert g.hold("read_passage", {"target": "s005"}) is None  # past the cover to the first topic


def test_front_matter_isnt_held_back():
    from tutor.server.live import TeachingGuard
    g = TeachingGuard()
    g.read({"section": "COGNITIVE COMPUTING › Contents", "pages": "2", "text": "1. What is Cognitive Computing? 3\n" * 40,
            "is_section_end": False, "parts_left_in_section": 1})
    g.model_output("The contents list the course's two parts.")
    assert g.hold("read_passage", {"target": "next"}) is None and g.hold("read_passage", {"target": "s005"}) is None
