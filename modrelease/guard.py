"""Git identity guard for a public alias: per-folder personas, a deny list, hooks and a history audit."""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from . import pack

PREFIX = "[modrelease guard]"
MARKER = "# written by modrelease guard install"
GUARDED = ("pre-commit", "commit-msg", "pre-push")
# hooks a global core.hooksPath would hide from .git/hooks (git-lfs uses several): these only pass through
PASSTHROUGH = ("prepare-commit-msg", "post-commit", "post-checkout", "post-merge", "post-rewrite",
               "pre-rebase", "pre-merge-commit")
STDIN_HOOKS = {"pre-push", "post-rewrite", "reference-transaction"}
TOP_KEYS = {"deny", "persona"}
PERSONA_KEYS = {"path", "name", "email", "utc", "refuse_trailers"}
ZERO = "0" * 40
# Windows default and shared accounts: as automatic terms they would block code like `def get(user):`
GENERIC = {d.decode() for d in pack.SHARED_USER_DIRS} | {"user", "admin", "owner", "administrator"}
IDENT = re.compile(r"^(.*) <(.*)> \d+ ([+-]\d{4})$")
TRAILER = re.compile(r"(?im)^(?:co-authored-by|generated-by|assisted-by|ai-assisted|made-with)\s*:"
                     r"|generated (?:with|by) \S*\s*(?:claude|chatgpt|copilot|cursor|codex|gemini|aider)"
                     r"|noreply@anthropic\.com")
COMMIT = "\x1emodrelease-commit\x1f"  # splits `git log -p` output; a blob would need these exact bytes to confuse it


def config_dir() -> Path:
    return Path(os.environ.get("MODRELEASE_CONFIG_DIR") or Path.home() / ".config" / "modrelease")


def load(path: Path | None = None) -> dict:
    p = path or config_dir() / "guard.toml"
    if not p.is_file():
        return {"deny": [], "persona": []}
    with open(p, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("deny", [])
    cfg.setdefault("persona", [])
    unknown = (set(cfg) - TOP_KEYS) | {f"persona.{k}" for per in cfg["persona"] for k in set(per) - PERSONA_KEYS}
    if unknown:
        raise ValueError(f"{p}: unknown setting(s) {', '.join(sorted(unknown))}")
    for i, per in enumerate(cfg["persona"]):
        missing = {"path", "name", "email"} - set(per)
        if missing:
            raise ValueError(f"{p}: persona {i + 1} needs {', '.join(sorted(missing))}")
    return cfg


def deny_terms(cfg: dict, persona: dict | None = None) -> list[str]:
    """The deny list plus what pack checks for (login, PC name, global git identity) and the home path. A found term
    that is a generic login ("User") or the persona's own name or email is dropped; listed terms always stay."""
    home = Path.home()
    paths = [] if os.environ.get("CI") else [str(home), home.as_posix()]
    listed = {t.lower() for t in cfg["deny"]}
    skip = GENERIC | (_own(persona) if persona else set())
    return [t for t in pack.personal_terms([*cfg["deny"], *paths]) if t.lower() in listed or t.lower() not in skip]


def _own(persona: dict) -> set[str]:
    return {persona["name"].lower(), persona["email"].lower()}


def identity_text(parts: list[str], persona: dict | None, msg: str = "") -> bytes:
    """Names and emails to search for deny-list terms, minus the persona's own (its name may contain a listed word)."""
    own = _own(persona) if persona else set()
    return (" ".join(p for p in parts if p.lower() not in own) + "\n" + msg).encode()


def _norm(p) -> str:
    return os.path.normcase(str(Path(p).expanduser().resolve()))


def persona_for(cfg: dict, where) -> dict | None:
    """The persona whose folder holds `where`; the deepest folder wins."""
    here, best, best_len = _norm(where), None, -1
    for per in cfg["persona"]:
        root = _norm(per["path"]).rstrip(os.sep)
        if (here == root or here.startswith(root + os.sep)) and len(root) > best_len:
            best, best_len = per, len(root)
    return best


def persona_here(cfg: dict, where) -> dict | None:
    """Like persona_for, but a linked worktree outside the alias folder counts as part of the repo it came from."""
    per = persona_for(cfg, where)
    if per or not cfg["persona"]:
        return per
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=where, check=False).decode("utf-8", "replace").strip()
    return persona_for(cfg, Path(common).parent) if common else None


def git(*args: str, cwd=None, check: bool = True) -> bytes:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def found(data: bytes, needles) -> list[str]:
    return pack.scan_stream(io.BytesIO(data), needles)[0] if data else []


def _identity_problems(who: str, name: str, email: str, offset: str, persona: dict) -> list[str]:
    out = []
    if name != persona["name"] or email.lower() != persona["email"].lower():
        out.append(f"{who} is {name} <{email}>, this folder belongs to {persona['name']} <{persona['email']}>")
    if persona.get("utc", True) and offset != "+0000":
        out.append(f"{who} date has the offset {offset}: commit with TZ=UTC so the time zone shows no region")
    return out


def pending_identity(persona: dict) -> list[str]:
    """What the next commit would record: `git var` sees -c user.email, --author, --date, GIT_* variables and TZ."""
    out = []
    for who, var in (("author", "GIT_AUTHOR_IDENT"), ("committer", "GIT_COMMITTER_IDENT")):
        m = IDENT.match(git("var", var, check=False).decode("utf-8", "replace").strip())
        if not m:
            out.append(f"no {who} identity set: git config user.name / user.email")
            continue
        out += _identity_problems(who, *m.groups(), persona)
    return out


def git_lines(args: list[str], cwd=None):
    """git's output line by line, a long line in CHUNK pieces, so a history full of built paks never sits in memory."""
    with subprocess.Popen(["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as p:
        while piece := p.stdout.readline(pack.CHUNK):
            yield piece
        err = p.stderr.read()
    if p.returncode:
        raise RuntimeError(f"git {args[0]}: {err.decode('utf-8', 'replace').strip()}")


class Added:
    """Deny-list terms in the added lines of a diff fed piece by piece; holds at most about CHUNK bytes."""
    def __init__(self, needles):
        self.needles, self.buf, self.terms, self.plus, self.line_start = needles, bytearray(), [], False, True

    def feed(self, piece: bytes) -> None:
        if self.line_start:
            self.plus = piece.startswith(b"+")  # "+++ b/name" counts too: a new file name can leak
        self.line_start = piece.endswith(b"\n")
        if self.plus:
            self.buf += piece
            if len(self.buf) >= pack.CHUNK:
                self._scan()
                del self.buf[:-4096]  # the tail is scanned again, so a term cut in two is still found

    def done(self) -> list[str]:
        self._scan()
        return self.terms

    def _scan(self) -> None:
        self.terms += [t for t in found(bytes(self.buf), self.needles) if t not in self.terms]


DIFF = ["--no-color", "--no-ext-diff", "--no-renames", "--text", "-U0"]  # a rename shows its new name as added


def staged_added(needles) -> list[str]:
    added = Added(needles)
    for piece in git_lines(["diff", "--cached", *DIFF]):
        added.feed(piece)
    return added.done()


def commits(revs: list[str], needles, cwd=None):
    """-> (hash, author, email, date, committer, email, date, message, terms in added lines) per commit."""
    fmt = COMMIT + "%h%x00%an%x00%ae%x00%ai%x00%cn%x00%ce%x00%ci%x00%B%x00"
    head, fields, added = None, None, None
    for piece in git_lines(["log", "-p", *DIFF, f"--format={fmt}", *revs], cwd):
        if piece.startswith(COMMIT.encode()):
            if added:
                yield *fields, added.done()
            head, added = piece[len(COMMIT):], None
        elif added:
            added.feed(piece)
        elif head is not None:
            head += piece  # a message over several lines
        if not added and head is not None and head.count(b"\x00") >= 8:
            fields = [f.decode("utf-8", "replace") for f in head.split(b"\x00", 8)[:8]]
            head, added = None, Added(needles)
    if added:
        yield *fields, added.done()


def audit(revs: list[str], persona: dict | None, needles, cwd=None) -> tuple[list[str], set[str], set[str]]:
    """-> (problems, identities, offsets) for the commits `revs` select: identity and dates against the persona,
    deny-list strings in identities, messages and added lines."""
    problems, idents, offsets = [], set(), set()
    for h, an, ae, ad, cn, ce, cd, msg, terms in commits(revs, needles, cwd):
        idents |= {f"{an} <{ae}>", f"{cn} <{ce}>"}
        offsets |= {ad[-5:], cd[-5:]}
        if persona:
            problems += [f"{h}: {p}" for p in _identity_problems("author", an, ae, ad[-5:], persona)
                         + _identity_problems("committer", cn, ce, cd[-5:], persona)]
        for t in found(identity_text([an, ae, cn, ce], persona, msg), needles):
            problems.append(f"{h}: {t!r} in the author, committer or message")
        for t in terms:
            problems.append(f"{h}: adds {t!r}")
    return problems, idents, offsets


def tag_audit(sha: str, persona: dict | None, needles, cwd=None) -> tuple[list[str], set[str], set[str]]:
    """An annotated tag keeps a tagger name, email and date of its own, apart from the commit's."""
    head, _, msg = git("cat-file", "tag", sha, cwd=cwd).decode("utf-8", "replace").partition("\n\n")
    fields = dict(line.split(" ", 1) for line in head.splitlines() if " " in line)
    label = f"tag {fields.get('tag', sha[:7])}"
    m = IDENT.match(fields.get("tagger", ""))  # very old tags have no tagger line
    name, email, offset = m.groups() if m else ("", "", "")
    problems = [f"{label}: {p}" for p in _identity_problems("tagger", name, email, offset, persona)] if m and persona else []
    problems += [f"{label}: {t!r} in the tagger or message" for t in found(identity_text([name, email], persona, msg), needles)]
    return problems, ({f"{name} <{email}>"} if m else set()), ({offset} if m else set())


def all_tags(persona: dict | None, needles, cwd=None) -> tuple[list[str], set[str], set[str]]:
    problems, idents, offsets = [], set(), set()
    for line in git("for-each-ref", "--format=%(objectname) %(objecttype)", "refs/tags", cwd=cwd).decode().split("\n"):
        sha, _, kind = line.partition(" ")
        if kind == "tag":
            p, i, o = tag_audit(sha, persona, needles, cwd)
            problems, idents, offsets = problems + p, idents | i, offsets | o
    return problems, idents, offsets


def run_hook(name: str, args: list[str]) -> int:
    sys.stderr.reconfigure(errors="replace")  # a name the console code page lacks must not crash the hook
    cfg = load()
    persona = persona_here(cfg, os.getcwd())
    if persona is None or name not in GUARDED:
        return 0
    needles = pack.term_needles(deny_terms(cfg, persona))
    problems, hint = [], ""
    if name == "pre-commit":
        problems = pending_identity(persona) + [f"the staged changes add {t!r}" for t in staged_added(needles)]
    elif name == "commit-msg":
        msg = Path(args[0]).read_text("utf-8", "replace")
        msg = "\n".join(line for line in msg.splitlines() if not line.startswith("#"))
        if persona.get("refuse_trailers", True) and TRAILER.search(msg):
            problems.append("the message has a Co-Authored-By or AI attribution line")
        problems += [f"the message contains {t!r}" for t in found(msg.encode(), needles)]
    else:
        remote = args[0] if args else "origin"
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[1] != ZERO:
                problems += audit([parts[1], "--not", f"--remotes={remote}"], persona, needles)[0]
                if parts[0].startswith("refs/tags/") and git("cat-file", "-t", parts[1]).strip() == b"tag":
                    problems += tag_audit(parts[1], persona, needles)[0]
        hint = (f"\nOnly commits that are not on {remote} yet are checked. If these are other people's commits"
                f" (a fresh fork), run `git fetch {remote}` and push again.")
    if problems:
        print(f"{PREFIX} {name} refused in {persona['name']}'s folder:", file=sys.stderr)
        for p in dict.fromkeys(problems):
            print(f"  ! {p}", file=sys.stderr)
        if hint:
            print(hint.strip("\n"), file=sys.stderr)
        return 1
    return 0


def hook_script(name: str, prior: str | None) -> str:
    """A small sh script for Git Bash, Linux and macOS: the check runs in Python, then the hook this one replaced."""
    py = Path(sys.executable).as_posix()
    inp = ' < "$tmp"' if name in STDIN_HOOKS else ""
    lines = ["#!/bin/sh", MARKER]
    if inp:
        lines += ['tmp=$(mktemp) || exit 1', "trap 'rm -f \"$tmp\"' EXIT", 'cat > "$tmp"']
    if name in GUARDED:
        lines.append(f'"{py}" -m modrelease guard hook {name} "$@"{inp} || exit 1')
    # without a previous hooksPath the repo's own .git/hooks run (--git-path hooks would point back here)
    lines += [f"d={shlex.quote(prior or '')}",
              '[ -n "$d" ] || d="$(git rev-parse --git-common-dir)/hooks"',
              f'[ -x "$d/{name}" ] || exit 0',
              f'"$d/{name}" "$@"{inp}']
    return "\n".join(lines) + "\n"


def _global_hooks_path() -> str | None:
    out = git("config", "--global", "--type=path", "core.hooksPath", check=False).decode().strip()
    return out or None


def _same(a: str | None, b) -> bool:
    return bool(a) and _norm(a) == _norm(b)


def install(apply: bool) -> int:
    d = config_dir() / "hooks"
    state_file = config_dir() / "guard-install.json"
    state = json.loads(state_file.read_text("utf-8")) if state_file.is_file() else {}
    current = _global_hooks_path()
    prior = state.get("prior") if _same(current, d) else current
    names = set(GUARDED) | set(PASSTHROUGH)
    if prior and Path(prior).is_dir():
        names |= {f.name for f in Path(prior).iterdir() if f.is_file() and "." not in f.name}
    d.mkdir(parents=True, exist_ok=True)
    for n in sorted(names):
        f = d / n
        if f.exists() and MARKER not in f.read_text("utf-8", "replace"):
            raise ValueError(f"{f} was not written by modrelease: move it away first")
    for n in sorted(names):
        (d / n).write_text(hook_script(n, prior), "utf-8", newline="\n")
        (d / n).chmod(0o755)
    state = {"hooks": str(d), "prior": prior, "applied": state.get("applied", False)}
    cmd = f'git config --global core.hooksPath "{d.as_posix()}"'
    if apply and not _same(current, d):
        git("config", "--global", "core.hooksPath", d.as_posix())
        state["applied"] = True
    state_file.write_text(json.dumps(state, indent=2), "utf-8")
    print(f"Hooks written to {d}")
    if prior:
        print(f"They run your previous hooks from {prior} after the guard.")
    if _same(_global_hooks_path(), d):
        print("git uses them now (core.hooksPath).")
    else:
        print(f"Your git config is unchanged. To turn the guard on:\n  {cmd}\nor run `modrelease guard install --apply`.")
    if not load()["persona"]:
        print(f"No [[persona]] in {config_dir() / 'guard.toml'} yet, so the hooks guard nothing.")
    return 0


def uninstall() -> int:
    state_file = config_dir() / "guard-install.json"
    if not state_file.is_file():
        raise ValueError("the guard is not installed (no guard-install.json)")
    state = json.loads(state_file.read_text("utf-8"))
    d, prior = Path(state["hooks"]), state.get("prior")
    if _same(_global_hooks_path(), d):
        if state.get("applied"):
            git("config", "--global", *(["core.hooksPath", prior] if prior else ["--unset", "core.hooksPath"]))
            print(f"core.hooksPath set back to {prior}" if prior else "core.hooksPath removed")
        else:
            undo = f'git config --global core.hooksPath "{prior}"' if prior else "git config --global --unset core.hooksPath"
            print(f"You set core.hooksPath yourself, so it stays. To undo it:\n  {undo}")
    for f in d.iterdir() if d.is_dir() else []:
        if f.is_file() and MARKER in f.read_text("utf-8", "replace"):
            f.unlink()
    if d.is_dir() and not any(d.iterdir()):
        d.rmdir()
    state_file.unlink()
    print(f"Hooks removed from {d}")
    return 0


def check(path: str) -> int:
    cfg = load()
    persona = persona_here(cfg, path)
    needles = pack.term_needles(deny_terms(cfg, persona))
    problems, idents, offsets = audit(["--all"], persona, needles, cwd=path)
    p, i, o = all_tags(persona, needles, cwd=path)
    problems, idents, offsets = problems + p, idents | i, offsets | o
    for p in problems:
        print(f"  ! {p}")
    print("identities: " + ", ".join(sorted(idents)))
    print("time zones: " + ", ".join(sorted(offsets)))
    if persona is None:
        print("(no persona for this folder: identities and time zones are listed, not judged)")
    print(f"{len(problems)} problem(s)" if problems else "clean")
    return 1 if problems else 0
