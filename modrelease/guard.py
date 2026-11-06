"""Git identity guard for a public alias: per-folder personas, a deny list, hooks and a history audit."""
from __future__ import annotations

import getpass
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
    """The deny list plus this PC's login and home folder; never a part of the persona's own name or email."""
    home = Path.home()
    auto = [] if os.environ.get("CI") else [getpass.getuser(), home.name, str(home), home.as_posix()]
    own = f"{persona['name']} {persona['email']}".lower() if persona else ""
    seen, out = set(), []
    for t in [*cfg["deny"], *auto]:
        if len(t) >= 3 and t.lower() not in seen and t.lower() not in own:
            seen.add(t.lower())
            out.append(t)
    return out


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
        r = subprocess.run(["git", "var", var], capture_output=True, text=True)
        m = IDENT.match(r.stdout.strip())
        if not m:
            out.append(f"no {who} identity set: git config user.name / user.email")
            continue
        out += _identity_problems(who, *m.groups(), persona)
    return out


def staged_added() -> bytes:
    # shortcut: the whole staged diff is held in memory, fine for source repos, not for multi-GB binaries
    diff = git("diff", "--cached", "--no-color", "--no-ext-diff", "--text", "-U0")
    return b"\n".join(line for line in diff.splitlines() if line.startswith(b"+"))


def audit(revs: list[str], persona: dict | None, needles, cwd=None) -> tuple[list[str], set[str], set[str]]:
    """-> (problems, identities, offsets) for the commits `revs` select: identity and dates against the persona,
    deny-list strings in identities, messages and added lines."""
    fmt = COMMIT + "%h%x00%an%x00%ae%x00%ai%x00%cn%x00%ce%x00%ci%x00%B%x00"
    out = git("log", "--no-color", "--no-ext-diff", "-p", "--text", "-U0", f"--format={fmt}", *revs, cwd=cwd)
    problems, idents, offsets = [], set(), set()
    for chunk in out.split(COMMIT.encode())[1:]:
        *head, diff = chunk.split(b"\x00", 8)
        h, an, ae, ad, cn, ce, cd, msg = (f.decode("utf-8", "replace") for f in head)
        idents |= {f"{an} <{ae}>", f"{cn} <{ce}>"}
        offsets |= {ad[-5:], cd[-5:]}
        if persona:
            problems += [f"{h}: {p}" for p in _identity_problems("author", an, ae, ad[-5:], persona)
                         + _identity_problems("committer", cn, ce, cd[-5:], persona)]
        added = b"\n".join(line for line in diff.splitlines() if line.startswith(b"+"))
        for t in found(f"{an} {ae} {cn} {ce}\n{msg}".encode(), needles):
            problems.append(f"{h}: {t!r} in the author, committer or message")
        for t in found(added, needles):
            problems.append(f"{h}: adds {t!r}")
    return problems, idents, offsets


def run_hook(name: str, args: list[str]) -> int:
    sys.stderr.reconfigure(errors="replace")  # a name the console code page lacks must not crash the hook
    cfg = load()
    persona = persona_for(cfg, os.getcwd())
    if persona is None or name not in GUARDED:
        return 0
    needles = pack.term_needles(deny_terms(cfg, persona))
    problems, hint = [], ""
    if name == "pre-commit":
        problems = pending_identity(persona) + [f"the staged changes add {t!r}" for t in found(staged_added(), needles)]
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
    persona = persona_for(cfg, path)
    problems, idents, offsets = audit(["--all"], persona, pack.term_needles(deny_terms(cfg, persona)), cwd=path)
    for p in problems:
        print(f"  ! {p}")
    print("identities: " + ", ".join(sorted(idents)))
    print("time zones: " + ", ".join(sorted(offsets)))
    if persona is None:
        print("(no persona for this folder: identities and time zones are listed, not judged)")
    print(f"{len(problems)} problem(s)" if problems else "clean")
    return 1 if problems else 0
