"""Voice Tutor UI server (Next.js, in frontend/). run.bat starts it in its own terminal window,
"Voice Tutor - UI". This is the address you open; it forwards /api to the Python API server (app.py).

    python ui.py build                 install the UI's packages and build it, if anything changed
    python ui.py serve [file.pdf]      run the UI; opens the browser once the UI and the API are ready
    python ui.py serve --no-browser
    python ui.py serve --dev           development server with hot reload (no build needed)
    python ui.py check                 exit 0 if the UI server is already running, otherwise 3
    python ui.py open [file.pdf]       open the app in the browser (add --wait to wait for both servers)

Needs Node.js 20.9 or newer (https://nodejs.org, LTS).
"""

import argparse
import hashlib
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):  # not when something (a test runner) has replaced the stream
        _stream.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from tutor import config  # noqa: E402

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
NEXT_BIN = FRONTEND / "node_modules" / "next" / "dist" / "bin" / "next"
BUILD_ID = FRONTEND / ".next" / "BUILD_ID"
BUILD_STAMP = FRONTEND / ".next" / "vt-build-stamp"
DEPS_STAMP = FRONTEND / "node_modules" / ".vt-deps-stamp"
SETUP_LOG = ROOT / "logs" / "setup.log"
MIN_NODE = (20, 9)
NODE_HELP = "Install Node.js 20.9 or newer (the LTS version) from https://nodejs.org, then run run.bat again."
# Ignored when deciding whether the UI must be rebuilt: dependencies, build output, generated files.
SKIP_DIRS = {"node_modules", ".next"}
SKIP_FILES = {"next-env.d.ts", "tsconfig.tsbuildinfo"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

log = logging.getLogger("ui")


# ---------------------------------------------------------------- helpers

def say(text: str = "") -> None:
    print(f"      {text}" if text else "", flush=True)


def setup_log(text: str) -> None:
    SETUP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SETUP_LOG.open("a", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def find_node() -> tuple[str | None, str]:
    node = shutil.which("node")
    if not node:
        return None, ""
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return None, ""
    return node, version


def node_ok(version: str) -> bool:
    m = re.match(r"v(\d+)\.(\d+)", version)
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= MIN_NODE


def npm_command(node: str) -> list[str] | None:
    # Run npm's own script with node (avoids going through npm.cmd); fall back to npm on PATH.
    cli = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if cli.exists():
        return [node, str(cli)]
    npm = shutil.which("npm")
    return [npm] if npm else None


def ui_env() -> dict[str, str]:
    return {**os.environ, "VOICE_TUTOR_HOST": config.HOST, "VOICE_TUTOR_API_PORT": str(config.API_PORT),
            "NEXT_TELEMETRY_DISABLED": "1"}


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def sources_hash() -> str:
    """Changes whenever anything that goes into the build changes (code, config, assets, API address)."""
    digest = hashlib.sha256(f"{config.HOST}:{config.API_PORT}".encode())
    for path in sorted(FRONTEND.rglob("*")):
        rel = path.relative_to(FRONTEND)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file() or path.name in SKIP_FILES:
            continue
        digest.update(rel.as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run_logged(cmd: list[str], title: str) -> int:
    """Run a command with its output in logs/setup.log (shown on screen only if it fails)."""
    setup_log(f"--- {title}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=FRONTEND, env=ui_env(), capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = ANSI.sub("", (proc.stdout or "") + (proc.stderr or ""))
    setup_log(output)
    if proc.returncode != 0:
        say(f"[error] {title} failed. Last lines (full output: logs\\setup.log):")
        for line in output.strip().splitlines()[-25:]:
            say("  " + line)
    return proc.returncode


def is_up(url: str, app: str) -> bool:
    try:
        return httpx.get(url, timeout=1.5).json().get("app") == app
    except Exception:
        return False


def ui_running() -> bool:
    return is_up(f"{config.UI_URL}/ui-health", "voice-tutor-ui")


def api_running() -> bool:
    return is_up(f"{config.API_URL}/api/health", "voice-tutor")


def port_taken(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((config.HOST, port)) == 0


def set_console_title(title: str) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


# ---------------------------------------------------------------- build

def ui_window_still_open() -> int:
    """Rebuilding replaces the files the running UI server is serving from (frontend/.next): it would break."""
    say('[error] The interface changed, but the "Voice Tutor - UI" window from an earlier start is still running.')
    say("Close that window, then run run.bat again.")
    return 1


def build() -> int:
    node, version = find_node()
    if not node:
        say("[error] Node.js was not found. The interface needs it to run.")
        say(NODE_HELP)
        return 1
    if not node_ok(version):
        say(f"[error] Node.js {version} is too old (need 20.9 or newer).")
        say(NODE_HELP)
        return 1
    npm = npm_command(node)
    if not npm:
        say("[error] npm was not found next to Node.js. Reinstall Node.js from https://nodejs.org.")
        return 1

    lock = file_hash(FRONTEND / "package-lock.json")
    if not NEXT_BIN.exists() or not DEPS_STAMP.exists() or DEPS_STAMP.read_text(encoding="utf-8") != lock:
        if ui_running():
            return ui_window_still_open()
        say(f"Installing the interface's packages (Node.js {version}). The first time takes a minute.")
        if run_logged(npm + ["ci", "--no-audit", "--no-fund"], "npm ci") != 0:
            say("Common fixes: check your internet connection; pause OneDrive syncing; run this file again.")
            return 1
        DEPS_STAMP.write_text(lock, encoding="utf-8")

    stamp = sources_hash()
    if BUILD_ID.exists() and BUILD_STAMP.exists() and BUILD_STAMP.read_text(encoding="utf-8") == stamp:
        say(f"Interface is up to date (Node.js {version})")
        return 0
    if ui_running():
        return ui_window_still_open()
    say("Building the interface. This takes about a minute the first time.")
    if run_logged([node, str(NEXT_BIN), "build"], "next build") != 0:
        return 1
    BUILD_STAMP.write_text(sources_hash(), encoding="utf-8")  # after the build: it may touch tsconfig.json
    say("Interface built")
    return 0


# ---------------------------------------------------------------- browser

def notebook_for_pdf(pdf: Path) -> str:
    """Create a notebook named after the PDF, upload it, and return the notebook's address."""
    with httpx.Client(base_url=config.API_URL, timeout=120) as client:
        nb = client.post("/api/notebooks", json={"name": pdf.stem.replace("_", " ")}).raise_for_status().json()
        with pdf.open("rb") as f:
            client.post(f"/api/notebooks/{nb['id']}/sources",
                        files={"files": (pdf.name, f, "application/pdf")}).raise_for_status()
        return f"{config.UI_URL}/notebooks/{nb['id']}"


def open_app(pdf: Path | None, wait: bool) -> bool:
    if wait:
        deadline = time.monotonic() + 180  # the API's first start also loads the embedding model
        while not (ui_running() and api_running()):
            if time.monotonic() > deadline:
                log.error("The app didn't come up within 3 minutes; not opening the browser. See the API window.")
                return False
            time.sleep(0.4)
    url = config.UI_URL + "/"
    if pdf is not None:
        try:
            url = notebook_for_pdf(pdf)
            log.info("Created a notebook for %s", pdf.name)
        except Exception as e:
            log.error("Couldn't add %s: %s", pdf.name, e)
    log.info("Opening %s in your browser", url)
    webbrowser.open(url)
    return True


def parse_pdf_arg(raw: str | None) -> Path | None:
    if not raw:
        return None
    pdf = Path(str(raw).strip('"'))
    if pdf.is_file() and pdf.suffix.lower() == ".pdf":
        return pdf
    print(f"Ignoring {pdf}: not a PDF file.")
    return None


# ---------------------------------------------------------------- serve

def pump(proc: subprocess.Popen) -> None:
    """Next.js output -> this window and logs/ui.log, with sensible levels."""
    next_log = logging.getLogger("next")
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = ANSI.sub("", raw).rstrip()
        if not line.strip():
            continue
        if "Ready in" in line:
            set_console_title("Voice Tutor - UI")  # Next.js renames the window to "next-server (vX)" on start
        lowered = line.lower()
        if "⨯" in line or "error" in lowered:
            next_log.error(line)
        elif "⚠" in line or "warn" in lowered:
            next_log.warning(line)
        else:
            next_log.info(line)


def serve(pdf: Path | None, no_browser: bool, dev: bool) -> int:
    from tutor.logs import UI_LOG, disable_quick_edit, setup_logging

    setup_logging(logging.INFO, main_log=UI_LOG, error_log=None)
    if ui_running():
        log.info("The Voice Tutor UI is already running at %s", config.UI_URL)
        if not no_browser:
            open_app(pdf, wait=True)
        return 0
    if port_taken(config.PORT):
        log.error("Port %d is in use by another program. Set VOICE_TUTOR_PORT=<free port> in .env and start again.", config.PORT)
        return 1
    node, version = find_node()
    if not node or not node_ok(version):
        log.error("Node.js 20.9+ is required. %s", NODE_HELP)
        return 1
    if not NEXT_BIN.exists() or (not dev and not BUILD_ID.exists()):
        log.error("The interface isn't built yet. Start the app with run.bat (or run: python ui.py build).")
        return 1

    set_console_title("Voice Tutor - UI")
    disable_quick_edit()
    rows = [("Open", config.UI_URL), ("API", f"{config.API_URL}  (the API window)"), ("Node.js", version),
            ("Mode", "development (hot reload)" if dev else "production"), ("Logs", str(UI_LOG))]
    width = max(len(k) for k, _ in rows)
    print("\n  Voice Tutor UI server")
    print("  " + "-" * 60)
    for k, v in rows:
        print(f"  {k:<{width}}  {v}")
    print("  " + "-" * 60)
    print("  Keep this window open while you use the app. Press Ctrl+C or close it to stop.\n", flush=True)

    cmd = [node, str(NEXT_BIN), "dev" if dev else "start", "--hostname", config.HOST, "--port", str(config.PORT)]
    proc = subprocess.Popen(cmd, cwd=FRONTEND, env=ui_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    threading.Thread(target=pump, args=(proc,), daemon=True, name="next-output").start()
    if not no_browser:
        threading.Thread(target=open_app, args=(pdf, True), daemon=True, name="open-browser").start()
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = 0
    log.info("Stopped.")
    return code


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["build", "serve", "check", "open"])
    ap.add_argument("pdf", nargs="?", help="optional PDF to open in a new notebook (serve, open)")
    ap.add_argument("--no-browser", action="store_true", help="serve: don't open the browser")
    ap.add_argument("--dev", action="store_true", help="serve: development server with hot reload")
    ap.add_argument("--wait", action="store_true", help="open: wait for both servers first")
    args = ap.parse_args()

    if args.command == "build":
        return build()
    if args.command == "check":
        return 0 if ui_running() else 3
    if args.command == "open":
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return 0 if open_app(parse_pdf_arg(args.pdf), wait=args.wait) else 1
    return serve(parse_pdf_arg(args.pdf), args.no_browser, args.dev)


if __name__ == "__main__":
    sys.exit(main())
