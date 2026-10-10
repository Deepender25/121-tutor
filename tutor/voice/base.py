"""Voice session interface: the swap point between Gemini Live and any future cascade (STT+LLM+TTS).

The web layer only talks to this interface. A session receives the user's audio/text and emits events
through an async callback; tool calls are executed by the session via the `run_tool` callback.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

INPUT_SAMPLE_RATE = 16000  # 16-bit mono PCM from the browser
OUTPUT_SAMPLE_RATE = 24000  # 16-bit mono PCM to the browser


@dataclass
class VoiceEvent:
    """kind: status | audio | transcript | interrupted | turn_complete | tool_call | tool_result | error"""

    kind: str
    data: dict = field(default_factory=dict)
    audio: bytes | None = None  # only for kind == "audio"


EmitFn = Callable[[VoiceEvent], Awaitable[None]]
# (tool name, args) -> (result for the model, ui payload or None)
RunToolFn = Callable[[str, dict], Awaitable[tuple[dict, Any]]]
PromptFn = Callable[[], str]  # builds a fresh system prompt (called on every (re)connect)


class VoiceSession(ABC):
    @abstractmethod
    async def run(self) -> None:
        """Connect and stream until close() is called. Reconnects transparently when possible."""

    @abstractmethod
    async def send_audio(self, pcm16: bytes) -> None: ...

    @abstractmethod
    async def end_audio(self) -> None:
        """Mic paused: flush any buffered audio so the model can respond."""

    @abstractmethod
    async def send_text(self, text: str) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...
