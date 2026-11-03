"""modrelease: get a mod ready for Nexus Mods, GitHub or anywhere else.

  modrelease check  FILES...        look for your login, PC name, email and user paths, image metadata
  modrelease pack   SRC -o X.zip    the same check, then a reproducible zip and its .sha256
  modrelease clean  MEDIA... -o DIR screenshots and videos without EXIF, GPS, tags or encoder strings
  modrelease vt     X.zip           VirusTotal result for the file (VT_API_KEY)
  modrelease appeal X.zip ...       the email that gets a quarantined Nexus file reviewed
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path

from . import __version__, appeal, clean, pack, virustotal


def _print_findings(problems, notes) -> None:
    for f in problems:
        print(f"  ! {f.file}: {f.what}")
    for f in notes:
        print(f"  - {f.file}: {f.what}")


def cmd_check(a) -> int:
    terms = pack.personal_terms(a.term)
    needles = pack.term_needles(terms)
    problems, notes = [], []
    for s in a.paths:
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
            pr, no = pack.check(pack.collect([p]), terms)
            problems += pr
            notes += no
    _print_findings(problems, notes)
    print(f"{len(problems)} problem(s)" if problems else "clean")
    return 1 if problems else 0


def cmd_pack(a) -> int:
    out = Path(a.output)
    # a zip written into the mod folder must not end up inside the next one
    items = pack.collect([Path(s) for s in a.sources], skip=[out, out.with_name(out.name + ".sha256")])
    if not items:
        raise ValueError("nothing to pack: no files in " + ", ".join(a.sources))
    problems, notes = pack.check(items, pack.personal_terms(a.term))
    _print_findings(problems, notes)
    if problems and not a.force:
        print(f"{len(problems)} problem(s), nothing written. Fix them or pass --force.")
        return 1
    digest = pack.write_zip(items, out, a.prefix)
    print(f"{out} ({len(items)} files, {out.stat().st_size // 1024} KB)\nsha256 {digest}")
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
    sub = p.add_subparsers(dest="cmd", required=True)
    term = dict(action="append", default=[], metavar="TEXT", help="another string that must not ship (repeatable)")

    s = sub.add_parser("check", help="look for personal data in files, folders or a zip")
    s.add_argument("paths", nargs="+")
    s.add_argument("--term", **term)
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("pack", help="check, then write a reproducible zip and its .sha256")
    s.add_argument("sources", nargs="+", help="a folder (its contents go to the zip root) or files")
    s.add_argument("-o", "--output", required=True, help="the zip to write")
    s.add_argument("--prefix", default="", help="folder inside the zip, e.g. ProjectPBZ/Content/Paks/~mods")
    s.add_argument("--force", action="store_true", help="write the zip even if the check finds problems")
    s.add_argument("--term", **term)
    s.set_defaults(fn=cmd_pack)

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
        return a.fn(a)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as e:  # OSError covers locked files and no network
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
