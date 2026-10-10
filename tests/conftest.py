import os
import tempfile

# Isolate test data from the real library. Must run before anything imports tutor.config.
os.environ["VOICE_TUTOR_DATA"] = tempfile.mkdtemp(prefix="voice-tutor-test-")
