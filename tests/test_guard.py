"""modrelease guard in throwaway git repos. HOME, the global git config and the modrelease config folder all point
into a temp folder, so the real ones are never read or written."""
import getpass
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from modrelease import guard
from modrelease.cli import main

ALIAS, EMAIL = "myalias", "123+myalias@users.noreply.github.com"


class GuardBase(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        (self.d / "home").mkdir()
        (self.d / "gitconfig").write_text("")
        env = {"HOME": str(self.d / "home"), "GIT_CONFIG_GLOBAL": str(self.d / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
               "MODRELEASE_CONFIG_DIR": str(self.d / "cfg"), "TZ": "UTC",
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        drop = [k for k in os.environ if k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_")) or k == "CI"]
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in drop:
            os.environ.pop(k)
        self.config()

    def config(self, persona_extra: str = "", utc: str = "true", name: str = ALIAS, deny: str = '"Jane Roe", "jane.roe@example.com"'):
        (self.d / "cfg").mkdir(exist_ok=True)
        (self.d / "cfg" / "guard.toml").write_text(
            f"deny = [{deny}]\n\n[[persona]]\npath = '{(self.d / 'alias').as_posix()}'\n"
            f"name = \"{name}\"\nemail = \"{EMAIL}\"\nutc = {utc}\n{persona_extra}", encoding="utf-8")

    def git(self, *args, cwd, env=None):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              env={**os.environ, **(env or {})})

    def repo(self, where: str, name=ALIAS, email=EMAIL) -> Path:
        r = self.d / where
        r.mkdir(parents=True)
        self.git("init", "-q", "-b", "main", cwd=r)
        self.git("config", "user.name", name, cwd=r)
        self.git("config", "user.email", email, cwd=r)
        return r

    def commit(self, r: Path, text="hello\n", msg="change", *flags, env=None, file="a.txt"):
        with open(r / file, "a", encoding="utf-8") as f:
            f.write(text)
        self.git("add", "-A", cwd=r)
        return self.git("commit", "-q", "-m", msg, *flags, cwd=r, env=env)

    def install(self, apply=True) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["guard", "install"] + (["--apply"] if apply else [])), 0)
        return out.getvalue()

    def global_hooks(self) -> str:
        return self.git("config", "--global", "core.hooksPath", cwd=self.d).stdout.strip()


class HookCase(GuardBase):
    def setUp(self):
        super().setUp()
        self.install()
        self.r = self.repo("alias/mod")

    def refused(self, r, *needles):
        self.assertNotEqual(r.returncode, 0, r.stderr)
        for n in needles:
            self.assertIn(n, r.stderr)


class PreCommitTests(HookCase):
    def test_persona_identity_in_utc_passes(self):
        r = self.commit(self.r)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_wrong_email_refused(self):
        self.git("config", "user.email", "alice@example.com", cwd=self.r)
        self.refused(self.commit(self.r), "alice@example.com", "this folder belongs to myalias")

    def test_agent_override_per_command_refused(self):
        r = self.git("-c", "user.email=agent@example.com", "commit", "-q", "--allow-empty", "-m", "x", cwd=self.r)
        self.refused(r, "agent@example.com")
        self.refused(self.commit(self.r, "x\n", "x", "--author", "alice <alice@example.com>"), "author is alice")

    def test_local_time_zone_refused(self):
        self.refused(self.commit(self.r, env={"TZ": "UTC-3"}), "+0300", "TZ=UTC")
        self.refused(self.git("commit", "-q", "--allow-empty", "-m", "x", "--date", "2026-01-01T10:00:00+0200",
                              cwd=self.r), "+0200")

    def test_utc_false_allows_local_time(self):
        self.config(utc="false")
        r = self.commit(self.r, env={"TZ": "UTC-3"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_deny_list_in_staged_lines_any_case(self):
        self.refused(self.commit(self.r, "made by JANE ROE\n"), "'Jane Roe'")
        self.git("reset", "-q", "--hard", cwd=self.r)
        with open(self.r / "b.txt", "wb") as f:
            f.write("contact jane.roe@example.com".encode("utf-16-le"))
        self.git("add", "-A", cwd=self.r)
        self.refused(self.git("commit", "-q", "-m", "x", cwd=self.r), "jane.roe@example.com")

    def test_login_and_home_are_denied_automatically(self):
        self.refused(self.commit(self.r, f"log /home/{getpass.getuser()}/x\n"), getpass.getuser())

    def test_non_ascii_persona_name(self):
        # git prints UTF-8; with PYTHONUTF8=0 a text-mode read on Windows decodes it with the ANSI code page
        self.config(name="Zoë")
        self.git("config", "user.name", "Zoë", cwd=self.r)
        r = self.commit(self.r, env={"PYTHONUTF8": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_outside_persona_folders_nothing_is_checked(self):
        r = self.repo("work/app", "alice", "alice@example.com")
        res = self.commit(r, "made by Jane Roe\n", "x\n\nCo-Authored-By: bot <bot@example.com>", env={"TZ": "UTC-3"})
        self.assertEqual(res.returncode, 0, res.stderr)


class CommitMsgTests(HookCase):
    def test_trailers_refused(self):
        self.refused(self.commit(self.r, msg="fix\n\nCo-Authored-By: Bot <noreply@example.com>"), "Co-Authored-By")
        self.refused(self.commit(self.r, msg="fix\n\nGenerated with [Claude Code](https://claude.com)"), "attribution")

    def test_trailers_allowed_when_configured(self):
        self.config("refuse_trailers = false\n")
        r = self.commit(self.r, msg="fix\n\nCo-authored-by: friend <friend@example.com>")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_deny_list_in_message(self):
        self.refused(self.commit(self.r, msg="thanks Jane Roe"), "the message contains 'Jane Roe'")


class ChainTests(GuardBase):
    def test_repo_hooks_still_run_inside_and_outside(self):
        self.install()
        for where in ("alias/mod", "work/app"):
            r = self.repo(where)
            hook = r / ".git" / "hooks" / "pre-commit"
            hook.write_text("#!/bin/sh\necho ran > ran.txt\n", newline="\n")
            hook.chmod(0o755)
            self.assertEqual(self.commit(r).returncode, 0)
            self.assertTrue((r / "ran.txt").exists(), where)

    def test_previous_hooks_path_is_chained_and_restored(self):
        old = self.d / "oldhooks"
        old.mkdir()
        (old / "pre-commit").write_text("#!/bin/sh\necho refused by the old hook >&2\nexit 1\n", newline="\n")
        (old / "post-merge").write_text("#!/bin/sh\nexit 0\n", newline="\n")
        self.git("config", "--global", "core.hooksPath", old.as_posix(), cwd=self.d)
        out = self.install()
        self.assertIn(str(old.as_posix()), out)
        self.assertTrue(guard._same(self.global_hooks(), self.d / "cfg" / "hooks"))
        r = self.commit(self.repo("work/app"))
        self.assertIn("refused by the old hook", r.stderr)
        self.install()  # a second install keeps the old folder chained
        self.assertIn(old.as_posix(), (self.d / "cfg" / "hooks" / "pre-commit").read_text())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["guard", "uninstall"]), 0)
        self.assertEqual(self.global_hooks(), old.as_posix())
        self.assertEqual(sorted(p.name for p in old.iterdir()), ["post-merge", "pre-commit"])
        self.assertFalse((self.d / "cfg" / "hooks").exists())

    def test_without_apply_git_config_is_untouched(self):
        out = self.install(apply=False)
        self.assertEqual(self.global_hooks(), "")
        self.assertIn("git config --global core.hooksPath", out)
        self.assertTrue((self.d / "cfg" / "hooks" / "pre-commit").is_file())
        self.git("config", "--global", "core.hooksPath", (self.d / "cfg" / "hooks").as_posix(), cwd=self.d)
        with redirect_stdout(io.StringIO()) as out:
            main(["guard", "uninstall"])
        self.assertIn("stays", out.getvalue())  # the user set it, so the user removes it
        self.assertNotEqual(self.global_hooks(), "")

    def test_uninstall_removes_setting_it_added(self):
        self.install()
        with redirect_stdout(io.StringIO()):
            main(["guard", "uninstall"])
        self.assertEqual(self.global_hooks(), "")

    def test_foreign_file_in_hooks_folder_is_not_overwritten(self):
        (self.d / "cfg" / "hooks").mkdir(parents=True)
        (self.d / "cfg" / "hooks" / "pre-commit").write_text("#!/bin/sh\nmine\n")
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(main(["guard", "install"]), 2)
        self.assertIn("not written by modrelease", err.getvalue())
        self.assertEqual((self.d / "cfg" / "hooks" / "pre-commit").read_text(), "#!/bin/sh\nmine\n")


class PrePushTests(GuardBase):
    def setUp(self):
        super().setUp()
        self.install()
        self.git("init", "-q", "--bare", str(self.d / "remote.git"), cwd=self.d)
        self.r = self.repo("alias/mod")
        self.git("remote", "add", "origin", str(self.d / "remote.git"), cwd=self.r)

    def push(self):
        return self.git("push", "-q", "origin", "main", cwd=self.r)

    def test_persona_commits_push(self):
        self.commit(self.r)
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_foreign_author_refused(self):
        self.commit(self.r, "x\n", "x", "--no-verify", "--author", "alice <alice@example.com>")
        r = self.push()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("author is alice <alice@example.com>", r.stderr)
        self.assertIn("git fetch origin", r.stderr)

    def test_local_dates_refused(self):
        self.commit(self.r, "x\n", "x", "--no-verify", env={"TZ": "UTC-3"})
        r = self.push()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("+0300", r.stderr)

    def test_only_commits_not_on_the_remote_are_checked(self):
        self.commit(self.r, "x\n", "x", "--no-verify", "--author", "alice <alice@example.com>")
        self.assertEqual(self.git("push", "-q", "--no-verify", "origin", "main", cwd=self.r).returncode, 0)
        self.commit(self.r, "y\n")
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_chained_pre_push_gets_the_refs(self):
        hook = self.r / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\ncat > refs.txt\n", newline="\n")
        hook.chmod(0o755)
        self.commit(self.r)
        self.assertEqual(self.push().returncode, 0)
        self.assertIn("refs/heads/main", (self.r / "refs.txt").read_text())


class CheckTests(GuardBase):
    def run_check(self, path) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["guard", "check", str(path)])
        return code, out.getvalue()

    def test_history_with_leaks(self):
        r = self.repo("alias/mod")
        self.commit(r)
        self.commit(r, "by Jane Roe\n", "x", "--no-verify")
        self.commit(r, "z\n", "z", "--no-verify", "--author", "alice <alice@example.com>", env={"TZ": "UTC-3"})
        code, out = self.run_check(r)
        self.assertEqual(code, 1)
        self.assertIn("adds 'Jane Roe'", out)
        self.assertIn("author is alice", out)
        self.assertIn("+0300", out)

    def test_clean_history(self):
        r = self.repo("alias/mod")
        self.commit(r)
        code, out = self.run_check(r)
        self.assertEqual(code, 0, out)
        self.assertIn("clean", out)

    def test_without_persona_lists_identities(self):
        r = self.repo("work/app", "alice", "alice@example.com")
        self.commit(r, env={"TZ": "UTC-3"})
        code, out = self.run_check(r)
        self.assertEqual(code, 0)
        self.assertIn("alice <alice@example.com>", out)
        self.assertIn("+0300", out)


class ConfigTests(GuardBase):
    def test_bad_config(self):
        (self.d / "cfg" / "guard.toml").write_text("deny = []\n[[persona]]\npath = 'x'\nname = 'a'\nmail = 'b'\n")
        with self.assertRaisesRegex(ValueError, "persona.mail"):
            guard.load()

    def test_deny_terms(self):
        cfg = {"deny": ["Jane Roe", "myalias"], "persona": []}
        terms = guard.deny_terms(cfg, {"name": ALIAS, "email": EMAIL})
        self.assertIn("Jane Roe", terms)
        self.assertIn(getpass.getuser(), terms)
        self.assertNotIn("myalias", terms)  # the persona's own name is never denied

    def test_deepest_persona_wins(self):
        cfg = {"persona": [{"path": str(self.d), "name": "a"}, {"path": str(self.d / "x"), "name": "b"}]}
        self.assertEqual(guard.persona_for(cfg, self.d / "x" / "y")["name"], "b")
        self.assertEqual(guard.persona_for(cfg, self.d / "y")["name"], "a")
        self.assertIsNone(guard.persona_for(cfg, self.d.parent))


if __name__ == "__main__":
    unittest.main()
