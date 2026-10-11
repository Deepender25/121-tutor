"""Server administration: the login account, sessions, and the data folder.

    python manage.py user create <email>      create the one account (asks for the password twice)
    python manage.py user password <email>    set a new password (signs out every session)
    python manage.py user show                show the account and its sessions
    python manage.py user delete <email>      delete the account (your notebooks and PDFs stay)
    python manage.py sessions end-all         sign out every browser
    python manage.py data info                what's stored and how big it is
    python manage.py data prune [--yes]       delete leftovers: PDFs no notebook uses, unfinished uploads, temp files
    python manage.py data backup <file.tar.gz>    consistent backup of everything except caches

Reads the same .env as the server (VOICE_TUTOR_DATA etc.), so run it where the server runs.
"""

import argparse
import getpass
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tutor import auth, config  # noqa: E402

CACHE_DIRS = ("pages",)  # inside each document folder: rendered page images, rebuilt on demand
TOP_LEVEL_CACHES = ("voice-previews",)


def _store() -> auth.AuthStore:
    return auth.AuthStore(config.AUTH_DB)


def _ask_password() -> str:
    while True:
        first = getpass.getpass(f"New password ({auth.MIN_PASSWORD}+ characters): ")
        if getpass.getpass("Repeat it: ") != first:
            print("They don't match; try again.")
            continue
        try:
            auth.check_password(first)
            return first
        except auth.AuthError as e:
            print(e)


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _mb(n: int) -> str:
    return f"{n / 2**20:,.1f} MB"


def _api_running() -> bool:
    try:
        import httpx

        return httpx.get(f"{config.API_URL}/api/health", timeout=1.5).json().get("app") == "voice-tutor"
    except Exception:
        return False


# ---- user / sessions

def user_create(email: str) -> int:
    store = _store()
    if store.users():
        print(f"An account already exists ({store.users()[0].email}). Use `user password` to change its password.")
        return 1
    auth.normalize_email(email)
    user = store.create_user(email, _ask_password())
    print(f"Account created: {user.email}")
    if not config.AUTH_ENABLED:
        print("Note: login is off in this .env (VOICE_TUTOR_AUTH). Set VOICE_TUTOR_AUTH=on and restart the API to use it.")
    return 0


def user_password(email: str) -> int:
    _store().set_password(email, _ask_password())
    print("Password changed. Every browser has been signed out.")
    return 0


def user_show() -> int:
    store = _store()
    users = store.users()
    print(f"Login: {'on' if config.AUTH_ENABLED else 'off'}   database: {config.AUTH_DB}")
    if not users:
        print("No account yet: python manage.py user create <email>")
        return 0
    print(f"Account: {users[0].email}")
    sessions = store.sessions()
    print(f"Signed-in sessions: {len(sessions)}")
    for s in sessions:
        print(f"  since {_when(s['created_at'])}, last used {_when(s['last_seen'])}, {s['ip'] or '?'}  "
              f"{(s['user_agent'] or '')[:70]}")
    return 0


def user_delete(email: str) -> int:
    if input(f"Delete the account {email}? Notebooks and PDFs are kept. Type 'delete' to confirm: ") != "delete":
        print("Cancelled.")
        return 1
    print("Deleted." if _store().delete_user(email) else f"No account for {email}.")
    return 0


def sessions_end_all() -> int:
    n = _store().end_all_sessions()
    print(f"Signed out {n} session{'s' if n != 1 else ''}.")
    return 0


# ---- data

def _referenced_docs() -> set[str]:
    refs = set()
    for f in config.NOTEBOOKS_DIR.glob("*.json"):
        try:
            refs.update(json.loads(f.read_text(encoding="utf-8")).get("sources", []))
        except (OSError, ValueError):
            pass
    return refs


def _leftovers() -> list[tuple[Path, str]]:
    """Things nothing points to any more: safe to delete while the API is stopped."""
    refs = _referenced_docs()
    docs_dir = config.DATA_ROOT / "docs"
    out = []
    for folder in sorted(docs_dir.iterdir()) if docs_dir.exists() else []:
        if folder.is_dir() and folder.name not in refs:
            out.append((folder, "indexed PDF that no notebook uses"))
    for pdf in sorted(config.FILES_DIR.glob("*.pdf")) if config.FILES_DIR.exists() else []:
        if pdf.stem not in refs:
            out.append((pdf, "uploaded PDF that no notebook uses"))
    for part in sorted(config.FILES_DIR.glob("upload-*.part")) if config.FILES_DIR.exists() else []:
        out.append((part, "unfinished upload"))
    for tmp in sorted(config.DATA_ROOT.rglob("*.tmp")):
        out.append((tmp, "temporary file"))
    return out


def data_info() -> int:
    root = config.DATA_ROOT
    print(f"Data folder: {root}")
    if not root.exists():
        print("  (empty)")
        return 0
    notebooks = list(config.NOTEBOOKS_DIR.glob("*.json"))
    docs = [d for d in (root / "docs").iterdir() if d.is_dir()] if (root / "docs").exists() else []
    cache = sum(_size(d / c) for d in docs for c in CACHE_DIRS if (d / c).exists())
    cache += sum(_size(root / c) for c in TOP_LEVEL_CACHES if (root / c).exists())
    print(f"  notebooks          {len(notebooks)}")
    print(f"  indexed PDFs       {len(docs)}  ({_mb(sum(_size(d) for d in docs))}, of which page-image cache {_mb(cache)})")
    print(f"  uploaded PDFs      {_mb(_size(config.FILES_DIR)) if config.FILES_DIR.exists() else '0 MB'}")
    print(f"  total              {_mb(_size(root))}")
    left = _leftovers()
    if left:
        print(f"  leftovers          {len(left)} item(s), {_mb(sum(_size(p) for p, _ in left))}: run `data prune` to remove")
    return 0


def data_prune(yes: bool) -> int:
    if _api_running():
        print("Stop the API first (an upload in progress would look like a leftover).")
        return 1
    left = _leftovers()
    if not left:
        print("Nothing to remove.")
        return 0
    for path, why in left:
        print(f"  {why}: {path} ({_mb(_size(path))})")
    if not yes and input(f"Delete these {len(left)} item(s)? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1
    for path, _ in left:
        shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
    print(f"Removed {len(left)} item(s).")
    return 0


def data_backup(dest: str) -> int:
    """tar.gz of the data folder without caches. The login database is copied with SQLite's backup API, so the
    copy is consistent even while the server runs."""
    root = config.DATA_ROOT
    out = Path(dest).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    def skip(info: tarfile.TarInfo):
        parts = Path(info.name).parts
        if len(parts) >= 3 and parts[0] == "docs" and parts[2] in CACHE_DIRS:
            return None
        if parts and (parts[0] in TOP_LEVEL_CACHES or info.name.endswith((".tmp", ".part"))):
            return None
        if parts and parts[0].startswith("auth.sqlite3"):  # added separately, consistently
            return None
        return info

    partial = out.with_name(out.name + ".partial")
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(partial, "w:gz") as tar:
        for child in sorted(root.iterdir()) if root.exists() else []:
            tar.add(child, arcname=child.name, filter=skip)
        if config.AUTH_DB.exists():
            copy = Path(tmp) / "auth.sqlite3"
            src = sqlite3.connect(str(config.AUTH_DB))
            dst = sqlite3.connect(str(copy))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            tar.add(copy, arcname="auth.sqlite3")
    partial.replace(out)
    print(f"Backup written: {out} ({_mb(out.stat().st_size)}, {time.perf_counter() - started:.1f}s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="area", required=True)
    u = sub.add_parser("user").add_subparsers(dest="cmd", required=True)
    u.add_parser("create").add_argument("email")
    u.add_parser("password").add_argument("email")
    u.add_parser("show")
    u.add_parser("delete").add_argument("email")
    s = sub.add_parser("sessions").add_subparsers(dest="cmd", required=True)
    s.add_parser("end-all")
    d = sub.add_parser("data").add_subparsers(dest="cmd", required=True)
    d.add_parser("info")
    d.add_parser("prune").add_argument("--yes", action="store_true", help="don't ask")
    d.add_parser("backup").add_argument("file")
    args = ap.parse_args()
    try:
        match (args.area, args.cmd):
            case ("user", "create"):
                return user_create(args.email)
            case ("user", "password"):
                return user_password(args.email)
            case ("user", "show"):
                return user_show()
            case ("user", "delete"):
                return user_delete(args.email)
            case ("sessions", "end-all"):
                return sessions_end_all()
            case ("data", "info"):
                return data_info()
            case ("data", "prune"):
                return data_prune(args.yes)
            case ("data", "backup"):
                return data_backup(args.file)
    except auth.AuthError as e:
        print(e)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
