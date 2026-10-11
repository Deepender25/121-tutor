"""The embedding model runs in its own process, so it can never crash the app.

A native crash inside PyTorch (a processor missing instructions the maths kernels use, memory running out,
a clashing OpenMP runtime) kills whichever process it happens in, with no Python error to catch. With the
model in a worker process the API server survives it: it explains what happened (job error + logs),
restarts the model in a safe mode that only uses widely supported CPU instructions and fewer threads, and
retries. On start the worker logs the computer's details and runs a realistic warm-up, so problems show up
immediately rather than in the middle of indexing a PDF.

EmbedderClient has the same interface as Embedder (model_name, dim, count_tokens, embed_documents,
embed_query); the server uses it, the command-line tools keep using Embedder directly.
"""

import faulthandler
import logging
import multiprocessing as mp
import os
import platform
import threading
import time
from datetime import datetime
from typing import Callable

import numpy as np

from tutor import config
from tutor.retrieval.embedder import DOCUMENT_PROMPT, QUERY_PROMPT

log = logging.getLogger("embedder")
CRASH_LOG = config.PROJECT_ROOT / "logs" / "embedder-crash.log"

# Safe mode: no AVX2/AVX-512 kernels (ATen, oneDNN, MKL), oneDNN off, at most 4 threads.
SAFE_ENV = {
    "ATEN_CPU_CAPABILITY": "default",
    "ONEDNN_MAX_CPU_ISA": "SSE41",
    "DNNL_MAX_CPU_ISA": "SSE41",
    "MKL_ENABLE_INSTRUCTIONS": "SSE4_2",
}
DEFAULT_BATCH = 8
# Warm-up input about as long as a real chunk, so it exercises the same kernels and memory as indexing.
WARMUP_TEXT = "title: warm-up | text: " + " ".join(["The quick brown fox explains natural language processing."] * 60)

# Windows exit codes (NTSTATUS) of crashed processes.
ILLEGAL_INSTRUCTION = 0xC000001D
NO_MEMORY_CODES = {0xC0000017, 0xC000012D}  # STATUS_NO_MEMORY, STATUS_COMMITMENT_LIMIT


class EmbeddingCrash(RuntimeError):
    """The model process died (native crash)."""


class EmbeddingUnavailable(RuntimeError):
    """The model couldn't be loaded (a normal error, e.g. no internet for the first download)."""


# ---------------------------------------------------------------- computer details

def memory_mb() -> tuple[int, int] | None:
    """(total, available) physical memory in MB."""
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys // 2**20, status.ullAvailPhys // 2**20
            return None
        pages, size, avail = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE"), os.sysconf("SC_AVPHYS_PAGES")
        return pages * size // 2**20, avail * size // 2**20
    except Exception:
        return None


def cpu_name() -> str:
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def describe_crash(exitcode: int | None, info: dict) -> str:
    """A plain explanation of why the model process died."""
    code = (exitcode or 0) & 0xFFFFFFFF
    mem = memory_mb()
    free = f"{mem[1]:,} MB free of {mem[0]:,} MB" if mem else "unknown free memory"
    cpu = info.get("cpu") or cpu_name()
    if code == ILLEGAL_INSTRUCTION:
        reason = (f"this computer's processor ({cpu}) doesn't support instructions the AI model's maths library "
                  "uses (it stopped with an illegal-instruction error)")
    elif code in NO_MEMORY_CODES or (mem and mem[1] < 1200):
        reason = f"the computer ran out of memory ({free}). Close other programs, browser tabs especially, and try again"
    elif code == 3:
        reason = "the AI model's maths library aborted (usually a conflicting copy of the OpenMP runtime)"
    else:
        reason = f"the AI model process crashed (exit code 0x{code:08X}; {free})"
    return f"The embedding model stopped working: {reason}. Details: logs\\embedder-crash.log"


# ---------------------------------------------------------------- the worker process

def _worker_main(conn, model_name: str, dim: int, safe: bool) -> None:
    # Before torch is imported: a second OpenMP runtime must not abort the process (Intel's OMP error #15).
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    if safe:
        os.environ.update(SAFE_ENV)
    CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
    crash_file = open(CRASH_LOG, "a", encoding="utf-8")  # noqa: SIM115 - must stay open for faulthandler
    crash_file.write(f"\n===== embedding worker started {datetime.now():%Y-%m-%d %H:%M:%S} (pid {os.getpid()}, "
                     f"{'safe mode' if safe else 'normal mode'}) =====\n")
    crash_file.flush()
    faulthandler.enable(crash_file, all_threads=True)  # a native crash writes the Python stack here

    try:
        import torch

        cores = os.cpu_count() or 2
        threads = max(1, min(4 if safe else 8, cores // 2 if cores > 4 else cores))
        torch.set_num_threads(threads)
        if safe:
            torch.backends.mkldnn.enabled = False
        mem = memory_mb()
        info = {
            "cpu": cpu_name(),
            "cores": cores,
            "threads": threads,
            "ram_total_mb": mem[0] if mem else None,
            "ram_free_mb": mem[1] if mem else None,
            "torch": torch.__version__,
            "cpu_capability": torch.backends.cpu.get_cpu_capability(),
            "safe": safe,
        }
        crash_file.write(f"{info}\n")
        crash_file.flush()
        conn.send(("info", info))

        from tutor.retrieval.embedder import Embedder

        model = Embedder(model_name, dim)
        model._encode([WARMUP_TEXT] * 4, batch_size=4)
        conn.send(("ready", info))
    except BaseException as e:  # a Python error while loading: report it, don't retry in safe mode
        try:
            conn.send(("failed", f"{type(e).__name__}: {e}"))
        except Exception:
            pass
        return

    while True:
        try:
            request = conn.recv()
        except (EOFError, OSError):
            return  # the server has gone away
        if request[0] == "stop":
            return
        _, texts, batch = request
        try:
            conn.send(("ok", model._encode(texts, batch)))
        except MemoryError:
            conn.send(("oom", "MemoryError"))
        except RuntimeError as e:
            conn.send(("oom" if "memory" in str(e).lower() else "error", f"{type(e).__name__}: {e}"))
        except Exception as e:
            conn.send(("error", f"{type(e).__name__}: {e}"))


# ---------------------------------------------------------------- the client used by the server

class EmbedderClient:
    mode = "offline"
    label = "Offline · EmbeddingGemma, on this computer"

    def __init__(self, model_name: str = config.OFFLINE_EMBEDDING_MODEL, dim: int = config.EMBEDDING_DIM):
        self.model_name = model_name
        self.dim = dim
        self.safe = False
        self.info: dict = {}
        self._proc = None
        self._conn = None
        self._lock = threading.Lock()
        # The tokenizer (for chunking) lives here, loaded while the worker starts the model.
        self.tokenizer = None
        tokenizer_error: list[BaseException] = []

        def load_tokenizer() -> None:
            from transformers import AutoTokenizer

            try:
                try:
                    self.tokenizer = AutoTokenizer.from_pretrained(model_name, local_files_only=True)
                except Exception:  # first run: not downloaded yet
                    self.tokenizer = AutoTokenizer.from_pretrained(model_name)
            except BaseException as e:
                tokenizer_error.append(e)

        loader = threading.Thread(target=load_tokenizer, name="tokenizer-load", daemon=True)
        loader.start()
        self._start()
        loader.join()
        if tokenizer_error:
            raise EmbeddingUnavailable(f"Couldn't load the tokenizer: {tokenizer_error[0]}")

    # ---- process management

    def _spawn(self, safe: bool):
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=_worker_main, args=(child, self.model_name, self.dim, safe),
                           name="embedding-worker", daemon=True)
        proc.start()
        child.close()
        return proc, parent

    def _receive(self, proc, conn):
        """Next message from the worker; EmbeddingCrash if it dies first."""
        while True:
            try:
                if conn.poll(0.5):
                    return conn.recv()
            except (EOFError, OSError):  # the pipe broke: the worker is dying (Windows: BrokenPipeError)
                time.sleep(0.1)
            if not proc.is_alive():
                proc.join(timeout=5)
                raise EmbeddingCrash(describe_crash(proc.exitcode, self.info))

    def _start(self) -> None:
        """Start the model: normally first; after a crash, in safe mode."""
        attempts = [True] if self.safe else [False, True]
        crash = None
        for safe in attempts:
            started = time.perf_counter()
            proc, conn = self._spawn(safe)
            try:
                while True:
                    kind, payload = self._receive(proc, conn)
                    if kind == "info":
                        self.info = payload
                        ram = f"{payload['ram_free_mb']:,} MB free of {payload['ram_total_mb']:,} MB" if payload["ram_total_mb"] else "unknown"
                        log.info("Embedding worker (%s): %s, %d cores (using %d threads), RAM %s, torch %s, CPU kernels %s",
                                 "safe mode" if safe else "normal mode", payload["cpu"], payload["cores"],
                                 payload["threads"], ram, payload["torch"], payload["cpu_capability"])
                    elif kind == "ready":
                        break
                    elif kind == "failed":
                        proc.join(timeout=5)
                        raise EmbeddingUnavailable(payload)
            except EmbeddingCrash as e:
                crash = e
                log.error("%s%s", e, "" if safe else " Retrying in safe mode.")
                continue
            self._proc, self._conn, self.safe = proc, conn, safe
            log.info("Embedding model ready in %.1fs%s", time.perf_counter() - started,
                     " (safe mode: slower, but avoids the processor features that crashed)" if safe else "")
            return
        raise crash

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.send(("stop",))
                except Exception:
                    pass
            if self._proc is not None:
                self._proc.join(timeout=5)
                if self._proc.is_alive():
                    self._proc.terminate()
            self._proc = self._conn = None

    # ---- encoding

    def _encode_once(self, texts: list[str], batch: int) -> np.ndarray:
        if self._proc is None or not self._proc.is_alive():
            self._start()
        try:
            self._conn.send(("encode", texts, batch))
        except OSError:  # it died between requests
            self._proc.join(timeout=5)
            raise EmbeddingCrash(describe_crash(self._proc.exitcode, self.info)) from None
        kind, payload = self._receive(self._proc, self._conn)
        if kind == "ok":
            return payload
        if kind == "oom":
            raise MemoryError(payload)
        raise RuntimeError(payload)

    def _encode(self, texts: list[str], batch_size: int = DEFAULT_BATCH) -> np.ndarray:
        with self._lock:
            batch = batch_size
            while True:
                try:
                    return self._encode_once(texts, batch)
                except MemoryError:
                    if batch == 1:
                        raise RuntimeError("Not enough memory to run the embedding model. Close other programs "
                                           "(browser tabs especially) and try again.") from None
                    batch = max(1, batch // 2)
                    log.warning("Embedding ran short of memory; retrying %d at a time", batch)
                except EmbeddingCrash as e:
                    if self.safe:
                        self._proc = self._conn = None
                        raise
                    log.error("%s Restarting it in safe mode and retrying.", e)
                    self.safe = True
                    self._start()  # raises if safe mode crashes too
                    batch = 1

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def embed_documents(
        self,
        items: list[tuple[str, str]],
        show_progress: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
        batch_size: int = DEFAULT_BATCH,
    ) -> np.ndarray:
        """items: (title, text) pairs. Sent in batches (so live searches aren't starved and progress shows)."""
        texts = [DOCUMENT_PROMPT.format(title or "none", text) for title, text in items]
        parts = []
        for start in range(0, len(texts), batch_size):
            parts.append(self._encode(texts[start:start + batch_size], batch_size))
            if on_progress:
                on_progress(min(start + batch_size, len(texts)), len(texts))
        return np.concatenate(parts) if parts else np.zeros((0, self.dim), np.float32)

    def embed_query(self, query: str) -> np.ndarray:
        return self._encode([QUERY_PROMPT.format(query)], 1)[0]
