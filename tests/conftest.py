import atexit
import os
import shutil
import tempfile

# Isolate test data and logs from the real library and the project's logs/ folder. Must run before anything imports
# tutor.config. Both folders are removed when the test run ends.
for _variable, _prefix in (("VOICE_TUTOR_DATA", "voice-tutor-test-"), ("VOICE_TUTOR_LOGS", "voice-tutor-test-logs-")):
    _folder = tempfile.mkdtemp(prefix=_prefix)
    os.environ[_variable] = _folder
    atexit.register(shutil.rmtree, _folder, ignore_errors=True)
