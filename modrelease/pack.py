"""Reproducible release archives and the checks that run before one is written."""
from __future__ import annotations

import getpass
import hashlib
import io
import os
import re
import socket
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

JUNK = {"thumbs.db", "desktop.ini", ".ds_store"}
JUNK_DIRS = {"__MACOSX", ".git", "__pycache__"}
ARCHIVES = {".zip", ".7z", ".rar", ".tar", ".gz", ".xz"}
EXECUTABLES = {".exe", ".dll", ".bat", ".cmd", ".ps1", ".scr", ".msi", ".jar"}
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
# a user folder in a path: C:\Users\name, /home/name, /Users/name (also the doubled backslashes of JSON and C strings)
USER_PATH = re.compile(rb"(?i)(?:[a-z]:\\{1,2}users\\{1,2}|/home/|/users/)([^\\/\x00\s\"'<>|:*?]{2,64})")
SHARED_USER_DIRS = {b"public", b"default", b"all users", b"default user"}


@dataclass
class Finding:
    file: str
    what: str


def fixed_date() -> tuple[int, int, int, int, int, int]:
    """SOURCE_DATE_EPOCH if set (the usual reproducible-build variable), else 1980-01-01, the earliest zip date."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    return time.gmtime(int(epoch))[:6] if epoch else (1980, 1, 1, 0, 0, 0)


def personal_terms(extra: list[str] = ()) -> list[str]:
    """Strings that identify the machine or the person: login, computer name, git name and email, plus --term values."""
    terms = [getpass.getuser(), socket.gethostname(), Path.home().name, *extra]
    for key in ("user.email", "user.name"):
        try:
            out = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True, timeout=5).stdout
            terms.append(out.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    seen, result = set(), []
    for t in terms:
        if len(t) >= 3 and t.lower() not in seen:
            seen.add(t.lower())
            result.append(t)
    return result


def scan_bytes(name: str, data: bytes, terms: list[str]) -> list[Finding]:
    """Your own strings (UTF-8 and UTF-16, any case) in one file's bytes.
    shortcut: compressed data inside the file (pak, nested archives) is not unpacked, so text in it stays hidden."""
    found = []
    low = data.lower()
    for t in terms:
        for enc in ("utf-8", "utf-16-le"):
            if t.lower().encode(enc) in low:
                found.append(Finding(name, f"contains {t!r}" + (" (UTF-16)" if enc != "utf-8" else "")))
                break
    return found


def user_folders(name: str, data: bytes) -> list[Finding]:
    """Other people's user folders, e.g. the build machine path a library was compiled on. Usually harmless,
    but a folder from your second PC or old login would show up here too."""
    users = {m.group(1).lower() for m in USER_PATH.finditer(data)} - SHARED_USER_DIRS
    if not users:
        return []
    shown = ", ".join(u.decode("utf-8", "replace") for u in sorted(users))
    return [Finding(name, f"user folder path ({shown}): fine if it is a third-party build path, check it isn't yours")]


def image_metadata(name: str, data: bytes) -> list[Finding]:
    try:
        from PIL import Image
    except ImportError:
        return []
    try:
        with Image.open(io.BytesIO(data)) as im:
            keys = [k for k in im.info if k not in ("dpi", "jfif", "jfif_version", "jfif_unit", "jfif_density",
                                                    "gamma", "transparency", "duration", "loop", "background")]
            if im.getexif() or keys:
                return [Finding(name, "image metadata (EXIF or text chunks): run `modrelease clean` on it")]
    except OSError:
        pass
    return []


def collect(sources: list[Path]) -> list[tuple[Path, str]]:
    """(file, path inside the archive) for every file, sorted, junk left out. A folder's contents go to the archive root."""
    items = []
    for src in sources:
        if src.is_dir():
            for p in src.rglob("*"):
                if p.is_file() and p.name.lower() not in JUNK and not JUNK_DIRS & set(p.relative_to(src).parts):
                    items.append((p, p.relative_to(src).as_posix()))
        elif src.is_file():
            items.append((src, src.name))
        else:
            raise FileNotFoundError(src)
    items.sort(key=lambda x: x[1])
    dupes = {a for _, a in items if [b for _, b in items].count(a) > 1}
    if dupes:
        raise ValueError(f"two files would land on the same path: {sorted(dupes)}")
    return items


def check_file(name: str, data: bytes, terms: list[str]) -> tuple[list[Finding], list[Finding]]:
    """-> (problems that stop the release, notes about what Nexus will do with the file)."""
    ext = Path(name).suffix.lower()
    problems = scan_bytes(name, data, terms) + (image_metadata(name, data) if ext in IMAGES else [])
    notes = user_folders(name, data)
    if ext in ARCHIVES:
        notes.append(Finding(name, "archive inside the archive: Nexus quarantines nested archives"))
    if ext in EXECUTABLES:
        notes.append(Finding(name, "executable: Nexus quarantines these for a manual review (`modrelease appeal`)"))
    return problems, notes


def check(items: list[tuple[Path, str]], terms: list[str]) -> tuple[list[Finding], list[Finding]]:
    problems, notes = [], []
    for path, arc in items:
        pr, no = check_file(arc, path.read_bytes(), terms)
        problems += pr
        notes += no
    return problems, notes


def write_zip(items: list[tuple[Path, str]], out: Path, prefix: str = "") -> str:
    """Same files in, same bytes out: fixed dates and permissions, sorted entries, forward slashes. -> sha256."""
    prefix = prefix.strip("/\\").replace("\\", "/")
    date = fixed_date()
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as z:
        for path, arc in items:
            zi = zipfile.ZipInfo(f"{prefix}/{arc}" if prefix else arc, date)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zi.create_system = 0  # same header on every OS
            z.writestr(zi, path.read_bytes(), compresslevel=9)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_name(out.name + ".sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    return digest
