"""modrelease guard in throwaway git repos. HOME, the global git config and the modrelease config folder all point
into a temp folder, so the real ones are never read or written."""
import getpass
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from modrelease import guard, pack
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

    def test_rename_to_a_denied_name_refused(self):
        self.commit(self.r)
        self.git("mv", "a.txt", "Jane Roe notes.txt", cwd=self.r)
        self.refused(self.git("commit", "-q", "-m", "rename", cwd=self.r), "'Jane Roe'")

    def test_login_and_home_are_denied_automatically(self):
        self.refused(self.commit(self.r, f"log /home/{getpass.getuser()}/x\n"), getpass.getuser())

    def test_non_ascii_persona_name(self):
        # git prints UTF-8; with PYTHONUTF8=0 a text-mode read on Windows decodes it with the ANSI code page
        self.config(name="Zoë")
        self.git("config", "user.name", "Zoë", cwd=self.r)
        r = self.commit(self.r, env={"PYTHONUTF8": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_worktree_outside_the_alias_folder(self):
        self.commit(self.r)
        wt = self.d / "elsewhere" / "wt"
        self.git("worktree", "add", "-q", "-b", "feature", str(wt), cwd=self.r)
        r = self.git("-c", "user.email=agent@example.com", "commit", "-q", "--allow-empty", "-m", "x", cwd=wt)
        self.refused(r, "agent@example.com")
        self.assertEqual(self.git("commit", "-q", "--allow-empty", "-m", "x", cwd=wt).returncode, 0)

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

    def test_editor_commit_renaming_a_leaked_file(self):
        self.commit(self.r, "x\n", "x", "--no-verify", file="Jane Roe notes.txt")
        self.git("mv", "Jane Roe notes.txt", "notes.txt", cwd=self.r)
        editor = self.d / "editor.sh"  # git's template lists the old name in comment lines
        editor.write_text('#!/bin/sh\n{ echo "rename the notes"; cat "$1"; } > "$1.new" && mv "$1.new" "$1"\n', newline="\n")
        r = self.git("commit", "-q", cwd=self.r, env={"GIT_EDITOR": f"sh '{editor.as_posix()}'"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_verbose_commit_that_removes_a_leak(self):
        self.commit(self.r, "by Jane Roe\n", "x", "--no-verify")
        (self.r / "a.txt").write_text("by the author\n")
        self.git("add", "-A", cwd=self.r)
        editor = self.d / "editor.sh"  # writes a message above the template, like a person in an editor
        editor.write_text('#!/bin/sh\n{ echo "remove the name"; cat "$1"; } > "$1.new" && mv "$1.new" "$1"\n', newline="\n")
        r = self.git("commit", "-q", "-v", cwd=self.r, env={"GIT_EDITOR": f"sh '{editor.as_posix()}'"})
        self.assertEqual(r.returncode, 0, r.stderr)  # the removed line sits in the diff under the scissors


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
        for h in old.iterdir():
            h.chmod(0o755)  # git, and the chain, skip hooks that aren't executable (matters on Linux and macOS)
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

    def test_python_path_with_a_space(self):
        wrapper = self.d / "py thon $x" / "python"  # a space and a $ the shell must not expand
        wrapper.parent.mkdir()
        wrapper.write_text(f"#!/bin/sh\nexec '{Path(sys.executable).as_posix()}' \"$@\"\n", newline="\n")
        wrapper.chmod(0o755)
        with mock.patch.object(guard.sys, "executable", str(wrapper)):
            self.install()
        r = self.repo("alias/mod")
        self.assertEqual(self.commit(r).returncode, 0)
        self.git("config", "user.email", "alice@example.com", cwd=r)
        self.assertIn("alice@example.com", self.commit(r).stderr)

    def test_broken_hook_says_how_to_get_out(self):
        self.install()
        r = self.repo("alias/mod")
        self.git("config", "user.email", "alice@example.com", cwd=r)
        refused = self.commit(r)
        self.assertNotIn("--no-verify", refused.stderr)  # a refusal must not advertise the way around it
        (self.d / "cfg" / "guard.toml").write_text("deny = []\ntypo = 1\n")
        broken = self.commit(r)
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("unknown setting(s) typo", broken.stderr)
        self.assertIn("Skip once: --no-verify", broken.stderr)

    def test_uninstall_leaves_other_files_alone(self):
        self.install()
        mine = self.d / "cfg" / "hooks" / "notes.txt"
        mine.write_text("not a hook of ours")
        with redirect_stdout(io.StringIO()):
            main(["guard", "uninstall"])
        self.assertEqual([p.name for p in (self.d / "cfg" / "hooks").iterdir()], ["notes.txt"])

    def test_hook_entry_does_not_load_the_cli(self):
        code = "import sys, modrelease.guard; sys.exit(any(m in sys.modules for m in ('modrelease.cli', 'urllib.request')))"
        self.assertEqual(subprocess.run([sys.executable, "-c", code]).returncode, 0)

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
        self.assertIn("git push --no-verify", r.stderr)

    def test_merged_upstream_commits_are_known(self):
        self.git("init", "-q", "--bare", str(self.d / "upstream.git"), cwd=self.d)
        up = self.repo("work/up", "alice", "alice@example.com")
        self.commit(up, env={"TZ": "UTC-3"})
        self.git("push", "-q", str(self.d / "upstream.git"), "main", cwd=up)
        self.commit(self.r)
        self.git("remote", "add", "upstream", str(self.d / "upstream.git"), cwd=self.r)
        self.git("fetch", "-q", "upstream", cwd=self.r)
        m = self.git("merge", "-q", "--allow-unrelated-histories", "-m", "merge upstream", "upstream/main", cwd=self.r)
        self.assertEqual(m.returncode, 0, m.stderr)
        r = self.push()  # alice's commit is on upstream/main, so it is not this persona's to answer for
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_deleting_a_branch_or_tag(self):
        self.commit(self.r)
        self.git("tag", "-a", "v1", "-m", "v1", cwd=self.r)
        self.assertEqual(self.git("push", "-q", "origin", "main", "main:feature", "v1", cwd=self.r).returncode, 0)
        for ref in (":feature", ":refs/tags/v1"):
            r = self.git("push", "-q", "origin", ref, cwd=self.r)
            self.assertEqual(r.returncode, 0, r.stderr)

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

    def test_annotated_tag_with_foreign_tagger_refused(self):
        self.commit(self.r)
        self.git("-c", "user.name=alice", "-c", "user.email=alice@example.com", "tag", "-a", "v1", "-m", "v1",
                 cwd=self.r, env={"TZ": "UTC-3"})
        r = self.git("push", "-q", "origin", "v1", cwd=self.r)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tag v1: tagger is alice <alice@example.com>", r.stderr)
        self.assertIn("+0300", r.stderr)

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

    def test_history_is_read_in_pieces(self):
        r = self.repo("alias/mod")
        self.commit(r, "x" * 5000 + " by Jane Roe " + "y" * 5000 + "\n", "first line\n\nthird line names jane.roe@example.com",
                    "--no-verify")
        with mock.patch.object(pack, "CHUNK", 64):
            code, out = self.run_check(r)
        self.assertEqual(code, 1)
        self.assertIn("adds 'Jane Roe'", out)
        self.assertIn("'jane.roe@example.com' in the author, committer or message", out)

    def test_rename_to_a_denied_name(self):
        r = self.repo("alias/mod")
        self.commit(r)
        self.git("mv", "a.txt", "Jane Roe notes.txt", cwd=r)
        self.git("commit", "-q", "--no-verify", "-m", "rename", cwd=r)
        code, out = self.run_check(r)
        self.assertIn("adds 'Jane Roe'", out)

    def test_annotated_tags(self):
        r = self.repo("alias/mod")
        self.commit(r)
        self.git("-c", "user.name=alice", "-c", "user.email=alice@example.com", "tag", "-a", "v1", "-m", "v1",
                 cwd=r, env={"TZ": "UTC-3"})
        self.git("tag", "-a", "v2", "-m", "thanks Jane Roe", cwd=r)
        self.git("tag", "light", cwd=r)
        code, out = self.run_check(r)
        self.assertEqual(code, 1)
        self.assertIn("tag v1: tagger is alice <alice@example.com>", out)
        self.assertIn("tag v1: tagger date has the offset +0300", out)
        self.assertIn("tag v2: 'Jane Roe' in the tagger or message", out)
        self.assertNotIn("tag v2: tagger", out)
        self.assertNotIn("light", out)

    def test_listed_word_inside_the_alias_name(self):
        self.config(deny='"alias"')
        self.install()
        r = self.repo("alias/mod")
        self.assertEqual(self.commit(r).returncode, 0)  # the persona's own name in the commit is fine
        self.assertEqual(self.run_check(r)[0], 0)
        res = self.commit(r, "a new alias\n")
        self.assertIn("'alias'", res.stderr)  # anywhere else the word is still refused

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
        self.git("config", "--global", "user.name", "Jane Roe", cwd=self.d)
        terms = guard.deny_terms({"deny": ["alias"], "persona": []}, {"name": ALIAS, "email": EMAIL})
        self.assertIn("alias", terms)  # listed, even though it is part of the persona's name
        self.assertIn("Jane Roe", terms)  # the global git identity, through pack.personal_terms
        self.assertIn(getpass.getuser(), terms)
        self.assertIn(Path.home().as_posix(), terms)

    def test_generic_login_and_own_identity_are_not_automatic_terms(self):
        self.git("config", "--global", "user.name", ALIAS, cwd=self.d)  # the global identity is the alias itself
        with mock.patch("getpass.getuser", return_value="User"), \
                mock.patch("pathlib.Path.home", return_value=Path("/home/Administrator")):
            terms = [t.lower() for t in guard.deny_terms({"deny": ["Admin"], "persona": []}, {"name": ALIAS, "email": EMAIL})]
        self.assertNotIn("user", terms)
        self.assertNotIn("administrator", terms)
        self.assertNotIn(ALIAS, terms)
        self.assertIn("admin", terms)  # listed by hand, so it stays

    def test_deepest_persona_wins(self):
        cfg = {"persona": [{"path": str(self.d), "name": "a"}, {"path": str(self.d / "x"), "name": "b"}]}
        self.assertEqual(guard.persona_for(cfg, self.d / "x" / "y")["name"], "b")
        self.assertEqual(guard.persona_for(cfg, self.d / "y")["name"], "a")
        self.assertIsNone(guard.persona_for(cfg, self.d.parent))


if __name__ == "__main__":
    unittest.main()
