"""Settings from .env: a typo stops the app with a sentence saying what to fix, not a traceback."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_config(tmp_path, **env) -> subprocess.CompletedProcess:
    code = "import tutor.config as c; print(c.VAD_START_SENSITIVITY, c.VAD_END_SENSITIVITY, c.VAD_PREFIX_MS)"
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "VOICE_TUTOR_DATA": str(tmp_path), **env})


def test_a_malformed_number_is_a_readable_error(tmp_path):
    result = run_config(tmp_path, VOICE_TUTOR_VAD_SILENCE_MS="1s")
    assert result.returncode != 0
    assert "VOICE_TUTOR_VAD_SILENCE_MS must be a whole number (it is '1s'). Fix it in .env." in result.stderr
    assert "Traceback" not in result.stderr


def test_an_unknown_choice_is_a_readable_error(tmp_path):
    result = run_config(tmp_path, VOICE_TUTOR_VAD_START_SENSITIVITY="medium")
    assert result.returncode != 0
    assert "VOICE_TUTOR_VAD_START_SENSITIVITY must be one of low, high" in result.stderr
    assert "Traceback" not in result.stderr


def test_sensitivity_is_case_insensitive_and_defaults_stay(tmp_path):
    result = run_config(tmp_path, VOICE_TUTOR_VAD_START_SENSITIVITY=" Low ", VOICE_TUTOR_VAD_END_SENSITIVITY="HIGH",
                        VOICE_TUTOR_VAD_PREFIX_MS="")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["low", "high", "300"]  # empty means the default (prefix 300 ms)

