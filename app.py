"""Voice Tutor API server (Python: voice, retrieval, PDFs). run.bat starts this in its own terminal window,
"Voice Tutor - API". The UI (ui.py, Next.js) is what you open in the browser; it forwards /api here.

    python app.py            start the API server
    python app.py --check    exit 0 if the API server is already running, otherwise 3 (used by run.bat)
    python app.py --debug    also show DEBUG logs in this window
"""

import argparse
import faulthandler
import logging
import os
import socket
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from tutor import config  # noqa: E402

log = logging.getLogger("app")


def is_running() -> bool:
    try:
        return httpx.get(f"{config.API_URL}/api/health", timeout=1.5).json().get("app") == "voice-tutor"
    except Exception:
        return False


def port_taken() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((config.HOST, config.API_PORT)) == 0


def set_console_title(title: str) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def in_venv() -> bool:
    return sys.prefix != sys.base_prefix


def banner() -> None:
    from tutor import web
    from tutor.logs import MAIN_LOG

    key = "set" if os.environ.get("GEMINI_API_KEY", "").strip() else "MISSING (voice disabled; add it to .env)"
    rows = [
        ("Open", f"{config.UI_URL}  (the UI window serves the app)"),
        ("API", config.API_URL),
        ("Python", f"{sys.executable}  ({'virtual env' if in_venv() else 'NOT a virtual env'})"),
        ("Data", str(config.DATA_ROOT)),
        ("Logs", str(MAIN_LOG)),
        ("Gemini key", key),
        ("Voice model", config.LIVE_MODEL),
        ("Web search", "Tavily" if web.provider() == "tavily" else "DuckDuckGo"),
    ]
    width = max(len(k) for k, _ in rows)
    print("\n  Voice Tutor API server")
    print("  " + "-" * 60)
    for k, v in rows:
        print(f"  {k:<{width}}  {v}")
    print("  " + "-" * 60)
    print("  Keep this window open while you use the app. Press Ctrl+C or close it to stop.\n", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit 0 if the API server is running, otherwise 3")
    ap.add_argument("--debug", action="store_true", help="show DEBUG logs in the console too")
    args = ap.parse_args()

    if args.check:
        return 0 if is_running() else 3

    from tutor.logs import LOG_DIR, disable_quick_edit, setup_logging

    setup_logging(logging.DEBUG if args.debug else logging.INFO)
    # A native crash (not a Python error) would otherwise leave no trace: write its stack to a file.
    crash_log = open(LOG_DIR / "crash.log", "a", encoding="utf-8")  # noqa: SIM115 - kept open on purpose
    faulthandler.enable(crash_log, all_threads=True)
    if is_running():
        log.info("The Voice Tutor API is already running at %s", config.API_URL)
        return 0
    if port_taken():
        log.error("Port %d is in use by another program. Set VOICE_TUTOR_API_PORT=<free port> in .env and start again.",
                  config.API_PORT)
        return 1

    set_console_title("Voice Tutor - API")
    disable_quick_edit()
    banner()
    if not in_venv():
        log.warning("Not running inside a virtual environment (%s). Start the app with run.bat.", sys.executable)
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        log.warning("GEMINI_API_KEY is not set in .env: everything works except voice.")

    import uvicorn

    from tutor.server.app import app

    try:
        # log_config=None: uvicorn's loggers flow into our console + file handlers.
        uvicorn.run(app, host=config.HOST, port=config.API_PORT, log_config=None, access_log=False,
                    log_level="info")
    except KeyboardInterrupt:
        pass
    log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
