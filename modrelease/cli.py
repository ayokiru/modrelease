"""modrelease: get a mod ready for Nexus Mods, GitHub or anywhere else.

  modrelease check  FILES...        look for your login, PC name, email and user paths, image metadata
  modrelease pack   SRC -o X.zip    the same check, then a reproducible zip and its .sha256
  modrelease diff   OLD.zip NEW.zip   files added, removed and changed between two releases
  modrelease clean  MEDIA... -o DIR screenshots and videos without EXIF, GPS, tags or encoder strings
  modrelease vt     X.zip           VirusTotal result for the file (VT_API_KEY)
  modrelease nexus  X.zip ...       upload it as a new version of a file on Nexus Mods (NEXUS_API_KEY)
  modrelease appeal X.zip ...       the email that gets a quarantined Nexus file reviewed
"""
from __future__ import annotations

import argparse
import os
import sys
import tomllib
import zipfile
from pathlib import Path

from . import __version__, appeal, clean, nexus, pack, virustotal


CONFIG = "modrelease.toml"
CONFIG_KEYS = {"source", "output", "version", "prefix", "exclude", "terms", "nexus"}
NEXUS_KEYS = {"file_id", "mod_id", "category", "name"}


def load_config(path: str | None) -> dict:
    """modrelease.toml from the current folder (or --config); paths in it are relative to the file."""
    p = Path(path or CONFIG)
    if not p.is_file():
        if path:
            raise FileNotFoundError(f"config not found: {p}")
        return {}
    with open(p, "rb") as f:
        cfg = tomllib.load(f)
    unknown = (set(cfg) - CONFIG_KEYS) | {f"nexus.{k}" for k in set(cfg.get("nexus", {})) - NEXUS_KEYS}
    if unknown:
        raise ValueError(f"{p}: unknown setting(s) {', '.join(sorted(unknown))}")
    for key in ("source", "output"):
        if key in cfg:
            cfg[key] = str(p.parent / cfg[key])
    return cfg


def _output(given: str | None, cfg: dict, version: str | None) -> str | None:
    """The zip name from -o or the config, with {version} filled in."""
    output = given or cfg.get("output")
    if output and "{version}" in output:
        if not version:
            raise ValueError("the output name has {version}: pass --mod-version or set `version`")
        output = output.replace("{version}", str(version))
    return output


def _print_findings(problems, notes) -> None:
    for f in problems:
        print(f"  ! {f.file}: {f.what}")
    for f in notes:
        print(f"  - {f.file}: {f.what}")


def cmd_check(a) -> int:
    cfg = a.cfg
    paths = a.paths or ([cfg["source"]] if "source" in cfg else [])
    if not paths:
        raise ValueError(f"nothing to check: pass files or a folder, or set `source` in {CONFIG}")
    terms = pack.personal_terms(a.term + cfg.get("terms", []))
    needles = pack.term_needles(terms)
    problems, notes = [], []
    for s in paths:
        p = Path(s)
        if p.suffix.lower() == ".zip" and p.is_file():
            with zipfile.ZipFile(p) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    with z.open(info) as f:
                        pr, no = pack.check_file(info.filename, f, needles)
                    problems += pr
                    notes += no
                pr, no = pack.check_names([i.filename for i in z.infolist() if not i.is_dir()])
                problems += pr
                notes += no
                dates = {i.date_time for i in z.infolist()}
                if len(dates) > 1:  # one fixed date for every file is what reproducible packers write
                    notes.append(pack.Finding(p.name, f"real file dates in the zip (newest {max(dates)[:3]}): repack with `modrelease pack`"))
        else:
            pr, no = pack.check(pack.collect([p], exclude=cfg.get("exclude", [])), terms)
            problems += pr
            notes += no
    _print_findings(problems, notes)
    print(f"{len(problems)} problem(s)" if problems else "clean")
    return 1 if problems else 0


def cmd_pack(a) -> int:
    cfg = a.cfg
    sources = a.sources or ([cfg["source"]] if "source" in cfg else [])
    output = _output(a.output, cfg, a.mod_version or cfg.get("version"))
    if not sources or not output:
        raise ValueError(f"pack needs a source and -o (or `source` and `output` in {CONFIG})")
    prefix = a.prefix if a.prefix is not None else cfg.get("prefix", "")
    out = Path(output)
    # a zip written into the mod folder must not end up inside the next one
    items = pack.collect([Path(s) for s in sources], skip=[out, out.with_name(out.name + ".sha256")],
                         exclude=a.exclude + cfg.get("exclude", []))
    if not items:
        raise ValueError("nothing to pack: no files in " + ", ".join(map(str, sources)))
    problems, notes = pack.check(items, pack.personal_terms(a.term + cfg.get("terms", [])), prefix)
    _print_findings(problems, notes)
    if problems and not a.force:
        print(f"{len(problems)} problem(s), nothing written. Fix them or pass --force.")
        return 1
    digest = pack.write_zip(items, out, prefix)
    print(f"{out} ({len(items)} files, {out.stat().st_size // 1024} KB)\nsha256 {digest}")
    return 0


def _kb(n: int) -> str:
    return f"{n / 1024:,.0f} KB" if n >= 1024 else f"{n} B"


def cmd_diff(a) -> int:
    added, removed, changed, same = pack.diff_zips(Path(a.old), Path(a.new))
    for n in added:
        print(f"  + {n}")
    for n in removed:
        print(f"  - {n}")
    for n, old_size, new_size in changed:
        print(f"  ~ {n} ({_kb(old_size)} -> {_kb(new_size)})")
    print(f"{len(added)} added, {len(removed)} removed, {len(changed)} changed, {same} unchanged")
    return 0


def cmd_clean(a) -> int:
    done = clean.clean([Path(p) for p in a.paths], Path(a.output), keep_audio=not a.no_audio)
    for src, dst in done:
        print(f"  {src} -> {dst}")
    print(f"{len(done)} file(s) cleaned" if done else "no images or videos found")
    return 0


def cmd_vt(a) -> int:
    key = a.key or os.environ.get("VT_API_KEY")
    path = Path(a.file)
    if not key:
        print(f"No API key (set VT_API_KEY or pass --key). Look the file up by hand:\n"
              f"{virustotal.GUI.format(pack.sha256(path))}")
        return 2
    r = virustotal.report(path, key, upload=a.upload)
    if r["pending"]:
        print(f"Uploaded, VirusTotal is still scanning it. Run `modrelease vt` again (without --upload) in a few minutes.\n{r['link']}")
        return 2
    if not r["known"]:
        print(f"VirusTotal has not seen this file. Run again with --upload.\n{r['link']}")
        return 2
    s = r["stats"]
    total = sum(s.values())
    print(f"{s.get('malicious', 0)} malicious, {s.get('suspicious', 0)} suspicious out of {total} engines\n{r['link']}")
    for engine, verdict in r["flagged"]:
        print(f"  {engine}: {verdict}")
    return 1 if r["flagged"] else 0


def cmd_nexus(a) -> int:
    cfg, nx = a.cfg, a.cfg.get("nexus", {})
    version = a.mod_version or cfg.get("version")
    file = a.file or _output(None, cfg, version)
    file_id = a.file_id or nx.get("file_id")
    key = a.key or os.environ.get("NEXUS_API_KEY")
    missing = [what for what, v in (("the zip", file), ("--file-id", file_id), ("--mod-version", version),
                                    ("an API key (NEXUS_API_KEY or --key)", key)) if not v]
    if missing:
        raise ValueError("nexus needs " + ", ".join(missing))
    mod_id = a.mod_id or nx.get("mod_id")
    path = Path(file)
    print(f"Uploading {path.name} ({path.stat().st_size // 1024} KB) as version {version} of file {file_id}")
    vid = nexus.upload(path, key, str(file_id), str(version), name=a.name or nx.get("name"), description=a.description,
                       category=a.category or nx.get("category", "main"), archive_old=a.archive_old,
                       update_mod_version=a.update_mod_version, mod_id=str(mod_id) if mod_id else None,
                       changelog=a.changelog)
    print(f"Done: new version {vid} on Nexus")
    return 0


def cmd_appeal(a) -> int:
    path = Path(a.file)
    summary = ""
    key = a.key or os.environ.get("VT_API_KEY")
    link = virustotal.GUI.format(pack.sha256(path))
    if key:
        r = virustotal.report(path, key)
        if r["known"]:
            n, total = len(r["flagged"]), sum(r["stats"].values())
            names = ", ".join(f"{e} ({v})" for e, v in r["flagged"])
            summary = f"{n} of {total} engines flag it: {names}, all others are clean" if n else f"none of {total} engines flag it"
    print(appeal.email(mod_url=a.mod, file_name=a.name or path.stem, username=a.user, source=a.source,
                       vt_link=link, vt_summary=summary, contents=appeal.executables(path), build=a.build))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="modrelease", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"modrelease {__version__}")
    p.add_argument("--config", help=f"settings file (default: ./{CONFIG} if it exists)")
    sub = p.add_subparsers(dest="cmd", required=True)
    term = dict(action="append", default=[], metavar="TEXT", help="another string that must not ship (repeatable)")

    s = sub.add_parser("check", help="look for personal data in files, folders or a zip")
    s.add_argument("paths", nargs="*", help="default: `source` from the config")
    s.add_argument("--term", **term)
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("pack", help="check, then write a reproducible zip and its .sha256")
    s.add_argument("sources", nargs="*", help="a folder (its contents go to the zip root) or files")
    s.add_argument("-o", "--output", help="the zip to write; {version} is replaced by the mod version")
    s.add_argument("--mod-version", help="fills {version} in the output name")
    s.add_argument("--prefix", help="folder inside the zip, e.g. ProjectPBZ/Content/Paks/~mods")
    s.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                   help="leave files out: *.psd, Saved/, docs/*.md (repeatable)")
    s.add_argument("--force", action="store_true", help="write the zip even if the check finds problems")
    s.add_argument("--term", **term)
    s.set_defaults(fn=cmd_pack)

    s = sub.add_parser("diff", help="what changed between two release zips")
    s.add_argument("old")
    s.add_argument("new")
    s.set_defaults(fn=cmd_diff)

    s = sub.add_parser("clean", help="strip metadata from screenshots and videos")
    s.add_argument("paths", nargs="+")
    s.add_argument("-o", "--output", default="clean", help="folder for the cleaned copies (default: ./clean)")
    s.add_argument("--no-audio", action="store_true", help="drop the audio track of videos")
    s.set_defaults(fn=cmd_clean)

    s = sub.add_parser("vt", help="VirusTotal result for a file")
    s.add_argument("file")
    s.add_argument("--upload", action="store_true", help="upload the file if VirusTotal has not seen it")
    s.add_argument("--key", help="VirusTotal API key (default: VT_API_KEY)")
    s.set_defaults(fn=cmd_vt)

    s = sub.add_parser("nexus", help="upload a new version of a file to Nexus Mods (NEXUS_API_KEY)")
    s.add_argument("file", nargs="?", help="the zip (default: `output` from the config)")
    s.add_argument("--file-id", help="the file on the mod page that gets the new version (nexus.file_id)")
    s.add_argument("--mod-version", help="version of the upload (default: `version` from the config)")
    s.add_argument("--name", help="display name (default: the zip name)")
    s.add_argument("--description", help="file description")
    s.add_argument("--category", choices=nexus.CATEGORIES, help="default: main")
    s.add_argument("--archive-old", action="store_true", help="move the previous version to Old files")
    s.add_argument("--update-mod-version", action="store_true", help="set the mod's version to this one")
    s.add_argument("--mod-id", help="needed for --changelog (nexus.mod_id)")
    s.add_argument("--changelog", help="changelog text for this version")
    s.add_argument("--key", help="Nexus API key (default: NEXUS_API_KEY)")
    s.set_defaults(fn=cmd_nexus)

    s = sub.add_parser("appeal", help="write the email for a quarantined Nexus Mods file")
    s.add_argument("file", help="the zip you uploaded")
    s.add_argument("--mod", required=True, help="mod page URL")
    s.add_argument("--user", required=True, help="your Nexus username")
    s.add_argument("--name", help="file name as shown on Nexus (default: the zip name)")
    s.add_argument("--source", default="", help="link to the source code")
    s.add_argument("--build", action="append", default=[], metavar="STEP", help="a build step (repeatable)")
    s.add_argument("--key", help="VirusTotal API key (default: VT_API_KEY)")
    s.set_defaults(fn=cmd_appeal)

    a = p.parse_args(argv)
    try:
        a.cfg = load_config(a.config)
        return a.fn(a)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tomllib.TOMLDecodeError) as e:  # OSError covers locked files and no network
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
