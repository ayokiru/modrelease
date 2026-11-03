"""python -m unittest  (no network; video tests need ffmpeg)"""
import io
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from PIL import Image

from modrelease import appeal, clean, pack, virustotal
from modrelease.cli import main


def scan(data: bytes, terms: list[str]):
    return pack.scan_stream(io.BytesIO(data), pack.term_needles(terms))


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


class ScanTests(unittest.TestCase):
    def test_login_any_case_and_utf16(self):
        self.assertEqual(scan(b"log C:\\Users\\JDoe\\Desktop", ["jdoe"])[0], ["jdoe"])
        self.assertEqual(scan("built by JDOE".encode("utf-16-le"), ["jdoe"])[0], ["jdoe"])
        self.assertEqual(scan(b"nothing here", ["jdoe"])[0], [])

    def test_short_logins(self):
        # "max" is everywhere in programs (max(), SetMaxSize): only a path segment or user folder counts
        self.assertEqual(scan(b"SetMaxSize max(a, b) max_speed", ["max"])[0], [])
        self.assertEqual(scan(b"D:\\mods\\max\\build", ["max"])[0], ["max"])
        self.assertEqual(scan("/home/max/".encode("utf-16-le"), ["max"])[0], ["max"])
        problems, _ = pack.check_file("a.log", io.BytesIO(b"home=C:\\Users\\max"), pack.term_needles(["max"]))
        self.assertEqual([p.what for p in problems], ["contains 'max'"])
        # 4-5 letters: a whole word only
        self.assertEqual(scan(b"Mikesoft MIKE_LIB", ["mike"])[0], [])
        self.assertEqual(scan(b"made by Mike.", ["mike"])[0], ["mike"])

    def test_non_latin_name_any_case(self):
        self.assertEqual(scan("автор: ДМИТРИЙ".encode("utf-8"), ["Дмитрий"])[0], ["Дмитрий"])

    def test_pieces_overlap(self):
        with mock.patch.object(pack, "CHUNK", 8):
            terms, users, _ = scan(b"xxxxxxxxxxx C:\\Users\\jdoe-laptop\\x and jdoe", ["jdoe"])
        self.assertEqual(terms, ["jdoe"])
        self.assertEqual(users, {b"jdoe-laptop"})  # not "jdo" and "jdoe-l" from the cut

    def test_user_folders(self):
        self.assertEqual(scan(b'{"p": "C:\\\\Users\\\\alice\\\\x"}', [])[1], {b"alice"})  # JSON-escaped path
        self.assertEqual(scan(b"C:\\Users\\Public\\Documents", [])[1], set())            # shared folder
        self.assertEqual(scan(b"see https://github.com/users/bob/repos", [])[1], set())  # a web address


class SecretAndDevFileTests(unittest.TestCase):
    def test_secrets_are_named_not_shown(self):
        cases = {
            b"hook=https://discord.com/api/webhooks/123456789012/AbCdEfGhIjKlMnOpQrStUvWxYz0123": "a Discord webhook URL",
            b"token: ghp_" + b"a1B2" * 9: "a GitHub token",
            b"aws_key=AKIAZ7Q3KXP2LMN4R5T6": "an AWS access key",
            b"-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA" + b"x" * 40: "a private key",
        }
        for data, label in cases.items():
            self.assertEqual(scan(data, [])[2], [label], data[:20])

    def test_lookalikes_are_not_secrets(self):
        for data in (b"AKIAIOSFODNN7EXAMPLE",                     # AWS's documentation key
                     b"-----BEGIN PRIVATE KEY-----\x00",          # the PEM header string inside OpenSSL
                     b"https://discord.com/api/webhooks/{id}/{token}"):
            self.assertEqual(scan(data, [])[2], [], data)

    def test_dev_files_and_folders(self):
        _, notes = pack.check_file("Mod.pdb", io.BytesIO(b""), [])
        self.assertIn("debug symbols", notes[0].what)
        _, notes = pack.check_names(["Saved/Logs/x.log", "Saved/Config/y.ini", "Paks/mod.pak"])
        self.assertEqual([n.file for n in notes], ["Saved/"])  # one note per folder, not per file


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

    def test_failed_write_keeps_the_old_zip(self):
        m = self.mod()
        out = self.d / "out.zip"
        out.write_bytes(b"old release")
        items = pack.collect([m]) + [(self.d / "gone.bin", "gone.bin")]
        with self.assertRaises(FileNotFoundError):
            pack.write_zip(items, out)
        self.assertEqual(out.read_bytes(), b"old release")
        self.assertFalse((self.d / "out.zip.part").exists())

    def test_pack_refuses_leaks_unless_forced(self):
        m = self.mod()
        (m / "config.ini").write_text("author=Jane Roe")
        out = self.d / "out.zip"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["pack", str(m), "-o", str(out), "--term", "jane roe"]), 1)
            self.assertFalse(out.exists())
            self.assertEqual(main(["pack", str(m), "-o", str(out), "--term", "jane roe", "--force"]), 0)
        self.assertTrue(out.exists())

    def test_zip_inside_the_mod_folder_is_not_packed_again(self):
        m = self.mod()
        out = m / "release.zip"
        with redirect_stdout(io.StringIO()):
            main(["pack", str(m), "-o", str(out)])
            main(["pack", str(m), "-o", str(out)])
        self.assertEqual(zipfile.ZipFile(out).namelist(), ["b.txt", "sub/a.pak"])

    def test_notes_for_nexus_quarantine(self):
        m = self.mod()
        (m / "tool.exe").write_bytes(b"MZ")
        (m / "inner.zip").write_bytes(b"PK")
        _, notes = pack.check(pack.collect([m]), [])
        self.assertEqual(sorted(n.file for n in notes), ["inner.zip", "tool.exe"])

    def test_own_folder_is_a_problem_not_also_a_note(self):
        problems, notes = pack.check_file("log.txt", io.BytesIO(b"C:\\Users\\jdoe\\x"), pack.term_needles(["jdoe"]))
        self.assertEqual(len(problems), 1)
        self.assertEqual(notes, [])

    def test_broken_zip_is_an_error_not_a_traceback(self):
        bad = self.d / "bad.zip"
        bad.write_bytes(b"not a zip")
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(main(["check", str(bad)]), 2)
        self.assertIn("error:", err.getvalue())


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

    def test_phone_photo_stays_upright(self):
        src = self.d / "phone.jpg"
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: rotate 90 to display
        Image.new("RGB", (40, 20), "blue").save(src, exif=exif)
        clean.clean_image(src, src)  # in place
        with Image.open(src) as im:
            self.assertEqual(im.size, (20, 40))

    def test_png_text_gone_transparency_kept(self):
        from PIL import PngImagePlugin
        info = PngImagePlugin.PngInfo()
        info.add_text("Author", "Real Name")
        src = self.d / "a.png"
        Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(src, pnginfo=info)
        pal = self.d / "p.png"
        Image.new("P", (4, 4), 0).save(pal, transparency=0)
        clean.clean([src, pal], self.d / "out")
        self.assertNotIn(b"Real Name", (self.d / "out" / "a.png").read_bytes())
        with Image.open(self.d / "out" / "p.png") as im:
            self.assertEqual(im.info.get("transparency"), 0)

    def test_same_names_in_two_folders(self):
        for sub in ("a", "b"):
            (self.d / "shots" / sub).mkdir(parents=True)
            Image.new("RGB", (4, 4), "red").save(self.d / "shots" / sub / "1.jpg")
        clean.clean([self.d / "shots"], self.d / "out")
        self.assertTrue((self.d / "out" / "a" / "1.jpg").exists())
        self.assertTrue((self.d / "out" / "b" / "1.jpg").exists())

    def test_animated_image_is_refused(self):
        src = self.d / "anim.webp"
        frames = [Image.new("RGB", (4, 4), c) for c in ("red", "blue")]
        frames[0].save(src, save_all=True, append_images=frames[1:])
        with self.assertRaises(ValueError):
            clean.clean_image(src, self.d / "out.webp")

    def _video(self, codec: str, settings: bytes):
        src, dst = self.d / f"{codec}.mp4", self.d / "out" / f"{codec}.mp4"
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1",
                            "-c:v", codec, "-metadata", "title=My PC", str(src)], capture_output=True)
        if r.returncode:
            self.skipTest(f"ffmpeg has no {codec}")
        self.assertIn(settings, src.read_bytes())
        clean.clean_video(src, dst)
        data = dst.read_bytes()
        for leak in (b"My PC", settings, b"Lavf"):
            self.assertNotIn(leak, data)
        frames = lambda p: subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
                                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(p)],
                                         capture_output=True, text=True).stdout.strip()
        self.assertEqual(frames(src), frames(dst))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_h264_video(self):
        self._video("libx264", b"x264 - core")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_h265_video(self):
        self._video("libx265", b"x265 (build")


class VirusTotalTests(Tmp):
    def setUp(self):
        super().setUp()
        self.f = self.d / "m.zip"
        self.f.write_bytes(b"zip")

    def test_flagged_engines(self):
        answer = {"data": {"attributes": {
            "last_analysis_stats": {"malicious": 1, "undetected": 67},
            "last_analysis_results": {"Bkav Pro": {"category": "malicious", "result": "W32.Malware"},
                                      "ESET-NOD32": {"category": "undetected", "result": None}}}}}
        with mock.patch.object(virustotal, "_call", return_value=answer):
            r = virustotal.report(self.f, "key")
        self.assertTrue(r["known"])
        self.assertEqual(r["flagged"], [("Bkav Pro", "W32.Malware")])

    def test_upload_still_scanning(self):
        calls = iter([None, {"data": {"attributes": {"status": "queued"}}}])
        with mock.patch.object(virustotal, "_call", side_effect=lambda *a, **k: next(calls)), \
                mock.patch.object(virustotal, "_upload", return_value="analysis-1"):
            r = virustotal.report(self.f, "key", upload=True, wait=0)
        self.assertTrue(r["pending"])
        self.assertFalse(r["known"])


class AppealTests(Tmp):
    def test_lists_programs_and_counts_libraries(self):
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

    def test_clean_result_wording(self):
        z = self.d / "tool.zip"
        zipfile.ZipFile(z, "w").close()
        clean_vt = {"known": True, "pending": False, "stats": {"undetected": 70}, "flagged": []}
        with mock.patch.object(virustotal, "report", return_value=clean_vt), redirect_stdout(io.StringIO()) as out:
            main(["appeal", str(z), "--mod", "m", "--user", "u", "--key", "k"])
        self.assertIn("none of 70 engines flag it", out.getvalue())


if __name__ == "__main__":
    unittest.main()
