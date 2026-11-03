"""Reproducible release archives and the checks that run before one is written."""
from __future__ import annotations

import fnmatch
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
# credentials that end up in mod configs and tools; the finding names the kind, never the value
# credentials that end up in mod configs and tools; the finding names the kind, never the value.
# Each regex runs only on pieces that contain one of its lowercase anchors, which keeps big files fast.
SECRETS = [(label, anchors, re.compile(p)) for label, anchors, p in (
    ("a Discord webhook URL", [b"/api/webhooks/"], rb"discord(?:app)?\.com/api/webhooks/\d{5,}/[\w-]{20,}"),
    ("a GitHub token", [b"ghp_", b"gho_", b"ghu_", b"ghs_", b"ghr_", b"github_pat_"],
     rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{60,})"),
    ("an AWS access key", [b"akia", b"asia"], rb"\b(?:AKIA|ASIA)(?!IOSFODNN7EXAMPLE)[0-9A-Z]{16}\b"),  # docs example is fine
    ("a Google API key", [b"aiza"], rb"\bAIza[\w-]{35}\b"),
    ("a Slack token", [b"xox"], rb"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("a private key", [b"private key-----"],
     rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\s*[A-Za-z0-9+/=]{40}"),
)]
DEBUG_EXT = {".pdb"}
SOURCE_EXT = {".psd", ".blend", ".blend1", ".max", ".ma", ".mb", ".spp", ".kra", ".xcf",
              ".uproject", ".sln", ".csproj", ".vcxproj"}
DEV_DIRS = {"saved", "intermediate", "deriveddatacache", ".vs", ".idea"}  # engine and IDE work folders
WIN_BAD = re.compile(r'[<>:"|?*\x00-\x1f]|[. ]$')
WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
LONG_PATH = 160  # plus a deep game folder like C:\Program Files (x86)\Steam\steamapps\common\... nears Windows' 260


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
    # on a CI runner the login ("runner") and host name are the build machine's, not yours: only --term/config count
    machine = [] if os.environ.get("CI") else [getpass.getuser(), socket.gethostname(), Path.home().name]
    terms = [*machine, *extra]
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


def scan_stream(f: BinaryIO, needles: list[tuple[str, list[bytes], re.Pattern | None]]) -> tuple[list[str], set[bytes], list[str]]:
    """-> (terms found, user folder names, kinds of secrets) for one file, read in CHUNK pieces that overlap a little."""
    overlap = 256 + max((len(n) for _, ns, _ in needles for n in ns), default=0)
    found, users, secrets = [], set(), []
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
        for label, anchors, p in SECRETS:
            if label not in secrets and _secret_near(buf, low, anchors, p):
                secrets.append(label)
        prev, block = buf[-overlap:], nxt
    return found, users - SHARED_USER_DIRS, secrets


def _secret_near(buf: bytes, low: bytes, anchors: list[bytes], p: re.Pattern) -> bool:
    """The regex only runs in a small window around each anchor: a full regex pass per piece is several times slower."""
    for a in anchors:
        i = low.find(a)
        while i != -1:
            if p.search(buf, max(0, i - 64), i + 512):
                return True
            i = low.find(a, i + 1)
    return False


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
    found, users, secrets = scan_stream(f, needles)
    for t, _, _ in needles:  # your own login as a user folder (C:\Users\max at the end of a line) is a problem
        if t not in found and t.lower().encode("utf-8") in users:
            found.append(t)
    users -= {t.lower().encode("utf-8") for t, _, _ in needles}
    problems = [Finding(name, f"contains {t!r}") for t in found] + problems
    problems += [Finding(name, f"contains {s}: remove it and revoke it, it is no longer secret") for s in secrets]
    notes = []
    if users:
        shown = ", ".join(u.decode("utf-8", "replace") for u in sorted(users))
        notes.append(Finding(name, f"user folder path ({shown}): fine if it is a third-party build path, check it isn't yours"))
    if ext in ARCHIVES:
        notes.append(Finding(name, "archive inside the archive: Nexus quarantines nested archives"))
    if ext in EXECUTABLES:
        notes.append(Finding(name, "executable: Nexus quarantines these for a manual review (`modrelease appeal`)"))
    if ext in DEBUG_EXT:
        notes.append(Finding(name, "debug symbols: players don't need them and they hold the build folder path"))
    if ext in SOURCE_EXT:
        notes.append(Finding(name, "a source/project file: players don't need it, exclude it unless you ship it on purpose"))
    return problems, notes


def check_names(names: list[str]) -> tuple[list[Finding], list[Finding]]:
    """Checks on the paths inside the archive, as opposed to the file contents: things that break on players' PCs."""
    problems, notes = [], []
    by_case = {}
    for n in names:
        by_case.setdefault(n.lower(), []).append(n)
    for same in by_case.values():
        if len(same) > 1:
            problems.append(Finding(same[0], f"differs only in case from {', '.join(same[1:])}: Windows keeps one of them"))
    for n in names:
        parts = n.split("/")
        if any(WIN_BAD.search(p) or p.split(".")[0].lower() in WIN_RESERVED for p in parts):
            problems.append(Finding(n, "a name Windows can't create (reserved word, <>:\"|?*, or a trailing dot or space)"))
        if len(n) > LONG_PATH:
            notes.append(Finding(n, f"{len(n)} characters: inside a deep game folder it can pass Windows' 260 limit"))
        if not n.isascii():
            notes.append(Finding(n, "non-Latin characters in the name: some mod managers and old unzip tools mangle them"))
    dev = sorted({p for n in names for p in n.split("/")[:-1] if p.lower() in DEV_DIRS})
    for d in dev:
        notes.append(Finding(d + "/", "an engine or IDE work folder: players don't need it"))
    return problems, notes


def excluded(arc: str, patterns: list[str]) -> bool:
    """`*.psd` matches a file name anywhere, `Saved/` a folder anywhere, `docs/*.md` a path from the archive root."""
    arc = arc.lower()
    parts = arc.split("/")
    for pat in (p.lower().replace("\\", "/") for p in patterns):
        if pat.endswith("/"):
            if any(fnmatch.fnmatchcase(d, pat.rstrip("/")) for d in parts[:-1]):
                return True
        elif fnmatch.fnmatchcase(arc, pat) or ("/" not in pat and fnmatch.fnmatchcase(parts[-1], pat)):
            return True
    return False


def collect(sources: list[Path], skip: list[Path] = (), exclude: list[str] = ()) -> list[tuple[Path, str]]:
    """(file, path inside the archive) for every file, sorted; junk, `exclude` patterns and `skip` (the zip
    being written) left out. A folder's contents go to the archive root."""
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
    items = [(p, a) for p, a in items if p.resolve() not in skipped and not excluded(a, exclude)]
    items.sort(key=lambda x: x[1])
    dupes = [a for a, n in Counter(a for _, a in items).items() if n > 1]
    if dupes:
        raise ValueError(f"two files would land on the same path: {sorted(dupes)}")
    return items


def check(items: list[tuple[Path, str]], terms: list[str], prefix: str = "") -> tuple[list[Finding], list[Finding]]:
    needles = term_needles(terms)
    problems, notes = [], []
    for path, arc in items:
        with open(path, "rb") as f:
            pr, no = check_file(arc, f, needles)
        problems += pr
        notes += no
    prefix = prefix.strip("/\\").replace("\\", "/")
    pr, no = check_names([f"{prefix}/{arc}" if prefix else arc for _, arc in items])
    return problems + pr, notes + no


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


def diff_zips(old: Path, new: Path) -> tuple[list[str], list[str], list[tuple[str, int, int]], int]:
    """-> (added, removed, changed as (name, old size, new size), unchanged count), from the zip headers alone."""
    def entries(p: Path) -> dict[str, tuple[int, int]]:
        with zipfile.ZipFile(p) as z:
            return {i.filename: (i.CRC, i.file_size) for i in z.infolist() if not i.is_dir()}
    a, b = entries(old), entries(new)
    added = sorted(b.keys() - a.keys())
    removed = sorted(a.keys() - b.keys())
    changed = sorted((n, a[n][1], b[n][1]) for n in a.keys() & b.keys() if a[n] != b[n])
    return added, removed, changed, len(a.keys() & b.keys()) - len(changed)
