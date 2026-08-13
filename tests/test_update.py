"""`dictate update`, all of it, on a machine that is not his.

Nobody who wrote this has Windows, his GitHub sign-in, or his install. So every
decision the command makes is a plain function over files and text, and this
drives all of them for real: real archives built here and unpacked, real files
written and replaced and rolled back, real journals, real refusals.

What is NOT here, and cannot be: `gh auth login` opening a browser, and Windows
remembering the sign-in afterwards. The GitHub CLI is behind one seam - a
callable that takes its arguments and returns what it said - and every branch
through that seam, including each way it can be refused, is exercised below with
GitHub's own words.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from dictate import instance, update
from dictate.errors import DictateError

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Building the things the real command is handed
# ---------------------------------------------------------------------------


#: The smallest tree `check_source_tree` will accept, which is also the smallest
#: tree that is honestly dictate: the package, the command line, the installer
#: and the project file that names it.
def source_tree(**files: str) -> dict[str, str]:
    tree = {
        "pyproject.toml": '[project]\nname = "dictate"\nversion = "0.1.0"\n',
        "setup.ps1": "# installer\n",
        "src/dictate/__init__.py": '__version__ = "0.1.0"\n',
        "src/dictate/cli.py": "def main():\n    return 0\n",
        "README.md": "# dictate\n",
    }
    tree.update(files)
    return tree


def write_file(path: Path, text: str) -> Path:
    """Write a file the way a source archive holds one: with LF line endings.

    Not a detail. Python on Windows turns every "\\n" into "\\r\\n" on the way
    out, while a `.tar.gz` from GitHub carries the bytes it was made with - so a
    tree written the ordinary way here would differ from the identical tree in
    the archive, in every single file, and only on Windows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        write_file(root / relative, text)
    return root


def tarball(files: dict[str, str], top: str = "brchue-ux-dictate_v1-4f2a1c9") -> bytes:
    """A GitHub source archive, shaped exactly the way GitHub shapes one: one
    folder at the top named after the repository and the revision."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative, text in sorted(files.items()):
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{top}/{relative}")
            info.size = len(data)
            info.mtime = 1_700_000_000
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def commits_payload(*subjects: str) -> str:
    return json.dumps([{"sha": f"{i:040x}", "commit": {"message": s}}
                       for i, s in enumerate(subjects)])


def head_payload(sha: str, subject: str = "the newest thing") -> str:
    return json.dumps({
        "sha": sha,
        "commit": {"message": subject,
                   "committer": {"date": "2026-08-13T21:04:00Z"}},
    })


class Gh:
    """Stands in for the GitHub CLI, at the one seam it is behind.

    It answers the three things dictate asks - who is signed in, what the branch
    is on, and the source itself - and records what it was asked, so a test can
    prove that a command which said it changed nothing also *fetched* nothing.
    """

    def __init__(self, *, head: str = "b" * 40, subjects=("did the thing",),
                 files: dict[str, str] | None = None, auth_code: int = 0,
                 api: dict[str, tuple[int, bytes]] | None = None) -> None:
        self.head_sha = head
        self.subjects = subjects
        self.files = files if files is not None else source_tree()
        self.auth_code = auth_code
        self.api = api or {}
        self.asked: list[str] = []

    def __call__(self, args: list[str]) -> update.ToolResult:
        self.asked.append(" ".join(args))
        if args[0] == "auth":
            return update.ToolResult(self.auth_code, b"", b"not logged in")
        path = args[1]
        for prefix, (code, body) in self.api.items():
            if path.startswith(prefix):
                return update.ToolResult(code, body if code == 0 else b"",
                                         b"" if code == 0 else body)
        if "/commits/" in path:
            return update.ToolResult(0, head_payload(self.head_sha).encode())
        if "/compare/" in path:
            payload = json.dumps({"commits": [
                {"commit": {"message": s}} for s in reversed(self.subjects)]})
            return update.ToolResult(0, payload.encode())
        if path.startswith("repos/") and "/commits?" in path:
            return update.ToolResult(0, commits_payload(*self.subjects).encode())
        if "/tarball/" in path:
            return update.ToolResult(0, tarball(self.files))
        raise AssertionError(f"nothing asks for {path}")


def spawn_ok(argv, **_kwargs):
    """A fresh interpreter, standing in only for the process boundary.

    It really compiles the code that was just written, so "the new version does
    not load" is decided by the new version rather than by the test - which is
    what makes the roll-back tests below mean anything. `verify_install` reads
    the first line of what it prints as the file it loaded.
    """
    root = Path(os.environ["DICTATE_TEST_ROOT"])
    if "-c" not in argv:
        return SimpleNamespace(returncode=0, stdout="everything is in place",
                               stderr="")
    for module in ("src/dictate/__init__.py", "src/dictate/cli.py"):
        try:
            compile((root / module).read_text(encoding="utf-8"), module, "exec")
        except (SyntaxError, OSError) as exc:
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr=f"{type(exc).__name__}: {exc}")
    return SimpleNamespace(
        returncode=0, stderr="",
        stdout=f"{root}/src/dictate/__init__.py\n0.1.0\n")


def spawn_never_works(argv, **_kwargs):
    if "-c" in argv:
        return SimpleNamespace(returncode=1, stdout="",
                               stderr="ImportError: no dictate here at all")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class TempState(unittest.TestCase):
    """A disposable state directory and a disposable install folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._previous = os.environ.get("DICTATE_STATE_DIR")
        os.environ["DICTATE_STATE_DIR"] = str(self.tmp / "state")
        self.addCleanup(self._restore)
        self.root = write_tree(self.tmp / "install", source_tree())
        os.environ["DICTATE_TEST_ROOT"] = str(self.root)
        self.said: list[str] = []

    def _restore(self):
        if self._previous is None:
            os.environ.pop("DICTATE_STATE_DIR", None)
        else:
            os.environ["DICTATE_STATE_DIR"] = self._previous
        os.environ.pop("DICTATE_TEST_ROOT", None)
        self._tmp.cleanup()

    def say(self, message: str = "") -> None:
        self.said.append(message)

    @property
    def output(self) -> str:
        return "\n".join(self.said)

    def stamp(self, revision: str = "a" * 40, files: dict | None = None) -> None:
        update.write_stamp(self.root, update.Stamp(
            revision=revision,
            files=update.tree_files(self.root) if files is None else files,
        ))

    def run_update(self, gh: Gh | None = None, **kwargs):
        gh = gh or Gh()
        return update.update(
            say=self.say, root=self.root,
            github=update.GitHub(update.DEFAULT_REPO, run=gh),
            restart=kwargs.pop("restart", False),
            spawn=kwargs.pop("spawn", spawn_ok), **kwargs)


# ---------------------------------------------------------------------------
# Which folder
# ---------------------------------------------------------------------------


class WhichFolderIsTheInstall(unittest.TestCase):
    """The confusion that cost an evening: updating a folder Python does not
    load. The answer comes from the module's own location, never from a name."""

    def test_it_finds_the_folder_this_very_module_lives_in(self):
        self.assertEqual(update.find_install_root(), REPO)
        self.assertTrue((update.find_install_root() / "pyproject.toml").exists())

    def test_it_follows_the_module_it_is_given_rather_than_a_constant(self):
        # Compared against a path resolved the same way: on Windows a
        # drive-relative "/somewhere" resolves onto the current drive, and the
        # point here is the three levels up, not the spelling.
        found = update.find_install_root("/somewhere/else/src/dictate/update.py")
        self.assertEqual(found, Path("/somewhere/else").resolve())

    def test_a_folder_that_is_not_a_source_tree_is_refused_with_a_way_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DictateError) as ctx:
                update.check_install_root(Path(tmp))
        self.assertIn("not a copy of its source folder", ctx.exception.message)
        self.assertIn("setup.ps1", ctx.exception.remedy)

    def test_the_real_install_folder_passes(self):
        update.check_install_root(REPO)   # raises if it does not


# ---------------------------------------------------------------------------
# Knowing which version this is
# ---------------------------------------------------------------------------


class WhatVersionThisIs(TempState):
    """`dictate --version` says 0.1.0 and always will. This is the thing that
    can actually answer "do I need an update?"."""

    def test_it_round_trips(self):
        self.stamp(revision="c" * 40)
        read = update.read_stamp(self.root)
        self.assertEqual(read.revision, "c" * 40)
        self.assertEqual(read.short, "ccccccc")
        self.assertIn("src/dictate/cli.py", read.files)

    def test_no_stamp_at_all_is_not_an_error(self):
        self.assertIsNone(update.read_stamp(self.root))

    def test_an_unreadable_stamp_is_an_empty_one_rather_than_a_crash(self):
        update.stamp_path(self.root).write_text("{ not json", encoding="utf-8")
        stamp = update.read_stamp(self.root)
        self.assertEqual(stamp.revision, "")
        self.assertIn("not recorded", stamp.describe())

    def test_setup_records_the_checksums_even_with_no_revision_to_record(self):
        """The ZIP install. There is no revision in a ZIP anywhere, but the
        checksums alone already answer "which of these files have you edited",
        which is the question that decides whether an update is safe."""
        update.record_install("", root=self.root)
        stamp = update.read_stamp(self.root)
        self.assertEqual(stamp.revision, "")
        self.assertEqual(stamp.installed_by, "setup.ps1")
        self.assertIn("src/dictate/cli.py", stamp.files)

        (self.root / "src" / "dictate" / "cli.py").write_text("mine", encoding="utf-8")
        edits = update.local_edits(update.tree_files(self.root), stamp.files)
        self.assertEqual(edits.changed, ("src/dictate/cli.py",))

    def test_setup_records_the_revision_when_it_ran_on_a_clone(self):
        update.record_install("  " + "d" * 40 + "\n", root=self.root)
        self.assertEqual(update.read_stamp(self.root).revision, "d" * 40)

    def test_the_stamp_is_not_part_of_the_tree_it_describes(self):
        """Otherwise every update would see the file it just wrote as a change,
        and the folder would never match the revision it holds."""
        self.stamp()
        self.assertNotIn(update.STAMP_NAME, update.tree_files(self.root))


# ---------------------------------------------------------------------------
# What changed, in plain language
# ---------------------------------------------------------------------------


class WhatChanged(unittest.TestCase):
    def test_a_merge_shows_the_pull_request_title_not_the_branch_name(self):
        message = ("Merge pull request #8 from brchue-ux/fm/dictate-orphan-server"
                   "\n\nAn orphaned whisper-server is now impossible")
        self.assertEqual(update.subject_of(message),
                         "An orphaned whisper-server is now impossible")

    def test_an_ordinary_commit_is_its_first_line(self):
        self.assertEqual(update.subject_of("fix: the tray no longer blocks\n\nlong"),
                         "fix: the tray no longer blocks")

    def test_bookkeeping_merges_are_dropped_and_repeats_shown_once(self):
        subjects = update.plain_subjects([
            "Merge branch 'main' into fm/thing",
            "Merge pull request #8 from x/y\n\nStarting at logon",
            "Starting at logon",
            "fix: a real change",
        ])
        self.assertEqual(subjects, ("Starting at logon", "fix: a real change"))

    def test_a_comparison_is_shown_newest_first(self):
        payload = json.dumps({"commits": [
            {"commit": {"message": "the oldest"}},
            {"commit": {"message": "the newest"}},
        ]})
        changes = update.parse_comparison(payload)
        self.assertEqual(changes.subjects, ("the newest", "the oldest"))
        self.assertTrue(changes.exact)

    def test_recent_history_says_it_is_not_the_exact_answer(self):
        changes = update.parse_recent(commits_payload("one", "two"))
        self.assertEqual(changes.subjects, ("one", "two"))
        self.assertFalse(changes.exact)
        self.assertIn("most recent", changes.note)

    def test_a_base_github_does_not_know_falls_back_and_says_so(self):
        gh = Gh(subjects=("one", "two"),
                api={"repos/x/y/compare": (1, b"gh: No commit found (HTTP 404)")})
        changes = update.GitHub("x/y", run=gh).changes("dead" * 10, "b" * 40, "main")
        self.assertEqual(changes.subjects, ("one", "two"))
        self.assertFalse(changes.exact)
        self.assertIn("could not compare", changes.note)

    def test_not_being_able_to_list_the_changes_does_not_stop_the_update(self):
        """Being unable to say WHAT changed is no reason to refuse to change it."""
        gh = Gh(api={"repos/x/y/compare": (1, b"HTTP 500"),
                     "repos/x/y/commits?": (1, b"HTTP 500")})
        changes = update.GitHub("x/y", run=gh).changes("a" * 40, "b" * 40, "main")
        self.assertEqual(changes.subjects, ())
        self.assertIn("would not list", changes.note)

    def test_the_head_of_a_branch_is_read_out_of_githubs_own_shape(self):
        revision = update.parse_revision(head_payload("f" * 40, "tidy: the tray"))
        self.assertEqual(revision.short, "fffffff")
        self.assertEqual(revision.subject, "tidy: the tray")
        self.assertEqual(revision.date, "2026-08-13")


# ---------------------------------------------------------------------------
# Signing in, and being refused
# ---------------------------------------------------------------------------


class TheSignIn(unittest.TestCase):
    """He types one command, once. Every way that can go wrong has to name the
    command that fixes it, because nobody can guess `gh auth login`."""

    def test_not_signed_in_is_said_before_anything_is_attempted(self):
        gh = Gh(auth_code=1)
        with self.assertRaises(DictateError) as ctx:
            update.GitHub(run=gh).require_sign_in()
        self.assertIn("sign in to GitHub once", ctx.exception.message)
        self.assertIn("gh auth login", ctx.exception.remedy)
        # And nothing was fetched: the message arrives instead of a failure.
        self.assertEqual(gh.asked, ["auth status --hostname github.com"])

    def test_the_cli_not_being_installed_names_how_to_install_it(self):
        with self.assertRaises(DictateError) as ctx:
            update.run_gh(["api", "x"], find=lambda: None,
                          spawn=lambda *a, **k: None)
        self.assertIn("GitHub CLI", ctx.exception.message)
        self.assertIn("winget install --id GitHub.cli", ctx.exception.remedy)
        self.assertIn("gh auth login", ctx.exception.remedy)

    def test_a_rejected_sign_in_later_is_told_apart_from_being_offline(self):
        error = update.api_failure(
            "repos/x/y", update.ToolResult(1, b"", b"gh: Bad credentials (HTTP 401)"),
            "x/y")
        self.assertIn("refused the sign-in", error.message)
        self.assertIn("gh auth login", error.remedy)

    def test_a_private_repository_is_not_reported_as_a_deleted_one(self):
        """404 on a private repository is what "you are not signed in" looks
        like. Saying only "not found" sends him looking for a repository that is
        sitting right there."""
        error = update.api_failure(
            "repos/x/y", update.ToolResult(1, b"", b"gh: Not Found (HTTP 404)"), "x/y")
        self.assertIn("private repository", error.remedy)
        self.assertIn("gh auth login", error.remedy)

    def test_being_offline_says_so_and_says_nothing_was_changed(self):
        error = update.api_failure(
            "repos/x/y", update.ToolResult(1, b"", b"dial tcp: no such host"), "x/y")
        self.assertIn("could not reach GitHub", error.message)
        self.assertIn("Nothing has been changed", error.remedy)

    def test_it_looks_for_gh_where_winget_puts_it_not_only_on_path(self):
        """The Vulkan SDK's lesson: a program installed a minute ago is on disk
        long before the window you are typing in has been told about it."""
        found = update.find_gh(
            which=lambda _name: None, env={"ProgramFiles": r"C:\Program Files"},
            exists=lambda p: "GitHub CLI" in p)
        self.assertIn("GitHub CLI", found)
        self.assertTrue(found.endswith("gh.exe"))

    def test_gh_not_being_anywhere_is_answered_with_nothing_rather_than_a_guess(self):
        self.assertIsNone(update.find_gh(which=lambda _name: None, env={},
                                         exists=lambda _p: False))

    def test_nothing_token_shaped_ever_reaches_the_screen(self):
        for secret in ("ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                       "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
                       "github_pat_11ABCDEFG0abcdefghijklmnop",
                       "Authorization: Bearer sekrit"):
            with self.subTest(secret=secret):
                cleaned = update.redact(f"gh said: {secret} while asking")
                self.assertNotIn(secret.split()[-1], cleaned)
                self.assertIn("(removed)", cleaned)

    def test_what_the_cli_said_is_redacted_on_the_way_out(self):
        result = update.ToolResult(1, b"", b"header Authorization: Bearer ghp_" + b"a" * 30)
        self.assertNotIn("ghp_", result.said)


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------


class TheArchive(TempState):
    def test_the_wrapper_folder_github_adds_is_stripped(self):
        into = self.tmp / "staging"
        found = update.extract_source(tarball(source_tree()), into)
        self.assertTrue((found / "src" / "dictate" / "cli.py").exists())
        self.assertEqual(found.parent, into)

    def test_a_path_that_escapes_the_folder_is_refused_and_nothing_unpacked(self):
        """An archive is the one thing here that arrives from outside and is
        written to disk by path. `..` in a member name is how that becomes
        "write anywhere on this PC"."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name in ("top/ok.txt", "top/../../escaped.txt"):
                info = tarfile.TarInfo(name)
                info.size = 2
                archive.addfile(info, io.BytesIO(b"hi"))
        with self.assertRaises(DictateError) as ctx:
            update.extract_source(buffer.getvalue(), self.tmp / "staging")
        self.assertIn("unsafe path", ctx.exception.message)
        self.assertFalse((self.tmp / "escaped.txt").exists())

    def test_links_are_dropped_rather_than_written(self):
        info = tarfile.TarInfo("top/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        ordinary = tarfile.TarInfo("top/real.txt")
        ordinary.size = 0
        kept = update.safe_members([info, ordinary])
        self.assertEqual([m.name for m in kept], ["top/real.txt"])

    def test_an_archive_that_is_not_dictate_is_refused_before_anything_is_copied(self):
        files = source_tree()
        files["pyproject.toml"] = '[project]\nname = "something-else"\n'
        found = update.extract_source(tarball(files), self.tmp / "staging")
        with self.assertRaises(DictateError) as ctx:
            update.check_source_tree(found)
        self.assertIn("some other project", ctx.exception.message)

    def test_an_archive_missing_half_the_program_is_refused(self):
        files = source_tree()
        del files["src/dictate/cli.py"]
        found = update.extract_source(tarball(files), self.tmp / "staging")
        with self.assertRaises(DictateError) as ctx:
            update.check_source_tree(found)
        self.assertIn("src/dictate/cli.py", ctx.exception.message)
        self.assertIn("Nothing has been changed", ctx.exception.remedy)

    def test_rubbish_instead_of_an_archive_is_a_message_not_a_traceback(self):
        with self.assertRaises(DictateError) as ctx:
            update.extract_source(b"not a tarball at all" * 100, self.tmp / "staging")
        self.assertIn("could not be unpacked", ctx.exception.message)


# ---------------------------------------------------------------------------
# Deciding what to write, and what he has edited
# ---------------------------------------------------------------------------


class WhatToChange(unittest.TestCase):
    def test_only_files_whose_content_differs_are_written(self):
        current = {"a.py": "1", "b.py": "2"}
        incoming = {"a.py": "1", "b.py": "changed", "c.py": "new"}
        plan = update.plan_apply(current, incoming, manifest=current)
        self.assertEqual(plan.write, ("b.py", "c.py"))
        self.assertEqual(plan.delete, ())

    def test_a_file_the_last_update_wrote_and_this_one_does_not_is_removed(self):
        current = {"a.py": "1", "gone.py": "2"}
        plan = update.plan_apply(current, {"a.py": "1"}, manifest=current)
        self.assertEqual(plan.delete, ("gone.py",))

    def test_with_no_manifest_nothing_is_ever_deleted(self):
        """The first update, of a folder that came out of a ZIP. dictate does not
        know which files were once part of the application and which are his, and
        guessing wrong means deleting something of his."""
        current = {"src/dictate/cli.py": "1", "his-notes.txt": "2"}
        plan = update.plan_apply(current, {"src/dictate/cli.py": "9"}, manifest=None)
        self.assertEqual(plan.delete, ())
        self.assertEqual(plan.write, ("src/dictate/cli.py",))

    def test_identical_trees_plan_nothing_at_all(self):
        same = {"a.py": "1"}
        self.assertTrue(update.plan_apply(same, same, manifest=same).empty)

    def test_his_edits_are_named_one_by_one(self):
        manifest = {"a.py": "1", "b.py": "2", "c.py": "3"}
        current = {"a.py": "1", "b.py": "HIS EDIT"}
        edits = update.local_edits(current, manifest)
        self.assertEqual(edits.changed, ("b.py",))
        self.assertEqual(edits.removed, ("c.py",))
        self.assertTrue(edits.any)

    def test_with_nothing_to_compare_against_it_claims_nothing(self):
        """"No local edits" would be a claim it cannot make."""
        self.assertFalse(update.local_edits({"a.py": "1"}, None).any)

    def test_build_leavings_are_not_part_of_the_application(self):
        for path in ("__pycache__/cli.cpython-312.pyc", "src/dictate.egg-info/PKG-INFO",
                     "dictate.log", ".venv/lib/thing.py", ".git/config",
                     "src/dictate/cli.py" + update.TEMP_SUFFIX):
            with self.subTest(path=path):
                self.assertTrue(update.is_ignored(path))
        for path in ("src/dictate/cli.py", ".github/workflows/ci.yml",
                     "config/dictate.example.toml", ".gitignore"):
            with self.subTest(path=path):
                self.assertFalse(update.is_ignored(path))


# ---------------------------------------------------------------------------
# Writing files
# ---------------------------------------------------------------------------


class WritingFiles(TempState):
    def test_a_file_is_never_half_written(self):
        """Written beside the target and renamed over it. The temporary name is
        gone afterwards, and the target is whole."""
        target = self.root / "src" / "dictate" / "cli.py"
        source = self.tmp / "new.py"
        write_file(source, "brand new\n")
        update.copy_file_atomically(source, target)
        self.assertEqual(target.read_text(encoding="utf-8"), "brand new\n")
        self.assertFalse(target.with_name(target.name + update.TEMP_SUFFIX).exists())

    def test_removing_the_last_file_in_a_folder_removes_the_folder(self):
        (self.root / "old").mkdir()
        (self.root / "old" / "thing.py").write_text("x", encoding="utf-8")
        update.apply_plan(self.root, self.root, update.Plan(delete=("old/thing.py",)))
        self.assertFalse((self.root / "old").exists())

    def test_a_backup_is_a_complete_copy_without_the_build_leavings(self):
        (self.root / "__pycache__").mkdir()
        (self.root / "__pycache__" / "x.pyc").write_bytes(b"junk")
        backup = self.tmp / "backup"
        update.copy_tree(self.root, backup)
        self.assertTrue((backup / "src" / "dictate" / "cli.py").exists())
        self.assertFalse((backup / "__pycache__").exists())

    def test_putting_a_backup_back_removes_what_was_added_as_well(self):
        backup = self.tmp / "backup"
        update.copy_tree(self.root, backup)
        (self.root / "src" / "dictate" / "cli.py").write_text("broken", encoding="utf-8")
        (self.root / "src" / "dictate" / "added.py").write_text("new", encoding="utf-8")
        plan = update.restore_tree(backup, self.root)
        self.assertEqual(update.tree_files(self.root), update.tree_files(backup))
        self.assertIn("src/dictate/cli.py", plan.write)
        self.assertIn("src/dictate/added.py", plan.delete)


# ---------------------------------------------------------------------------
# The whole command
# ---------------------------------------------------------------------------


class TheWholeCommand(TempState):
    def test_an_up_to_date_copy_fetches_nothing_and_changes_nothing(self):
        self.stamp(revision="b" * 40)
        gh = Gh(head="b" * 40)
        outcome = self.run_update(gh)
        self.assertTrue(outcome.up_to_date)
        self.assertIn("already up to date", self.output)
        self.assertNotIn("tarball", " ".join(gh.asked))

    def test_it_updates_and_says_what_changed_in_plain_language(self):
        self.stamp(revision="a" * 40)
        new = source_tree()
        new["src/dictate/cli.py"] = "def main():\n    return 1\n"
        new["src/dictate/brand_new.py"] = "# added upstream\n"
        outcome = self.run_update(Gh(files=new, subjects=(
            "An orphaned whisper-server is now impossible", "tidy: the tray")))

        self.assertTrue(outcome.ok)
        self.assertIn("An orphaned whisper-server is now impossible", self.output)
        self.assertEqual((self.root / "src" / "dictate" / "cli.py")
                         .read_text(encoding="utf-8"), "def main():\n    return 1\n")
        self.assertTrue((self.root / "src" / "dictate" / "brand_new.py").exists())
        self.assertEqual(update.read_stamp(self.root).revision, "b" * 40)

    def test_it_never_touches_the_toolchain_the_build_or_the_models(self):
        """The whole value of the command. It writes inside the install folder
        and nowhere else, and it says so."""
        self.stamp(revision="a" * 40)
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        self.run_update(Gh(files=new))
        self.assertNotIn("whisper.cpp", self.output)
        self.assertNotIn("model", self.output.lower())
        self.assertNotIn("setup.ps1 -Only build", self.output)

    def test_a_file_deleted_upstream_goes_when_the_last_update_put_it_there(self):
        self.stamp(revision="a" * 40)
        (self.root / "src" / "dictate" / "gone.py").write_text("old", encoding="utf-8")
        self.stamp(revision="a" * 40)   # re-record, so gone.py is in the manifest
        self.run_update(Gh(files=source_tree()))
        self.assertFalse((self.root / "src" / "dictate" / "gone.py").exists())

    def test_check_says_what_would_change_and_changes_nothing(self):
        self.stamp(revision="a" * 40)
        before = update.tree_files(self.root)
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        outcome = self.run_update(Gh(files=new), check_only=True)
        self.assertTrue(outcome.checked_only)
        self.assertEqual(update.tree_files(self.root), before)
        self.assertIn("nothing has been changed", self.output)
        self.assertIn("dictate update", self.output)

    def test_a_zip_install_that_is_already_current_records_its_revision(self):
        """The first `dictate update` on a folder that came from a ZIP of the
        current code: nothing needs writing, and from then on the question is
        answerable in a second without downloading anything."""
        outcome = self.run_update(Gh(files=source_tree()))
        self.assertTrue(outcome.up_to_date)
        self.assertEqual(outcome.written, ())
        self.assertEqual(update.read_stamp(self.root).revision, "b" * 40)
        self.assertIn("nothing needs changing", self.output)

    def test_without_a_manifest_it_says_it_cannot_tell_what_he_edited(self):
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        self.run_update(Gh(files=new))
        self.assertIn("cannot tell which files you have", self.output)

    def test_his_edits_stop_it_and_name_the_files(self):
        self.stamp(revision="a" * 40)
        write_file(self.root / "README.md", "# my own notes\n")
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        with self.assertRaises(DictateError) as ctx:
            self.run_update(Gh(files=new))
        self.assertIn("README.md", ctx.exception.message)
        self.assertIn("--force", ctx.exception.remedy)
        # And it stopped BEFORE writing anything.
        self.assertEqual((self.root / "src" / "dictate" / "cli.py")
                         .read_text(encoding="utf-8"), "def main():\n    return 0\n")

    def test_force_overwrites_his_edits_but_keeps_a_copy_and_says_where(self):
        self.stamp(revision="a" * 40)
        write_file(self.root / "README.md", "# my own notes\n")
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        outcome = self.run_update(Gh(files=new), force=True)
        self.assertIn("README.md", self.output)
        kept = Path(outcome.backup) / "README.md"
        self.assertEqual(kept.read_text(encoding="utf-8"), "# my own notes\n")

    def test_the_settings_folder_is_never_opened(self):
        """His config lives in %APPDATA%\\dictate. Everything this command writes
        is inside the install folder or the state folder, and this proves it by
        watching every path that is written."""
        self.stamp(revision="a" * 40)
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        written: list[Path] = []
        real = update.copy_file_atomically

        def watched(source, target):
            written.append(Path(target))
            real(source, target)

        update.copy_file_atomically = watched
        try:
            self.run_update(Gh(files=new))
        finally:
            update.copy_file_atomically = real
        self.assertTrue(written)
        for path in written:
            self.assertTrue(path.is_relative_to(self.root)
                            or path.is_relative_to(self.tmp / "state"), path)


class WhenItGoesWrong(TempState):
    def test_a_new_version_that_will_not_load_is_put_back_by_itself(self):
        self.stamp(revision="a" * 40)
        before = update.tree_files(self.root)
        new = source_tree()
        new["src/dictate/cli.py"] = "def main(:\n"      # a syntax error

        with self.assertRaises(DictateError) as ctx:
            self.run_update(Gh(files=new))
        self.assertIn("would not load", ctx.exception.message)
        self.assertIn("has been put back", ctx.exception.message)
        self.assertEqual(update.tree_files(self.root), before)
        self.assertIsNone(update.read_journal())

    def test_an_update_that_appeared_to_work_but_loaded_another_folder_is_caught(self):
        """The bug that has bitten him twice: everything reports success and
        nothing has changed, because Python is loading a different folder."""
        self.stamp(revision="a" * 40)
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"

        def spawn_elsewhere(argv, **_kwargs):
            if "-c" in argv:
                return SimpleNamespace(returncode=0, stderr="",
                                       stdout="C:\\somewhere\\else\\dictate\\__init__.py\n0.1.0\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with self.assertRaises(DictateError):
            self.run_update(Gh(files=new), spawn=spawn_elsewhere)
        self.assertEqual((self.root / "src" / "dictate" / "cli.py")
                         .read_text(encoding="utf-8"), "def main():\n    return 0\n")

    def test_a_file_that_will_not_be_written_puts_the_old_version_straight_back(self):
        """A virus scanner holding one file open, which is the realistic way an
        apply stops half way. The install must never be left part old and part
        new, and it must never end in a traceback."""
        self.stamp(revision="a" * 40)
        before = update.tree_files(self.root)
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        new["README.md"] = "# changed too\n"

        real = update.copy_file_atomically
        state = {"writes": 0}

        def refuses_the_second(source, target):
            # The second write of the update, and only that one - so a file
            # really has been replaced when it stops, and the roll-back that
            # follows is free to write as many as it needs.
            state["writes"] += 1
            if state["writes"] == 2:
                raise PermissionError(f"{target} is in use by another process")
            real(source, target)

        update.copy_file_atomically = refuses_the_second
        try:
            with self.assertRaises(DictateError) as ctx:
                self.run_update(Gh(files=new))
        finally:
            update.copy_file_atomically = real
        self.assertIn("could not write the new version", ctx.exception.message)
        self.assertIn("dictate update", ctx.exception.remedy)
        self.assertEqual(update.tree_files(self.root), before)
        self.assertIsNone(update.read_journal())

    def test_check_on_a_folder_that_already_matches_writes_nothing_at_all(self):
        """--check means --check. Even the record of the revision - which it
        would happily have written - waits for him to ask for it."""
        outcome = self.run_update(Gh(files=source_tree()), check_only=True)
        self.assertTrue(outcome.up_to_date)
        self.assertIsNone(update.read_stamp(self.root))
        self.assertIn("nothing needs changing", self.output)

    def test_when_even_the_roll_back_does_not_help_it_names_the_folder_to_copy(self):
        """The worst case there is. It must not end in a shrug: the complete
        copy of his install is somewhere, and this says exactly where."""
        self.stamp(revision="a" * 40)
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        with self.assertRaises(DictateError) as ctx:
            self.run_update(Gh(files=new), spawn=spawn_never_works)
        self.assertIn("putting the previous one back did not fix it",
                      ctx.exception.message)
        self.assertIn(str(self.tmp / "state" / "updates"), ctx.exception.remedy)
        self.assertIn("dictate doctor", ctx.exception.remedy)

    def test_pip_is_run_only_when_the_package_list_actually_changed(self):
        """An editable install is a pointer at this folder, so replacing the
        files under it IS the update. pip is seconds he does not have to spend
        unless `pyproject.toml` itself moved."""
        ran: list[list[str]] = []

        def spawn(argv, **kwargs):
            if "pip" in argv:
                ran.append(argv)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return spawn_ok(argv, **kwargs)

        self.stamp(revision="a" * 40)
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        self.run_update(Gh(files=new), spawn=spawn)
        self.assertEqual(ran, [])

        self.stamp(revision="a" * 40)
        new["pyproject.toml"] += '\ndependencies = ["something-new"]\n'
        outcome = self.run_update(Gh(head="c" * 40, files=new), spawn=spawn)
        self.assertTrue(outcome.reinstalled)
        self.assertEqual(len(ran), 1)
        self.assertIn("-e", ran[0])

    def test_pip_failing_does_not_undo_an_update_that_worked(self):
        """The code is in; only the package list may be behind. It says the one
        command that finishes the job rather than throwing the update away."""
        def spawn(argv, **kwargs):
            if "pip" in argv:
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr="ERROR: could not reach pypi.org")
            return spawn_ok(argv, **kwargs)

        self.stamp(revision="a" * 40)
        new = source_tree()
        new["pyproject.toml"] += '\ndependencies = ["something-new"]\n'
        outcome = self.run_update(Gh(files=new), spawn=spawn)
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.reinstalled)
        self.assertIn("pip did not finish", self.output)
        self.assertIn("pip install -e", self.output)

    def test_an_interrupted_update_whose_backup_is_gone_says_so_and_carries_on(self):
        update.write_journal(update.Journal(
            root=str(self.root), backup=str(self.tmp / "vanished")))
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        self.run_update(Gh(files=new))
        self.assertIn("cannot put it back", self.output)
        self.assertEqual((self.root / "src" / "dictate" / "cli.py")
                         .read_text(encoding="utf-8"), "changed\n")
        self.assertIsNone(update.read_journal())

    def test_an_interrupted_update_is_put_back_the_next_time_he_runs_it(self):
        """The half-replaced install. The journal is what makes this
        "interrupted" rather than "broken"."""
        good = update.tree_files(self.root)
        backup = self.tmp / "state" / "updates" / "backup-20260813-210000-aaaaaaa"
        update.copy_tree(self.root, backup)
        # A half-finished apply: one file is the new version, one is not.
        write_file(self.root / "src" / "dictate" / "cli.py", "half\n")
        update.write_journal(update.Journal(
            root=str(self.root), backup=str(backup), from_revision="a" * 40,
            to_revision="b" * 40, started="2026-08-13 21:00:00"))

        self.run_update(Gh(files=source_tree()))
        self.assertIn("did not finish", self.output)
        self.assertEqual(update.tree_files(self.root), good)
        self.assertIsNone(update.read_journal())

    def test_a_journal_for_some_other_folder_is_left_alone(self):
        update.write_journal(update.Journal(
            root="C:\\some\\other\\install", backup=str(self.tmp / "backup")))
        (self.tmp / "backup").mkdir()
        self.run_update(Gh(files=source_tree()))
        self.assertIn("left alone", self.output)

    def test_doctor_sees_an_interrupted_update_and_names_the_way_out(self):
        from dictate import doctor

        update.write_journal(update.Journal(root=str(self.root)))
        result = doctor.check_install()
        self.assertIs(result.status, doctor.Status.WARN)
        self.assertIn("did not finish", result.detail)
        self.assertIn("dictate update --restore", result.remedy)

    def test_doctor_says_where_dictate_is_loaded_from_and_which_version(self):
        """It must never report a healthy install as a problem, and it has to
        answer "which folder?" - the question that cost an evening."""
        from dictate import doctor

        result = doctor.check_install()
        self.assertIs(result.status, doctor.Status.OK)
        self.assertIn(str(REPO), result.detail)


class GoingBackOnPurpose(TempState):
    def test_restore_puts_the_previous_version_back(self):
        self.stamp(revision="a" * 40)
        before = update.tree_files(self.root)
        new = source_tree()
        new["src/dictate/cli.py"] = "changed\n"
        self.run_update(Gh(files=new))
        self.assertNotEqual(update.tree_files(self.root), before)

        self.said.clear()
        outcome = update.restore(say=self.say, root=self.root, restart=False,
                                 spawn=spawn_ok)
        self.assertTrue(outcome.ok)
        self.assertEqual(update.tree_files(self.root), before)
        self.assertEqual(update.read_stamp(self.root).revision, "a" * 40)

    def test_with_nothing_to_go_back_to_it_says_so_rather_than_failing_oddly(self):
        with self.assertRaises(DictateError) as ctx:
            update.restore(say=self.say, root=self.root, restart=False)
        self.assertIn("no copy of a previous version", ctx.exception.message)
        self.assertIn("setup.ps1", ctx.exception.remedy)

    def test_only_two_previous_versions_are_kept(self):
        updates = self.tmp / "state" / "updates"
        for stamp in ("20260101-000000-aaa", "20260102-000000-bbb",
                      "20260103-000000-ccc", "20260104-000000-ddd"):
            (updates / f"backup-{stamp}").mkdir(parents=True)
        update.prune_backups(updates)
        kept = [p.name for p in update.backups(updates)]
        self.assertEqual(kept, ["backup-20260104-000000-ddd",
                                "backup-20260103-000000-ccc"])


# ---------------------------------------------------------------------------
# Not in the middle of a sentence
# ---------------------------------------------------------------------------


class NotMidSentence(TempState):
    """It uses the machinery that landed today - the lock and the stop
    request - and it never escalates to ending anything. `dictate stop` is
    allowed to force, because he asked for it to be gone. An update has not been
    asked for that, so a copy it cannot stop cleanly is a copy it leaves alone."""

    def test_a_copy_in_the_middle_of_an_utterance_is_left_alone(self):
        lock = instance.InstanceLock(started_by="hand")
        lock.acquire()
        self.addCleanup(lock.release)
        instance.publish_activity(True, "an utterance - you are speaking")

        with self.assertRaises(DictateError) as ctx:
            update._stop_running_copy(self.say, timeout_s=0.2)
        self.assertIn("in the middle of", ctx.exception.message)
        self.assertIn("nothing has been changed", ctx.exception.message)
        self.assertIn("dictate update", ctx.exception.remedy)

    def test_a_copy_that_will_not_stop_is_not_ended_and_nothing_is_changed(self):
        lock = instance.InstanceLock(started_by="hand")
        lock.acquire()
        self.addCleanup(lock.release)
        self.addCleanup(instance.clear_stop_request)

        with self.assertRaises(DictateError) as ctx:
            update._stop_running_copy(self.say, timeout_s=0.3)
        self.assertIn("did not stop", ctx.exception.message)
        self.assertIn("nothing has been changed", ctx.exception.message)
        self.assertIn("dictate stop", ctx.exception.remedy)
        # Still running. Nothing was killed.
        self.assertIsNotNone(instance.running_instance())

    def test_nothing_running_is_simply_nothing_to_stop(self):
        self.assertIsNone(update._stop_running_copy(self.say, timeout_s=0.2))

    def test_a_busy_record_from_a_copy_that_has_gone_is_not_believed(self):
        """Otherwise one sentence somebody was half way through last week would
        refuse every update from then on."""
        holder = instance.Holder(pid=os.getpid(), started_epoch=time.time())
        stale = instance.Activity(busy=True, what="an utterance",
                                  pid=os.getpid(), at=time.time() - 3600)
        self.assertFalse(stale.belongs_to(holder))
        fresh = instance.Activity(busy=True, what="an utterance",
                                  pid=os.getpid(), at=time.time())
        self.assertTrue(fresh.belongs_to(holder))
        self.assertFalse(fresh.belongs_to(None))

    def test_a_record_from_another_process_is_not_believed_either(self):
        holder = instance.Holder(pid=4321, started_epoch=time.time())
        other = instance.Activity(busy=True, pid=1234, at=time.time())
        self.assertFalse(other.belongs_to(holder))

    def test_the_record_round_trips_through_a_file(self):
        instance.publish_activity(True, "transcribing what you just said")
        activity = instance.read_activity()
        self.assertTrue(activity.busy)
        self.assertEqual(activity.what, "transcribing what you just said")
        self.assertEqual(activity.pid, os.getpid())
        instance.clear_activity()
        self.assertIsNone(instance.read_activity())


class RestartingAfterwards(TempState):
    def test_a_copy_started_by_hand_comes_back_the_same_way(self):
        from unittest import mock

        holder = instance.Holder(pid=99, started_by="hand")
        with mock.patch("dictate.recovery.relaunch", return_value=4242) as relaunch:
            pid = update._restart(holder, None, self.say)
        self.assertEqual(pid, 4242)
        self.assertIsNone(relaunch.call_args.kwargs["executable"])
        self.assertFalse(relaunch.call_args.kwargs["autostart"])
        self.assertIn("started again on the new version", self.output)

    def test_a_copy_that_came_up_at_logon_comes_back_windowless(self):
        """Under the logon task there is no console at all. A restart that
        dropped that would start a copy that fell over on its first `print`, in a
        window that does not exist."""
        from unittest import mock

        holder = instance.Holder(pid=99, started_by="logon")
        with mock.patch("dictate.autostart.windowless_python",
                        return_value=Path(r"C:\Python\pythonw.exe")), \
             mock.patch("dictate.recovery.relaunch", return_value=7) as relaunch:
            update._restart(holder, None, self.say)
        self.assertTrue(relaunch.call_args.kwargs["autostart"])
        self.assertIn("pythonw.exe", relaunch.call_args.kwargs["executable"])

    def test_being_unable_to_restart_it_is_reported_not_hidden(self):
        from unittest import mock

        holder = instance.Holder(pid=99, started_by="hand")
        with mock.patch("dictate.recovery.relaunch",
                        side_effect=DictateError("no", "type `dictate run`")):
            self.assertIsNone(update._restart(holder, None, self.say))
        self.assertIn("dictate run", self.output)


if __name__ == "__main__":
    unittest.main()
