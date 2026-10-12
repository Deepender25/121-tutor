"""Voice-session audio recording for diagnostics (VOICE_TUTOR_RECORD_AUDIO=on)."""

import wave

from tutor import config
from tutor.server.live import AudioTape


def test_records_both_directions_with_a_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEBUG_AUDIO_DIR", tmp_path)
    tape = AudioTape("Biology")
    tape.tutor(b"\x01\x00" * 24000)  # 1 s at 24 kHz
    tape.mic(b"\x02\x00" * 8000)  # 0.5 s at 16 kHz
    tape.note("transcript hello")
    tape.close()
    tutor, mic, timeline = tape.paths
    with wave.open(str(tutor)) as w:
        assert (w.getframerate(), w.getnframes()) == (24000, 24000)
    with wave.open(str(mic)) as w:
        assert (w.getframerate(), w.getnframes()) == (16000, 8000)
    lines = timeline.read_text(encoding="utf-8").splitlines()
    assert "session: Biology" in lines[0]
    assert "tutor.wav@   1.00s" in lines[1] and "mic.wav@   0.50s" in lines[1] and lines[1].endswith("transcript hello")


def test_keeps_only_the_last_few_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEBUG_AUDIO_DIR", tmp_path)
    for day in range(1, 10):
        for part in ("tutor.wav", "mic.wav", "timeline.txt"):
            (tmp_path / f"2020010{day}-120000-{part}").write_bytes(b"")
    AudioTape("x").close()
    stamps = sorted({p.name[:15] for p in tmp_path.iterdir()})
    assert len(stamps) == AudioTape.KEEP
    assert "20200101-120000" not in stamps
