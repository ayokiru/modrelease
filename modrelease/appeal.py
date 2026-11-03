"""The email that gets a quarantined Nexus Mods file reviewed: what is inside, where the source is, how to build it."""
from __future__ import annotations

import zipfile
from pathlib import Path

from .pack import EXECUTABLES


def executables(archive: Path) -> list[str]:
    """Programs by name, libraries as one count: support reads the list, a page of DLLs helps nobody."""
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if Path(n).suffix.lower() in EXECUTABLES]
    programs = [n for n in names if Path(n).suffix.lower() != ".dll"]
    libs = len(names) - len(programs)
    return programs + ([f"{libs} .dll librar{'y' if libs == 1 else 'ies'} that come with them"] if libs else [])


def email(*, mod_url: str, file_name: str, username: str, source: str = "", vt_link: str = "",
          vt_summary: str = "", contents: list[str] = (), build: list[str] = ()) -> str:
    lines = ["Hi,", "", f'my file "{file_name}" on {mod_url} was quarantined.', ""]
    if vt_link:
        lines += [f"VirusTotal: {vt_summary}" if vt_summary else "VirusTotal:", vt_link, ""]
    if source:
        lines += [f"Source code: {source}", ""]
    if contents:
        lines += ["The executables in the archive:"] + [f"- {c}" for c in contents] + [""]
    if build:
        lines += ["To build it yourself:"] + [f"{i}. {step}" for i, step in enumerate(build, 1)] + [""]
    lines += ["Thanks,", username]
    return "\n".join(lines)
