"""Reproducible release archives and the checks that run before one is written."""
from __future__ import annotations

import getpass
import hashlib
import io
import os
import re
import shutil
import socket
import subprocess
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

JUNK = {"thumbs.db", "desktop.ini", ".ds_store"}
JUNK_DIRS = {"__MACOSX", ".git", "__pycache__"}
ARCHIVES = {".zip", ".7z", ".rar", ".tar", ".gz", ".xz"}
EXECUTABLES = {".exe", ".dll", ".bat", ".cmd", ".ps1", ".scr", ".msi", ".jar"}
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
# a user folder in a path: C:\Users\name, /home/name, /Users/name (also the doubled backslashes of JSON and C strings)
USER_PATH = re.compile(rb"(?i)(?:[a-z]:\\{1,2}users\\{1,2}|/home/|/users/)([^\\/\x00\s\"'<>|:*?]{2,64})")
URL_BEFORE = re.compile(rb"://[^\s\"'<>]*$")  # "/users/x" inside a web address is not a folder
SHARED_USER_DIRS = {b"public", b"default", b"all users", b"default user"}
CHUNK = 16 << 20   # files are read in pieces, so a 4 GB pak needs about 32 MB of memory
TINY, SHORT = 4, 6  # "max" counts only inside a path, "jdoe" only as a whole word, see term_needles


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


def term_needles(terms: list[str]) -> list[tuple[str, list[bytes], re.Pattern | None]]:
    """What to look for, per term, in lowercased file bytes: UTF-8 and UTF-16, with case variants for non-Latin
    names (bytes.lower() folds only A-Z). Short terms are common words inside programs: up to 3 letters ("max")
    they only count as a path segment (and as a user folder, see check_file); 4-5 letters only as a whole word,
    checked by a regex that runs only where the fast search found the letters."""
    out = []
    for t in terms:
        needles, words = set(), []
        for v in {t, t.lower(), t.upper(), t.title()}:
            for enc in ("utf-8", "utf-16-le"):
                b = v.encode(enc)
                if len(t) < TINY:
                    needles |= {(s.encode(enc) + b + s.encode(enc)).lower() for s in ("\\", "/")}
                else:
                    needles.add(b.lower())
                    edge = rb"[A-Za-z0-9_]" + (rb"\x00" if enc != "utf-8" else b"")
                    words.append(b"(?<!" + edge + b")" + re.escape(b) + b"(?!" + edge + b")")
        word = re.compile(b"|".join(sorted(words)), re.IGNORECASE) if TINY <= len(t) < SHORT else None
        out.append((t, sorted(needles), word))
    return out


def scan_stream(f: BinaryIO, needles: list[tuple[str, list[bytes], re.Pattern | None]]) -> tuple[list[str], set[bytes]]:
    """-> (terms found, user folder names) for one file, read in CHUNK pieces that overlap a little."""
    overlap = 256 + max((len(n) for _, ns, _ in needles for n in ns), default=0)
    found, users = [], set()
    prev, block = b"", f.read(CHUNK)
    while block:
        nxt = f.read(CHUNK)
        buf = prev + block
        low = buf.lower()  # one fast pass instead of a case-insensitive regex per term
        for t, ns, word in needles:
            if t not in found and any(n in low for n in ns) and (word is None or word.search(buf)):
                found.append(t)
        if b"users" in low or b"/home/" in low:
            for m in USER_PATH.finditer(buf):
                # a name cut off at the end of this piece is read again, whole, with the next one
                if (nxt and m.end() == len(buf)) or URL_BEFORE.search(buf, max(0, m.start() - 100), m.start()):
                    continue
                users.add(m.group(1).lower())
        prev, block = buf[-overlap:], nxt
    return found, users - SHARED_USER_DIRS


def image_metadata(name: str, data: bytes) -> list[Finding]:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            keys = [k for k in im.info if k not in ("dpi", "jfif", "jfif_version", "jfif_unit", "jfif_density",
                                                    "gamma", "transparency", "duration", "loop", "background")]
            if im.getexif() or keys:
                return [Finding(name, "image metadata (EXIF or text chunks): run `modrelease clean` on it")]
    except OSError:
        pass
    return []


def check_file(name: str, f: BinaryIO, needles: list[tuple[str, list[bytes], re.Pattern | None]]) -> tuple[list[Finding], list[Finding]]:
    """-> (problems that stop the release, notes about other people's paths and what Nexus will do with the file)."""
    ext = Path(name).suffix.lower()
    problems = []
    if ext in IMAGES:  # images are small; read once for both checks
        data = f.read()
        problems += image_metadata(name, data)
        f = io.BytesIO(data)
    found, users = scan_stream(f, needles)
    for t, _, _ in needles:  # your own login as a user folder (C:\Users\max at the end of a line) is a problem
        if t not in found and t.lower().encode("utf-8") in users:
            found.append(t)
    users -= {t.lower().encode("utf-8") for t, _, _ in needles}
    problems = [Finding(name, f"contains {t!r}") for t in found] + problems
    notes = []
    if users:
        shown = ", ".join(u.decode("utf-8", "replace") for u in sorted(users))
        notes.append(Finding(name, f"user folder path ({shown}): fine if it is a third-party build path, check it isn't yours"))
    if ext in ARCHIVES:
        notes.append(Finding(name, "archive inside the archive: Nexus quarantines nested archives"))
    if ext in EXECUTABLES:
        notes.append(Finding(name, "executable: Nexus quarantines these for a manual review (`modrelease appeal`)"))
    return problems, notes


def collect(sources: list[Path], skip: list[Path] = ()) -> list[tuple[Path, str]]:
    """(file, path inside the archive) for every file, sorted, junk and `skip` (the zip being written) left out.
    A folder's contents go to the archive root."""
    skipped = {p.resolve() for p in skip}
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
    items = [(p, a) for p, a in items if p.resolve() not in skipped]
    items.sort(key=lambda x: x[1])
    dupes = [a for a, n in Counter(a for _, a in items).items() if n > 1]
    if dupes:
        raise ValueError(f"two files would land on the same path: {sorted(dupes)}")
    return items


def check(items: list[tuple[Path, str]], terms: list[str]) -> tuple[list[Finding], list[Finding]]:
    needles = term_needles(terms)
    problems, notes = [], []
    for path, arc in items:
        with open(path, "rb") as f:
            pr, no = check_file(arc, f, needles)
        problems += pr
        notes += no
    return problems, notes


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_zip(items: list[tuple[Path, str]], out: Path, prefix: str = "") -> str:
    """Same files in, same bytes out: fixed dates and permissions, sorted entries, forward slashes. -> sha256.
    The zip is written next to `out` first, so a failure halfway leaves the previous zip as it was."""
    prefix = prefix.strip("/\\").replace("\\", "/")
    date = fixed_date()
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    try:
        with zipfile.ZipFile(part, "w") as z:
            for path, arc in items:
                zi = zipfile.ZipInfo(f"{prefix}/{arc}" if prefix else arc, date)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = 0o644 << 16
                zi.create_system = 0  # same header on every OS
                zi.file_size = path.stat().st_size  # lets zipfile switch to zip64 for files over 2 GB
                with open(path, "rb") as src, z.open(zi, "w") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
    digest = sha256(out)
    out.with_name(out.name + ".sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    return digest
