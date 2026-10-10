"""Voice Tutor server. run.bat starts this in its own terminal window.

    python app.py                    start the server and open the browser when it's ready
    python app.py file.pdf           also create a notebook for this PDF and open it
    python app.py --no-browser       start without opening a browser
    python app.py --open-if-running  if the server is already running, open the browser and exit 0;
                                     otherwise exit 3 (used by run.bat)
"""

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from tutor import config  # noqa: E402

BASE = f"http://{config.HOST}:{config.PORT}"
log = logging.getLogger("app")


def is_running() -> bool:
    try:
        return httpx.get(f"{BASE}/api/health", timeout=1.5).json().get("app") == "voice-tutor"
    except Exception:
        return False


def port_taken() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((config.HOST, config.PORT)) == 0


def set_console_title(title: str) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def in_venv() -> bool:
    return sys.prefix != sys.base_prefix


def notebook_for_pdf(pdf: Path) -> str:
    """Create a notebook named after the PDF, upload it, and return the notebook's URL."""
    with httpx.Client(base_url=BASE, timeout=120) as client:
        nb = client.post("/api/notebooks", json={"name": pdf.stem.replace("_", " ")}).raise_for_status().json()
        with pdf.open("rb") as f:
            client.post(f"/api/notebooks/{nb['id']}/sources",
                        files={"files": (pdf.name, f, "application/pdf")}).raise_for_status()
        return f"{BASE}/#/nb/{nb['id']}"


def open_browser(pdf: Path | None, wait: bool) -> None:
    if wait:
        deadline = time.monotonic() + 90
        while not is_running():
            if time.monotonic() > deadline:
                log.error("The server didn't come up within 90 s; not opening the browser.")
                return
            time.sleep(0.3)
    url = BASE + "/"
    if pdf is not None:
        try:
            url = notebook_for_pdf(pdf)
            log.info("Created a notebook for %s", pdf.name)
        except Exception as e:
            log.error("Couldn't add %s: %s", pdf.name, e)
    log.info("Opening %s in your browser", url)
    webbrowser.open(url)


def parse_pdf_arg(raw: Path | None) -> Path | None:
    if raw is None:
        return None
    pdf = Path(str(raw).strip('"'))
    if pdf.is_file() and pdf.suffix.lower() == ".pdf":
        return pdf
    print(f"Ignoring {pdf}: not a PDF file.")
    return None


def banner() -> None:
    from tutor import web
    from tutor.logs import MAIN_LOG

    key = "set" if os.environ.get("GEMINI_API_KEY", "").strip() else "MISSING (voice disabled; add it to .env)"
    rows = [
        ("Open", BASE),
        ("Python", f"{sys.executable}  ({'virtual env' if in_venv() else 'NOT a virtual env'})"),
        ("Data", str(config.DATA_ROOT)),
        ("Logs", str(MAIN_LOG)),
        ("Gemini key", key),
        ("Voice model", config.LIVE_MODEL),
        ("Web search", "Tavily" if web.provider() == "tavily" else "DuckDuckGo"),
    ]
    width = max(len(k) for k, _ in rows)
    print("\n  Voice Tutor server")
    print("  " + "-" * 60)
    for k, v in rows:
        print(f"  {k:<{width}}  {v}")
    print("  " + "-" * 60)
    print("  Keep this window open while you use the app. Press Ctrl+C or close it to stop.\n", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", type=Path, help="optional PDF to open in a new notebook")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--open-if-running", action="store_true")
    ap.add_argument("--debug", action="store_true", help="show DEBUG logs in the console too")
    args = ap.parse_args()
    pdf = parse_pdf_arg(args.pdf)

    if args.open_if_running:
        if not is_running():
            return 3
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        print(f"Voice Tutor is already running at {BASE}")
        open_browser(pdf, wait=False)
        return 0

    from tutor.logs import disable_quick_edit, setup_logging

    setup_logging(logging.DEBUG if args.debug else logging.INFO)
    if is_running():
        log.info("Voice Tutor is already running at %s", BASE)
        if not args.no_browser:
            open_browser(pdf, wait=False)
        return 0
    if port_taken():
        log.error("Port %d is in use by another program. Set VOICE_TUTOR_PORT=<free port> in .env and start again.",
                  config.PORT)
        return 1

    set_console_title("Voice Tutor - Server")
    disable_quick_edit()
    banner()
    if not in_venv():
        log.warning("Not running inside a virtual environment (%s). Start the app with run.bat.", sys.executable)
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        log.warning("GEMINI_API_KEY is not set in .env: everything works except voice.")
    if not args.no_browser:
        threading.Thread(target=open_browser, args=(pdf, True), daemon=True, name="open-browser").start()

    import uvicorn

    from tutor.server.app import app

    try:
        # log_config=None: uvicorn's loggers flow into our console + file handlers.
        uvicorn.run(app, host=config.HOST, port=config.PORT, log_config=None, access_log=False,
                    log_level="info")
    except KeyboardInterrupt:
        pass
    log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
