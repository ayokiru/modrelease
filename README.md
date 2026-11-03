# modrelease

Get a game mod ready for release. One command checks your files for personal data, packs a zip that comes out
byte-identical every time, and writes its SHA-256. Two more strip screenshots and videos for the mod page and look the
zip up on VirusTotal. If Nexus Mods quarantines your upload, `modrelease appeal` writes the email that gets it reviewed.

```
pipx install git+https://github.com/ayokiru/modrelease
```

Python 3.10+, works on Windows, Linux and macOS. Videos need `ffmpeg` on PATH.

<p align="center"><img src="docs/demo.gif" alt="modrelease pack stops on a log with the login in it and a screenshot with EXIF, clean strips the screenshot, then pack writes the zip and appeal writes the Nexus email" width="100%"></p>

## Why

Config files and logs in a mod archive keep `C:\Users\yourname\...` paths, compiled tools keep
the folder they were built in, screenshots keep the camera, the GPS position and the editor, and a zip made in
Explorer keeps the time you touched every file. If you mod under a nickname, any of these can tie it to you.

Nexus also quarantines every upload that contains an executable or an archive inside the archive, and lets it out
only after a support email. That email needs the same few facts every time.

## Commands

### check

```
modrelease check MyMod/            # a folder, files, or a finished zip
```

Looks for your login, computer name, home folder and git name and email (UTF-8 and UTF-16, any case), plus
anything you add with `--term "My Real Name"`. Images with EXIF or text chunks count as problems too. It also
lists other people's user folders (often a library's build machine path, usually harmless) and the files Nexus
will quarantine.

```
  ! config/settings.ini: contains 'jdoe'
  ! media/preview.jpg: image metadata (EXIF or text chunks): run `modrelease clean` on it
  - tools/UAssetGUI.exe: user folder path (runner): fine if it is a third-party build path, check it isn't yours
  - tools/MyTool.exe: executable: Nexus quarantines these for a manual review (`modrelease appeal`)
2 problem(s)
```

### pack

```
modrelease pack build/MyMod -o MyMod-1.0.zip --prefix "Game/Content/Paks/~mods"
```

Runs the check and stops if it finds a problem (`--force` writes the zip anyway). The zip has one fixed date on
every file (1980-01-01, or `SOURCE_DATE_EPOCH`), sorted entries, forward slashes and no `Thumbs.db` or `.DS_Store`,
so the same files always give the same bytes. `MyMod-1.0.zip.sha256` goes next to it. `--prefix` puts the files in
the folder the game expects, so players can extract the zip straight into the game folder.

### clean

```
modrelease clean screenshots/ trailer.mp4 -o upload/
```

Images are rebuilt from their pixels, which drops EXIF, GPS, XMP, ICC profiles and PNG text. JPEG and WebP get
encoded again at quality 95. Videos are copied without re-encoding, minus container tags, chapters, the `Lavf`
muxer string and the x264 settings block. Add `--no-audio` to drop the sound track.

### vt

```
set VT_API_KEY=...        # free key: virustotal.com/gui/my-apikey
modrelease vt MyMod-1.0.zip --upload
```

Shows how many engines flag the file and which ones. `--upload` sends files VirusTotal has not seen yet, big ones
included. Without a key you get the link to look it up by hand.

### appeal

```
modrelease appeal MyTool-1.0.zip --mod https://www.nexusmods.com/game/mods/123 --user MyNick ^
  --source https://github.com/me/mytool --build "pip install -r requirements.txt" --build "python build.py"
```

Prints the email for `support@nexusmods.com`: the file, the VirusTotal result with the names of the engines that
flag it, the programs in the archive, the source link and your build steps. Send it from the address on your
Nexus account.

## Limits

- Text hidden in compressed data (game paks, nested archives) is not unpacked, so the check cannot see it.
- The check knows your login and git identity on this PC. Names from another PC or an old account need `--term`.

## License

MIT
