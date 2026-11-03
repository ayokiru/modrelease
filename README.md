# modrelease

[![test](https://github.com/ayokiru/modrelease/actions/workflows/test.yml/badge.svg)](https://github.com/ayokiru/modrelease/actions/workflows/test.yml)

Get a game mod ready for release. One command checks your files for personal data, leaked tokens and files that
break on players' PCs, then packs a zip that comes out byte-identical every time, with its SHA-256. Others strip
screenshots and videos for the mod page, compare two releases, look the zip up on VirusTotal and upload it to
Nexus Mods. If Nexus quarantines your upload, `modrelease appeal` writes the email that gets it reviewed.

```
pipx install git+https://github.com/ayokiru/modrelease
```

Python 3.11+, works on Windows, Linux and macOS. Videos need `ffmpeg` on PATH.

<p align="center"><img src="docs/demo.gif" alt="modrelease pack stops on a log with the login in it and a screenshot with EXIF, clean strips the screenshot, then pack writes the zip and appeal writes the Nexus email" width="100%"></p>

## Why

Config files and logs in a mod archive keep `C:\Users\yourname\...` paths, compiled tools and their `.pdb` files
keep the folder they were built in, screenshots keep the camera, the GPS position and the editor, and a zip made in
Explorer keeps the time you touched every file. If you mod under a nickname, any of these can tie it to you. A
Discord webhook or a GitHub token in a config is worse: anyone who downloads the mod can use it.

Nexus also quarantines every upload that contains an executable or an archive inside the archive, and lets it out
only after a support email. That email needs the same few facts every time.

## Commands

### check

```
modrelease check MyMod/            # a folder, files, or a finished zip
```

Stops the release on:

- your login, computer name, home folder and git name and email (UTF-8 and UTF-16, any case), plus anything you add
  with `--term "My Real Name"`
- tokens and keys: Discord webhooks, GitHub, AWS, Google and Slack tokens, private keys (the finding names the kind
  of token and leaves its value out of the output)
- images with EXIF or text chunks
- file names that differ only in case, or that Windows can't create (`aux.txt`, `a:b.ini`, a trailing dot)

It also notes other people's user folders (often a library's build machine path), debug symbols (`.pdb`), source
files (`.psd`, `.blend`, `.uproject`), engine and IDE folders (`Saved/`, `Intermediate/`, `.vs/`), very long paths,
non-Latin file names and the files Nexus will quarantine.

```
  ! config/settings.ini: contains 'jdoe'
  ! config/discord.json: contains a Discord webhook URL: remove it and revoke it, it is no longer secret
  ! media/preview.jpg: image metadata (EXIF or text chunks): run `modrelease clean` on it
  - Binaries/MyMod.pdb: debug symbols: players don't need them and they hold the build folder path
  - tools/MyTool.exe: executable: Nexus quarantines these for a manual review (`modrelease appeal`)
3 problem(s)
```

### pack

```
modrelease pack build/MyMod -o MyMod-1.0.zip --prefix "Game/Content/Paks/~mods" --exclude "*.psd"
```

Runs the check and stops if it finds a problem (`--force` writes the zip anyway). The zip has one fixed date on
every file (1980-01-01, or `SOURCE_DATE_EPOCH`), sorted entries, forward slashes and no `Thumbs.db` or `.DS_Store`,
so the same files always give the same bytes. `MyMod-1.0.zip.sha256` goes next to it. `--prefix` puts the files in
the folder the game expects, so players can extract the zip straight into the game folder.

### diff

```
modrelease diff MyMod-1.0.zip MyMod-1.1.zip
```

Lists the files added, removed and changed between two releases, from the zip headers alone. Handy for the changelog,
and for spotting a file that slipped in by accident.

### clean

```
modrelease clean screenshots/ trailer.mp4 -o upload/
```

Images are rebuilt from their pixels, which drops EXIF, GPS, XMP, ICC profiles and PNG text; phone photos keep
their rotation. JPEG and WebP get encoded again at quality 95. Videos are copied without re-encoding, minus container
tags, chapters, the `Lavf` muxer string and the x264 or x265 settings block. Add `--no-audio` to drop the sound track.

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

## modrelease.toml

Put the settings next to your mod and a release is `modrelease pack` and `modrelease nexus`. Paths are relative to
the file; command line options win over it.

```toml
source = "build/MyMod"
output = "dist/MyMod-{version}.zip"
version = "1.1"
prefix = "Game/Content/Paks/~mods"
exclude = ["*.psd", "Saved/", "docs/*.md"]   # *.x: any file name, Dir/: a folder anywhere, a/b: from the root
terms = ["My Real Name"]                     # more strings that must never ship

[nexus]
file_id = 12345
mod_id = 678
category = "main"                            # main, optional or miscellaneous
```

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

- Text hidden in compressed data (game paks, nested archives) is not unpacked, so the check cannot see it.
- The check knows your login and git identity on this PC. Names from another PC or an old account need `--term`.
- Short logins are common words inside programs, so a login of up to 3 letters counts only inside a path, and one
  of 4 or 5 letters only as a whole word. A login like `mike` can still turn up in a third-party DLL's credits;
  check those findings and pack with `--force`.
- Tokens are found by their known shapes. A password typed into a config file has no shape, so read your configs.
- H.265 videos lose their HDR metadata in `clean`, since it sits in the same kind of block as the encoder settings.

## License

MIT
