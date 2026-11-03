"""python -m unittest  (no network, no game files)"""
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from modrelease import appeal, clean, pack
from modrelease.cli import main


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def mod(self) -> Path:
        m = self.d / "mod"
        (m / "sub").mkdir(parents=True)
        (m / "b.txt").write_text("hello")
        (m / "sub" / "a.pak").write_bytes(b"\x00pak")
        (m / "Thumbs.db").write_bytes(b"junk")
        return m


class PackTests(Tmp):
    def test_same_input_same_bytes(self):
        m = self.mod()
        a, b = self.d / "a.zip", self.d / "b.zip"
        pack.write_zip(pack.collect([m]), a, "Game/Paks/~mods")
        (m / "b.txt").touch()  # a newer file date must not matter
        pack.write_zip(pack.collect([m]), b, "Game/Paks/~mods")
        self.assertEqual(a.read_bytes(), b.read_bytes())
        z = zipfile.ZipFile(a)
        self.assertEqual(z.namelist(), ["Game/Paks/~mods/b.txt", "Game/Paks/~mods/sub/a.pak"])  # sorted, no Thumbs.db
        self.assertEqual({i.date_time for i in z.infolist()}, {(1980, 1, 1, 0, 0, 0)})
        self.assertIn("  a.zip", (self.d / "a.zip.sha256").read_text())

    def test_finds_login_paths_and_utf16(self):
        terms = ["jdoe", "jdoe@mail.com"]
        found = [f.what for f in pack.scan_bytes("x", b"log C:\\Users\\JDoe\\Desktop\\mod", terms)]
        self.assertTrue(any("'jdoe'" in w for w in found))
        self.assertTrue(pack.scan_bytes("x", "built by JDOE".encode("utf-16-le"), terms))
        self.assertFalse(pack.scan_bytes("x", b"nothing here", terms))

    def test_other_user_folders_are_notes(self):
        notes = [f.what for f in pack.user_folders("x", b"log C:\\Users\\JDoe\\Desktop\\mod")]
        self.assertTrue(any("(jdoe)" in w for w in notes))
        self.assertTrue(pack.user_folders("x", b'{"p": "C:\\\\Users\\\\alice\\\\x"}'))   # JSON-escaped path
        self.assertFalse(pack.user_folders("x", b"C:\\Users\\Public\\Documents"))      # shared folder is fine
        self.assertFalse(pack.user_folders("x", b"/opt/build/lib"))

    def test_pack_refuses_leaks_unless_forced(self):
        m = self.mod()
        (m / "config.ini").write_text("author=Jane Roe")
        out = self.d / "out.zip"
        self.assertEqual(main(["pack", str(m), "-o", str(out), "--term", "jane roe"]), 1)
        self.assertFalse(out.exists())
        self.assertEqual(main(["pack", str(m), "-o", str(out), "--term", "jane roe", "--force"]), 0)
        self.assertTrue(out.exists())

    def test_notes_for_nexus_quarantine(self):
        m = self.mod()
        (m / "tool.exe").write_bytes(b"MZ")
        (m / "inner.zip").write_bytes(b"PK")
        _, notes = pack.check(pack.collect([m]), [])
        self.assertEqual(sorted(n.file for n in notes), ["inner.zip", "tool.exe"])


class CleanTests(Tmp):
    def test_exif_is_gone(self):
        src = self.d / "shot.jpg"
        exif = Image.Exif()
        exif[0x010F] = "PhoneMaker"     # Make
        exif[0x8825] = {2: (55.0, 45.0, 0.0)}  # GPS
        Image.new("RGB", (8, 8), "red").save(src, exif=exif, comment=b"secret")
        self.assertTrue(pack.image_metadata("shot.jpg", src.read_bytes()))
        dst = self.d / "out" / "shot.jpg"
        clean.clean_image(src, dst)
        self.assertFalse(pack.image_metadata("shot.jpg", dst.read_bytes()))
        self.assertNotIn(b"PhoneMaker", dst.read_bytes())
        with Image.open(dst) as im:
            self.assertEqual(im.size, (8, 8))

    def test_png_text_chunks_are_gone(self):
        from PIL import PngImagePlugin
        src = self.d / "a.png"
        info = PngImagePlugin.PngInfo()
        info.add_text("Author", "Real Name")
        Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(src, pnginfo=info)
        dst = self.d / "out" / "a.png"
        clean.clean_image(src, dst)
        self.assertNotIn(b"Real Name", dst.read_bytes())
        with Image.open(dst) as im:
            self.assertEqual(im.mode, "RGBA")

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_video_tags_are_gone(self):
        src, dst = self.d / "v.mp4", self.d / "out" / "v.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5:duration=1",
                        "-c:v", "libx264", "-metadata", "title=My PC", "-metadata", "creation_time=2026-01-02T03:04:05Z",
                        str(src)], check=True)
        clean.clean_video(src, dst)
        data = dst.read_bytes()
        for leak in (b"My PC", b"x264 - core", b"Lavf"):
            self.assertNotIn(leak, data)


class AppealTests(Tmp):
    def test_lists_executables(self):
        z = self.d / "tool.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("Tool/Tool.exe", b"MZ")
            f.writestr("Tool/a.dll", b"MZ")
            f.writestr("Tool/b.dll", b"MZ")
            f.writestr("Tool/readme.txt", b"hi")
        text = appeal.email(mod_url="https://nexus/mods/1", file_name="Tool 1.0", username="me",
                            vt_link="https://vt/x", contents=appeal.executables(z), build=["pip install x"])
        self.assertIn("- Tool/Tool.exe", text)
        self.assertIn("- 2 .dll libraries", text)
        self.assertNotIn("readme", text)
        self.assertIn("1. pip install x", text)
        self.assertTrue(text.rstrip().endswith("me"))


if __name__ == "__main__":
    unittest.main()
