<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/banner-dark.png">
  <img src="docs/banner-light.png" alt="modrelease, a game mod release toolkit: check, pack and ship game mods without leaking who you are" width="100%">
</picture>

<p align="center">
  <a href="https://github.com/ayokiru/modrelease/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/ayokiru/modrelease/test.yml?branch=main&style=flat-square&label=tests" alt="Tests"></a>
  <a href="https://pypi.org/project/modrelease/"><img src="https://img.shields.io/pypi/v/modrelease?style=flat-square&color=16A34A" alt="PyPI version"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-1E293B?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Windows%20%C2%B7%20Linux-1E293B?style=flat-square" alt="Windows and Linux">
  <a href="#github-action"><img src="https://img.shields.io/badge/GitHub%20Action-ready-16A34A?style=flat-square" alt="GitHub Action"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-16A34A?style=flat-square" alt="MIT license"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &nbsp;·&nbsp;
  <a href="#commands">Commands</a> &nbsp;·&nbsp;
  <a href="#guard-your-alias-in-git">Git guard</a> &nbsp;·&nbsp;
  <a href="#modreleasetoml">Config</a> &nbsp;·&nbsp;
  <a href="#github-action">GitHub Action</a> &nbsp;·&nbsp;
  <a href="#limits">Limits</a> &nbsp;·&nbsp;
  <a href="https://pypi.org/project/modrelease/">PyPI</a>
</p>

Config files and logs in a mod archive keep `C:\Users\yourname\...` paths, tools and their `.pdb` files keep the
folder they were built in, screenshots keep the camera and the GPS position, and a zip made in Explorer keeps the
time you touched every file. A Discord webhook or a GitHub token in a config is worse: anyone who downloads the mod
can use it. modrelease finds all of that before you upload, then packs the release.

<table>
  <tr>
    <td width="33%" valign="top">
      <img src="docs/icons/scan.svg" width="32" height="32" alt=""><br>
      <b>Leak and secret scan</b><br>
      Your login, PC name and git email, Discord webhooks, tokens, private keys, EXIF, names that break on Windows.
    </td>
    <td width="33%" valign="top">
      <img src="docs/icons/pack.svg" width="32" height="32" alt=""><br>
      <b>Reproducible zip</b><br>
      Fixed dates, sorted entries and a SHA-256 next to it. The same files always give the same bytes.
    </td>
    <td width="33%" valign="top">
      <img src="docs/icons/clean.svg" width="32" height="32" alt=""><br>
      <b>Clean screenshots and videos</b><br>
      Strips EXIF, GPS and editor tags, and the x264/x265 settings from trailers without re-encoding them.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <img src="docs/icons/diff.svg" width="32" height="32" alt=""><br>
      <b>Release diff</b><br>
      Files added, removed and changed between two releases, for the changelog and for strays.
    </td>
    <td valign="top">
      <img src="docs/icons/virus.svg" width="32" height="32" alt=""><br>
      <b>VirusTotal and quarantine</b><br>
      Which engines flag the zip, and the support email Nexus asks for when it quarantines a file.
    </td>
    <td valign="top">
      <img src="docs/icons/upload.svg" width="32" height="32" alt=""><br>
      <b>Nexus upload and CI</b><br>
      A new file version on Nexus Mods from the command line or a GitHub Action on every tag.
    </td>
  </tr>
</table>

## Quick start

```bash
pipx install modrelease        # or: pip install modrelease
modrelease check MyMod/
modrelease pack MyMod/ -o MyMod-1.0.zip
```

The package is on [PyPI](https://pypi.org/project/modrelease/). `check` shows what would leak, and `pack` runs the
same check and writes the zip with its `.sha256`. Needs Python 3.11+;
videos also need `ffmpeg` on PATH.

<p align="center"><img src="docs/demo.gif" alt="modrelease pack stops on a log with the login in it and a screenshot with EXIF, clean strips the screenshot, then pack writes the zip and appeal writes the Nexus email" width="100%"></p>

## Commands

| Command | What it does |
|---|---|
| [`check`](#check) | Looks for personal data, secrets and risky files in a folder, files or a finished zip |
| [`pack`](#pack) | The same check, then a reproducible zip and its `.sha256` |
| [`clean`](#clean) | Screenshots and videos without EXIF, GPS, tags or encoder settings |
| [`diff`](#diff) | Files added, removed and changed between two release zips |
| [`vt`](#vt) | The VirusTotal result for a file, and which engines flag it |
| [`nexus`](#nexus) | Uploads the zip as a new version of a file on Nexus Mods |
| [`appeal`](#appeal) | Writes the email that gets a quarantined Nexus file reviewed |
| [`guard`](#guard-your-alias-in-git) | Git hooks that keep your real name, email and time zone out of your alias's commits |

### check

```
modrelease check MyMod/            # a folder, files, or a finished zip
```

```
  ! config/settings.ini: contains 'jdoe'
  ! config/discord.json: contains a Discord webhook URL: remove it and revoke it, it is no longer secret
  ! media/preview.jpg: image metadata (EXIF or text chunks): run `modrelease clean` on it
  - Binaries/MyMod.pdb: debug symbols: players don't need them and they hold the build folder path
  - tools/MyTool.exe: executable: Nexus quarantines these for a manual review (`modrelease appeal`)
3 problem(s)
```

<details>
<summary>What stops a release (<b>!</b>) and what is only a note (<b>-</b>)</summary>

<br>

Problems:

- your login, computer name, home folder and git name and email (UTF-8 and UTF-16, any case), plus anything you add
  with `--term "My Real Name"`
- tokens and keys: Discord webhooks, GitHub, AWS, Google and Slack tokens, private keys (the finding names the kind
  of token and leaves its value out of the output)
- images with EXIF or text chunks
- file names that differ only in case, or that Windows can't create (`aux.txt`, `a:b.ini`, a trailing dot)

Notes: other people's user folders (often a library's build machine path), debug symbols (`.pdb`), source files
(`.psd`, `.blend`, `.uproject`), engine and IDE folders (`Saved/`, `Intermediate/`, `.vs/`), very long paths,
non-Latin file names and the files Nexus will quarantine.

</details>

### pack

```
modrelease pack build/MyMod -o MyMod-1.0.zip --prefix "Game/Content/Paks/~mods" --exclude "*.psd"
```

Runs the check and stops on a problem (`--force` writes the zip anyway). Every file gets the same fixed date
(1980-01-01, or `SOURCE_DATE_EPOCH`), entries are sorted, paths use forward slashes and `Thumbs.db` or `.DS_Store`
stay out, so the same files always give the same bytes. `--prefix` puts the files in the folder the game expects,
so players can extract the zip straight into the game folder.

### clean

```
modrelease clean screenshots/ trailer.mp4 -o upload/
```

<details>
<summary>What gets removed</summary>

<br>

Images are rebuilt from their pixels, which drops EXIF, GPS, XMP, ICC profiles and PNG text; phone photos keep
their rotation. JPEG and WebP get encoded again at quality 95. Videos are copied without re-encoding, minus
container tags, chapters, the `Lavf` muxer string and the x264 or x265 settings block. `--no-audio` drops the
sound track.

</details>

### diff

```
modrelease diff MyMod-1.0.zip MyMod-1.1.zip
```

```
  + Paks/zzz_MyMod_Patch_P.pak
  - docs/old_readme.txt
  ~ Paks/zzz_MyMod_P.ucas (2,310 KB -> 2,402 KB)
1 added, 1 removed, 1 changed, 12 unchanged
```

Reads only the zip headers, so it is instant on big releases.

### vt

```
set VT_API_KEY=...        # free key: virustotal.com/gui/my-apikey
modrelease vt MyMod-1.0.zip --upload
```

Shows how many engines flag the file and which ones. `--upload` sends files VirusTotal has not seen yet, big ones
included. Without a key you get the link to look it up by hand.

### nexus

```
set NEXUS_API_KEY=...     # from the API keys page of your Nexus account settings
modrelease nexus MyMod-1.1.zip --file-id 12345 --mod-version 1.1 --archive-old
```

Uploads the zip as a new version of a file that is already on your mod page, through the Nexus Mods v3 API (the
same steps as Nexus's own upload action). `--mod-id` and `--changelog` add a changelog entry. The Nexus upload API
is still in beta.

### appeal

```
modrelease appeal MyTool-1.0.zip --mod https://www.nexusmods.com/game/mods/123 --user MyNick ^
  --source https://github.com/me/mytool --build "pip install -r requirements.txt" --build "python build.py"
```

Prints the email for `support@nexusmods.com`: the file, the VirusTotal result with the names of the engines that
flag it, the programs in the archive, the source link and your build steps. Send it from the address on your
Nexus account.

## Guard your alias in git

Every git commit stores a name, an email and the time with your UTC offset, and all three go public with the repo.
One commit made with your work email still in the global config puts your real name in the alias's history for
good, and the offset narrows down where you live. AI coding agents add one more way in: an agent can run
`git -c user.email=... commit` or pass `--author`, and whatever you set in the repo config no longer counts.

`modrelease guard` ties folders to identities and installs git hooks that check every commit made in them. Put the
settings in `~/.config/modrelease/guard.toml` (on Windows that is `%USERPROFILE%\.config\modrelease\guard.toml`,
and `MODRELEASE_CONFIG_DIR` moves the folder):

```toml
deny = ["My Real Name", "me@example.com"]

[[persona]]
path = "~/alias-projects"
name = "myalias"
email = "123+myalias@users.noreply.github.com"
utc = true
```

Then write the hooks:

```
modrelease guard install
```

They go to `~/.config/modrelease/hooks`, and install prints the git command that turns them on
(`git config --global core.hooksPath ...`). Your git config stays as it is until you run that command or
`modrelease guard install --apply`.

In a repo under a persona's `path` the hooks refuse:

| Hook | What it refuses |
|---|---|
| pre-commit | an author or committer that is not the persona (set by the config, `-c`, `--author` or `GIT_AUTHOR_*`), a date that is not +0000 when `utc = true`, staged lines that add a string from the deny list |
| commit-msg | `Co-Authored-By` and "Generated with" lines from AI tools (`refuse_trailers = false` allows them), deny list strings in the message |
| pre-push | any of the above in commits that no remote branch in your clone has yet, and in annotated tags |

The deny list always includes your login and home folder on this PC, in any letter case and in UTF-16 as well.
To commit in UTC, run `TZ=UTC git commit ...` in bash, or set `$env:TZ = "UTC"` once in PowerShell.

Outside persona folders the hooks check nothing. A global `core.hooksPath` hides each repo's own `.git/hooks`, so
the guard runs those after its checks, or the hooks from your previous global `core.hooksPath` if you had one.
`modrelease guard uninstall` deletes the hooks and puts the old `core.hooksPath` back, but only if `--apply` was
what changed it.

Before you make an old repo public, look at its history:

```
modrelease guard check path/to/repo
```

It reads every commit on every branch, lists deny list strings, other identities and time zones, and changes
nothing. Outside a persona folder it lists the identities and time zones it found and leaves the judging to you.

## modrelease.toml

Put the settings next to your mod and a release is `modrelease pack` and `modrelease nexus`. Paths are relative to
the file; command line options win over it.

```toml
source = "build/MyMod"
output = "dist/MyMod-{version}.zip"
version = "1.1"
prefix = "Game/Content/Paks/~mods"
exclude = ["*.psd", "Saved/", "docs/*.md"]
terms = ["My Real Name"]

[nexus]
file_id = 12345
mod_id = 678
category = "main"
```

In `exclude`, `*.psd` matches a file name anywhere, `Saved/` a folder anywhere and `docs/*.md` a path from the root
of the archive. `terms` are more strings that must never ship. `category` is `main`, `optional` or `miscellaneous`.

## GitHub Action

Pack on every tag, attach the zip to the GitHub release and send it to Nexus:

```yaml
on:
  push:
    tags: ["v*"]
jobs:
  release:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: ayokiru/modrelease@v0.2.0
        with:
          args: pack --mod-version ${{ github.ref_name }}
      - uses: softprops/action-gh-release@v2
        with:
          files: dist/*
      - uses: ayokiru/modrelease@v0.2.0
        with:
          args: nexus --mod-version ${{ github.ref_name }} --archive-old
        env:
          NEXUS_API_KEY: ${{ secrets.NEXUS_API_KEY }}
```

On a CI runner the login and computer name belong to the build machine, so only `terms` and `--term` are searched
for there.

## Limits

<details>
<summary>What the check can't see</summary>

<br>

- Text hidden in compressed data (game paks, nested archives) is not unpacked, so the check cannot see it.
- The check knows your login and git identity on this PC. Names from another PC or an old account need `--term`.
- Short logins are common words inside programs, so a login of up to 3 letters counts only inside a path, and one
  of 4 or 5 letters only as a whole word. A login like `mike` can still turn up in a third-party DLL's credits;
  check those findings and pack with `--force`.
- Tokens are found by their known shapes. A password typed into a config file has no shape, so read your configs.
- H.265 videos lose their HDR metadata in `clean`, since it sits in the same kind of block as the encoder settings.
- `git commit --no-verify` skips the guard hooks, and a repo that sets its own `core.hooksPath` (husky does) never
  runs them. The hooks call the Python modrelease was installed with, so run `guard install` again if that moves.
- pre-push counts a commit as new until a remote branch in your clone has it. Other people's commits that came
  in another way (a patch, a remote you removed) are refused until you `git fetch` their remote, or push once with
  `git push --no-verify`.

</details>

---

<p align="center">
  <sub>MIT license · icons from <a href="https://lucide.dev">Lucide</a> (ISC) · made by <a href="https://github.com/ayokiru">ayokiru</a></sub>
</p>
