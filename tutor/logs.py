"""Logging for the app: readable coloured console output + rotating log files in <project>/logs/.

    logs/voice-tutor.log   API server: everything at DEBUG (rotates at 5 MB, keeps 5)
    logs/errors.log        API server: warnings and errors only
    logs/ui.log            UI server (ui.py): its own file, so the two processes never rotate the same one
    logs/setup.log         run.bat steps, pip and npm output
"""

import atexit
import logging
import logging.handlers
import os
import queue
import sys

from tutor import config

LOG_DIR = config.LOG_DIR
MAIN_LOG = LOG_DIR / "voice-tutor.log"
ERROR_LOG = LOG_DIR / "errors.log"
UI_LOG = LOG_DIR / "ui.log"

_COLORS = {"DEBUG": "\x1b[90m", "INFO": "\x1b[0m", "WARNING": "\x1b[33m", "ERROR": "\x1b[31m", "CRITICAL": "\x1b[41m"}
_DIM, _RESET, _CYAN = "\x1b[90m", "\x1b[0m", "\x1b[36m"

# Third-party loggers that are noisy at INFO.
_QUIET = {
    "httpx": logging.WARNING, "httpcore": logging.WARNING, "urllib3": logging.WARNING,
    "huggingface_hub": logging.ERROR, "sentence_transformers": logging.WARNING, "transformers": logging.ERROR,
    "websockets": logging.WARNING, "multipart": logging.WARNING, "PIL": logging.WARNING,
    "google_genai": logging.WARNING, "google.genai": logging.WARNING, "filelock": logging.WARNING,
    "uvicorn.access": logging.WARNING, "watchfiles": logging.WARNING, "lancedb": logging.WARNING,
    "primp": logging.WARNING, "ddgs": logging.WARNING, "asyncio": logging.INFO,
}
# Friendlier names for third-party loggers in the output ("uvicorn.error" logs normal startup lines).
_RENAME = {"uvicorn.error": "uvicorn", "py.warnings": "warnings"}


class _Rename(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.name = _RENAME.get(record.name, record.name)
        return True


def _enable_windows_ansi() -> bool:
    if os.name != "nt":
        return sys.stdout.isatty()
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # not a console (redirected to a file)
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


def disable_quick_edit() -> None:
    """Clicking inside a Windows console with QuickEdit on freezes every write to it, which would stall a
    server that logs to that window. Turn QuickEdit off for this console (mouse selection still works
    via the window menu: Edit > Mark)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
            kernel32.SetConsoleMode(handle, (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS)
    except Exception:
        pass


class ConsoleFormatter(logging.Formatter):
    def __init__(self, color: bool):
        super().__init__(datefmt="%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        time = self.formatTime(record, self.datefmt)
        name = record.name.removeprefix("tutor.")[:10]
        level = record.levelname
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        if not self.color:
            return f"{time}  {level:<7} {name:<10}  {message}"
        tag = f"{_COLORS.get(level, '')}{level:<7}{_RESET}" if level != "INFO" else f"{level:<7}"
        return f"{_DIM}{time}{_RESET}  {tag} {_CYAN}{name:<10}{_RESET}  {_COLORS.get(level, '') if level != 'INFO' else ''}{message}{_RESET}"


class _DropConnectionResets(logging.Filter):
    """Windows' proactor event loop logs a harmless traceback whenever a browser drops a connection."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.exc_info and isinstance(record.exc_info[1], (ConnectionResetError, ConnectionAbortedError)))


class _NonBlockingQueueHandler(logging.handlers.QueueHandler):
    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:  # console far behind: drop console lines (files still get everything)
            pass


def setup_logging(console_level: int = logging.INFO, main_log=MAIN_LOG, error_log=ERROR_LOG) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # Console output goes through a queue + background thread: if the console window ever blocks (paused,
    # selection mode, slow terminal) the server keeps running and the log files keep being written.
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(ConsoleFormatter(color=_enable_windows_ansi()))
    console.addFilter(_DropConnectionResets())
    console.addFilter(_Rename())
    log_queue: queue.Queue = queue.Queue(maxsize=10_000)
    queue_handler = _NonBlockingQueueHandler(log_queue)
    queue_handler.setLevel(console_level)
    root.addHandler(queue_handler)
    listener = logging.handlers.QueueListener(log_queue, console, respect_handler_level=True)
    listener.start()
    atexit.register(listener.stop)

    file_fmt = logging.Formatter("%(asctime)s.%(msecs)03d  %(levelname)-7s %(name)-18s %(message)s", "%Y-%m-%d %H:%M:%S")
    main = logging.handlers.RotatingFileHandler(main_log, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    main.setLevel(logging.DEBUG)
    main.setFormatter(file_fmt)
    root.addHandler(main)
    handlers = [main, queue_handler]
    if error_log is not None:
        errors = logging.handlers.RotatingFileHandler(error_log, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        errors.setLevel(logging.WARNING)
        errors.setFormatter(file_fmt)
        root.addHandler(errors)
        handlers.append(errors)

    for handler in handlers:
        handler.addFilter(_DropConnectionResets())
        handler.addFilter(_Rename())
    for name, level in _QUIET.items():
        logging.getLogger(name).setLevel(level)
    logging.captureWarnings(True)
    logging.getLogger("py.warnings").setLevel(logging.ERROR)

    def excepthook(exc_type, exc, tb):  # crashes end up in the log file too
        if not issubclass(exc_type, KeyboardInterrupt):
            logging.getLogger("crash").critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = excepthook


def short(text: str, limit: int = 160) -> str:
    """One-line, truncated text for log messages."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
