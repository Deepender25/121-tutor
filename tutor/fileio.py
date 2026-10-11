"""Small JSON files (progress, document metadata) written so they're never half-written or lost on Windows."""

import json
import os
import tempfile
import time
from pathlib import Path


def write_json(path: Path, data) -> None:
    """Write a temp file next to `path`, then swap it in: a crash mid-write never leaves a half-written file."""
    fd, tmp = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def replace(src: str, dst: Path, attempts: int = 8) -> None:
    """os.replace, retried briefly on Windows: antivirus scanners and the search indexer open freshly written
    files for a moment, and replacing a file that's open elsewhere fails with WinError 32 / 5."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))  # 50 ms, 100 ms, ... about 1.4 s in all
