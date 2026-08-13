"""`dictate update` - fetch the current source and put it in place, in seconds.

Five changes shipped in one evening, and each one cost the same ritual: open
GitHub, download the ZIP, unzip it, replace the folder, run setup again. This is
that ritual, done properly, in one command.

**What it does not do, and that is the point.** The build tools, the compiled
`whisper.cpp` and the ~2 GB of models live in `C:\\dictate-gpu` and do not change
when the application does. Nothing here rebuilds, re-downloads or re-installs
any of them. His settings live in `%APPDATA%\\dictate` and are never opened. What
changes is the Python source in the install folder, and nothing else. If a future
change ever does need the toolchain or a new model, the thing to do is say so and
name `setup.ps1 -Only <step>` - never to spend half an hour he did not ask for.

**Which folder.** Not `C:\\dictate_v1` by assumption: the folder Python actually
loads dictate from, worked out from this very file. The install is editable
(`pip install -e`), so the source tree *is* the installed program; updating some
other copy of it would appear to succeed and change nothing, which is a confusion
that has already cost an evening once.

**Where the source comes from, and why not git.** His folder came out of a ZIP,
so there is no repository in it to pull into, and turning it into one would mean
`git init` followed by a hard reset over files nobody has checked - the one
operation that can destroy his edits without being able to name them first. So
the source is fetched as a source archive over the authenticated GitHub API,
extracted somewhere else entirely, and only then compared with what he has.

**How it cannot leave him broken.** In order:

1. Everything that can fail - signing in, fetching, unpacking, checking that what
   came back really is dictate - happens **outside the install folder**. Not one
   byte of his install is touched until a complete, verified tree is on disk.
2. If nothing actually differs, it stops there. Nothing is stopped, nothing is
   written, nothing is restarted.
3. Otherwise a complete copy of the install folder is taken first, and a journal
   file records that an update is in progress. That file is the difference
   between "interrupted" and "broken": the next `dictate update` (and
   `dictate doctor`) sees it and puts the backup back.
4. Files are replaced one at a time by writing beside the target and renaming
   over it, so no single file is ever half-written, whatever happens.
5. Afterwards the new code is made to prove it imports, in a fresh interpreter.
   If it does not, the backup goes back automatically and it says so.

**The credential.** dictate never sees one. The GitHub CLI is what talks to
GitHub; it reads the sign-in Windows already remembers, and dictate only ever
runs `gh` and reads what it prints. There is nothing here to print by accident,
nothing to write into the install folder, and nothing kept in a file. What `gh`
prints is passed through `redact()` before it reaches the screen anyway, because
"we would never print a token" is worth being a function rather than a habit.

Everything that decides *what to do* is plain and injectable, exactly like
`recovery.py`, so `tests/test_update.py` drives all of it on any platform - the
archive handling, the file plan, the local-edit refusal, the journal, the
rollback. The two things that cannot be exercised anywhere but his PC are the
one-time `gh auth login` in a browser and Windows remembering it afterwards.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import instance, recovery
from .errors import DictateError

log = logging.getLogger(__name__)

#: Where the source comes from. There is one of these, and it is this product's
#: own home; `--repo` exists so CI can point the same code at the same repository
#: by name rather than by luck.
DEFAULT_REPO = "brchue-ux/dictate_v1"
DEFAULT_BRANCH = "main"

#: What this copy is, written into the install folder after every successful
#: update (and by `setup.ps1` at install time). It records the revision AND a
#: SHA-256 of every file that came from it, which is what makes "you have edited
#: three of these files" answerable rather than guessable.
STAMP_NAME = ".dictate-install.json"

#: Written before the first file is touched, deleted after the last one. Its
#: presence means an update did not finish.
JOURNAL_NAME = "in-progress.json"

#: Written beside the file being replaced, then renamed over it. Never left
#: behind on any path that completes.
TEMP_SUFFIX = ".dictate-new"

#: How many previous versions are kept. Two is enough to go back after an update
#: that looked fine and was not, and small enough that nobody has to think about
#: disk space.
KEEP_BACKUPS = 2

#: Never copied, never compared, never deleted: build leavings, virtual
#: environments and logs are not part of the application.
IGNORED_NAMES = frozenset({
    "__pycache__", ".git", ".venv", "venv", "build", "dist",
    ".pytest_cache", ".mypy_cache", "node_modules",
})
IGNORED_SUFFIXES = (".pyc", ".pyo", ".log", ".egg-info", TEMP_SUFFIX)

#: What a source tree has to have in it before anything is copied out of it. A
#: partial download that unpacked cleanly is still not dictate.
REQUIRED_IN_SOURCE = ("pyproject.toml", "setup.ps1", "src/dictate/__init__.py",
                      "src/dictate/cli.py")


# ---------------------------------------------------------------------------
# Nothing that looks like a credential ever reaches the screen
# ---------------------------------------------------------------------------

#: GitHub's own token formats. `gh` has no reason to print one and does not, so
#: this exists for the day something else does.
_TOKEN_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    # A header or a setting line: everything after the colon goes, not just
    # the first word of it ("Authorization: Bearer <the actual secret>").
    re.compile(r"(?i)\b(authorization|token|password)\s*[:=].*"),
)


def redact(text: str) -> str:
    """Take anything token-shaped out of text that is about to be shown."""
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("(removed)", text)
    return text


# ---------------------------------------------------------------------------
# Which folder is the install
# ---------------------------------------------------------------------------


def find_install_root(module_file: str | None = None) -> Path:
    """The folder Python loads dictate from.

    Worked out from this file rather than from a constant: the install is
    editable, so `src/dictate/update.py` sits inside the very tree that has to be
    replaced. `C:\\dictate_v1` is where it *usually* is, and using that name
    instead of this answer is how someone updates a folder nothing loads.
    """
    here = Path(module_file or __file__).resolve()
    return here.parent.parent.parent


def check_install_root(root: Path) -> None:
    """Refuse to touch anything that is not a dictate source tree."""
    missing = [name for name in ("pyproject.toml", "src/dictate/__init__.py")
               if not (root / name).exists()]
    if missing:
        raise DictateError(
            f"dictate is loaded from {root}, which is not a copy of its source "
            f"folder (there is no {missing[0]} in it).",
            "`dictate update` replaces the source folder your install points at, "
            "so it only works on an install made by setup.ps1 (which uses\n"
            "  pip install -e <folder>\n"
            ").\n"
            "Install it that way once and updating works from then on:\n"
            "  powershell -ExecutionPolicy Bypass -File setup.ps1 -Only install",
        )
    if not os.access(root, os.W_OK):
        raise DictateError(
            f"dictate cannot write to its own install folder, {root}.",
            "Check the folder's permissions, or move the install somewhere you "
            "own and run setup again:\n"
            "  powershell -ExecutionPolicy Bypass -File setup.ps1 -Only install",
        )


def updates_dir() -> Path:
    """Where the download, the backups and the journal live: beside the lock and
    the logs, never inside the install folder and never inside his config."""
    return instance.state_dir() / "updates"


# ---------------------------------------------------------------------------
# What this copy is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stamp:
    """The record of where the files in the install folder came from.

    `files` is the whole point of keeping it: without a SHA-256 per file there is
    no way to tell a file he edited from a file the last update wrote, and
    "something might be overwritten" is not a thing worth saying to anybody.
    """

    revision: str = ""
    branch: str = DEFAULT_BRANCH
    repo: str = DEFAULT_REPO
    installed_at: str = ""
    installed_by: str = ""
    files: dict[str, str] = field(default_factory=dict)

    @property
    def short(self) -> str:
        return self.revision[:7] if self.revision else ""

    def describe(self) -> str:
        if not self.revision:
            return f"revision not recorded (installed by {self.installed_by or 'setup'})"
        when = f", recorded {self.installed_at}" if self.installed_at else ""
        return f"{self.short} on {self.branch}{when}"


def stamp_path(root: Path) -> Path:
    return root / STAMP_NAME


def parse_stamp(raw: str) -> Stamp:
    """Anything unreadable becomes an empty stamp rather than an error: not
    knowing which revision this is costs a sentence in the report, and must never
    be the reason an update refuses to run."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return Stamp()
    if not isinstance(data, dict):
        return Stamp()
    files = data.get("files")
    return Stamp(
        revision=str(data.get("revision") or ""),
        branch=str(data.get("branch") or DEFAULT_BRANCH),
        repo=str(data.get("repo") or DEFAULT_REPO),
        installed_at=str(data.get("installed_at") or ""),
        installed_by=str(data.get("installed_by") or ""),
        files={str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {},
    )


def read_stamp(root: Path) -> Stamp | None:
    try:
        raw = stamp_path(root).read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_stamp(raw)


def record_install(revision: str = "", *, root: Path | None = None,
                   branch: str = DEFAULT_BRANCH, repo: str = DEFAULT_REPO,
                   installed_by: str = "setup.ps1") -> Path:
    """Write down what this folder is, at install time. Called by `setup.ps1`.

    It is here rather than in PowerShell for one reason: the checksums have to
    be taken over exactly the files `dictate update` will compare, and two
    implementations of "which files count" would drift apart and start reporting
    edits he never made.

    `revision` is empty when setup ran on a folder that came out of a ZIP, which
    has no revision in it anywhere. That is fine and is said plainly - the
    checksums alone are already enough to tell his edits from the shipped files,
    and the first `dictate update` fills the revision in.
    """
    root = Path(root) if root else find_install_root()
    stamp = Stamp(revision=revision.strip(), branch=branch, repo=repo,
                  installed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                  installed_by=installed_by, files=tree_files(root))
    write_stamp(root, stamp)
    return stamp_path(root)


def write_stamp(root: Path, stamp: Stamp) -> None:
    payload = json.dumps({
        "revision": stamp.revision,
        "branch": stamp.branch,
        "repo": stamp.repo,
        "installed_at": stamp.installed_at,
        "installed_by": stamp.installed_by,
        "note": "Written by dictate. It records which revision of the source "
                "this folder holds, and a checksum of each file, so `dictate "
                "update` can tell what changed and what you have edited.",
        "files": dict(sorted(stamp.files.items())),
    }, indent=1)
    _write_atomically(stamp_path(root), payload.encode("utf-8"))


# ---------------------------------------------------------------------------
# Reading a tree of files
# ---------------------------------------------------------------------------


def is_ignored(relative: str) -> bool:
    """Is this path something the file plan should not look at?

    The stamp is here for a reason that is easy to get backwards: it describes
    the tree, so it is not IN the tree. If it were, every update would see the
    file it had just written as a change and the folder would never match the
    revision it holds. A BACKUP does keep it - `copy_tree` uses `IGNORED_NAMES`
    directly - because "which version was this" is part of the version.
    """
    if relative == STAMP_NAME:
        return True
    parts = relative.replace("\\", "/").split("/")
    for part in parts:
        if part in IGNORED_NAMES or part.endswith(".egg-info"):
            return True
    return parts[-1].endswith(IGNORED_SUFFIXES)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_files(root: Path) -> dict[str, str]:
    """Every file in a tree that is part of the application, and its checksum.

    Paths are relative and always use forward slashes, so a manifest written on
    one machine means the same thing on another.
    """
    found: dict[str, str] = {}
    root = Path(root)
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if is_ignored(relative):
            continue
        found[relative] = sha256_of(path)
    return found


# ---------------------------------------------------------------------------
# Deciding what to change. Pure - this is the part that must be right.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """What an update would do to the install folder."""

    write: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.write and not self.delete


def plan_apply(current: dict[str, str], incoming: dict[str, str],
               manifest: dict[str, str] | None) -> Plan:
    """Which files to write and which to remove.

    A file is written only when its content differs, so "12 files changed" is the
    truth rather than a file count.

    A file is removed only when the LAST update put it there and this one does
    not - which is what `manifest` is. With no manifest (the first update, of a
    folder that came from a ZIP) nothing is ever deleted: dictate would be
    guessing at which files were once part of the application and which are his,
    and guessing wrong means deleting something of his.
    """
    write = tuple(sorted(rel for rel, digest in incoming.items()
                         if current.get(rel) != digest))
    if not manifest:
        return Plan(write=write)
    delete = tuple(sorted(rel for rel in manifest
                          if rel not in incoming and rel in current))
    return Plan(write=write, delete=delete)


@dataclass(frozen=True)
class LocalEdits:
    """Files in the install folder that do not match what the last update wrote."""

    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        return bool(self.changed or self.removed)

    def describe(self) -> list[str]:
        lines = [f"  {rel}" for rel in self.changed]
        lines += [f"  {rel}  (deleted)" for rel in self.removed]
        return lines


def local_edits(current: dict[str, str], manifest: dict[str, str] | None) -> LocalEdits:
    """What he has changed in the install folder since the last update.

    `None` means nobody knows - there is no manifest to compare against - and the
    caller says exactly that rather than reporting "no local edits", which would
    be a claim it cannot make.
    """
    if not manifest:
        return LocalEdits()
    changed = tuple(sorted(rel for rel, digest in manifest.items()
                           if rel in current and current[rel] != digest))
    removed = tuple(sorted(rel for rel in manifest if rel not in current))
    return LocalEdits(changed=changed, removed=removed)


# ---------------------------------------------------------------------------
# What GitHub says. Pure parsing, tested against real payloads.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Revision:
    sha: str
    subject: str = ""
    date: str = ""

    @property
    def short(self) -> str:
        return self.sha[:7]


@dataclass(frozen=True)
class Changes:
    """The commit subjects between his copy and the current one."""

    subjects: tuple[str, ...] = ()
    #: True when these are exactly the changes he is missing; False when the
    #: base was not known and these are simply the most recent ones.
    exact: bool = True
    note: str = ""


def subject_of(message: str) -> str:
    """The one line of a commit message worth showing him.

    A merge commit's own subject is `Merge pull request #8 from a-branch-name`,
    which says nothing. GitHub puts the pull request's title in the paragraph
    below it, and that is the sentence he asked for.
    """
    lines = [line.strip() for line in (message or "").splitlines()]
    first = lines[0] if lines else ""
    if first.lower().startswith("merge pull request"):
        for line in lines[1:]:
            if line:
                return line
    return first


def plain_subjects(messages: list[str]) -> tuple[str, ...]:
    """Commit messages turned into a list he can read.

    Merges of main into a branch are dropped - they are bookkeeping, not
    changes - and a subject that appears twice (once on the branch, once as the
    merge's pull request title) is shown once.
    """
    seen: set[str] = set()
    out: list[str] = []
    for message in messages:
        first = (message or "").strip().splitlines()[0].strip() if (message or "").strip() else ""
        if first.lower().startswith("merge branch"):
            continue
        line = subject_of(message)
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return tuple(out)


def parse_revision(payload: str) -> Revision:
    data = json.loads(payload)
    commit = data.get("commit") or {}
    author = commit.get("committer") or commit.get("author") or {}
    return Revision(
        sha=str(data.get("sha") or ""),
        subject=subject_of(str(commit.get("message") or "")),
        date=str(author.get("date") or "")[:10],
    )


def _messages(commits) -> list[str]:
    out = []
    for entry in commits or []:
        commit = (entry or {}).get("commit") or {}
        out.append(str(commit.get("message") or ""))
    return out


def parse_comparison(payload: str) -> Changes:
    """`compare/base...head`. Its commits are oldest first; he wants newest
    first, because the thing he asked for this evening is the thing at the top."""
    data = json.loads(payload)
    subjects = plain_subjects(_messages(data.get("commits")))
    return Changes(subjects=tuple(reversed(subjects)), exact=True)


def parse_recent(payload: str) -> Changes:
    """`commits?sha=branch`, which is already newest first."""
    data = json.loads(payload)
    return Changes(
        subjects=plain_subjects(_messages(data)),
        exact=False,
        note="this copy's revision was never recorded, so these are the most "
             "recent changes rather than only the ones you are missing",
    )


# ---------------------------------------------------------------------------
# The GitHub CLI: the only thing here that knows a credential exists
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolResult:
    code: int
    out: bytes = b""
    err: bytes = b""

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", "replace")

    @property
    def said(self) -> str:
        both = (self.err.decode("utf-8", "replace") + "\n"
                + self.out.decode("utf-8", "replace")).strip()
        return redact(both)


GH_MISSING = ("dictate updates itself through the GitHub CLI, and that is not "
              "installed on this PC.")
GH_MISSING_REMEDY = (
    "Install it once, then sign in once:\n"
    "  winget install --id GitHub.cli\n"
    "  gh auth login\n"
    "Close this window and open a new one between those two commands - Windows "
    "only tells\nnew windows about a program it has just installed.\n"
    "Then run `dictate update` again."
)

SIGN_IN_REMEDY = (
    "Sign in once. It opens your browser and Windows remembers it afterwards:\n"
    "  gh auth login\n"
    "Choose GitHub.com, then HTTPS, then 'Login with a web browser', and paste "
    "the code it\nshows you. Then run `dictate update` again.\n"
    "To see what it currently thinks:  gh auth status"
)


def find_gh(*, which=shutil.which, env: dict[str, str] | None = None,
            exists=None) -> str | None:
    """Find `gh`, on PATH or where winget puts it.

    Looking in both is the same lesson the Vulkan SDK taught this installer: a
    program that was installed a minute ago is on disk long before the window you
    are typing in has been told about it, and "not on PATH" is not the same
    answer as "not installed".
    """
    found = which("gh")
    if found:
        return found
    env = os.environ if env is None else env
    exists = exists or (lambda p: Path(p).exists())
    for base in (env.get("ProgramFiles"), env.get("ProgramFiles(x86)"),
                 env.get("LOCALAPPDATA")):
        if not base:
            continue
        for tail in (r"GitHub CLI\gh.exe", r"Programs\GitHub CLI\gh.exe"):
            candidate = str(Path(base) / tail)
            if exists(candidate):
                return candidate
    return None


def run_gh(args: list[str], *, executable: str | None = None, find=find_gh,
           spawn=subprocess.run, timeout_s: float = 180.0) -> ToolResult:
    """Run the GitHub CLI and keep everything it said.

    Nothing about the credential passes through here in either direction: `gh`
    reads the sign-in Windows already holds, and dictate reads bytes back.
    """
    exe = executable or find()
    if not exe:
        raise DictateError(GH_MISSING, GH_MISSING_REMEDY)
    try:
        proc = spawn([exe, *args], capture_output=True, check=False, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise DictateError(
            f"GitHub did not answer within {timeout_s:.0f} seconds.",
            "Check you are online and run `dictate update` again. Nothing has "
            "been changed.",
        ) from exc
    except OSError as exc:
        raise DictateError(
            f"dictate could not run the GitHub CLI ({exe}): {exc}",
            GH_MISSING_REMEDY,
        ) from exc
    return ToolResult(proc.returncode, proc.stdout or b"", proc.stderr or b"")


def api_failure(path: str, result: ToolResult, repo: str) -> DictateError:
    """Turn what `gh` said into something worth reading.

    The interesting case is 404: on a private repository that is what "you are
    not signed in" looks like, because GitHub will not admit the repository
    exists to someone who cannot see it. Saying "not found" alone would send him
    looking for a deleted repository that is sitting right there.
    """
    said = result.said
    if "401" in said or "bad credentials" in said.lower():
        return DictateError(
            "GitHub refused the sign-in this PC has stored for it.\n"
            f"It said: {said}",
            "The sign-in has expired or been revoked. Do it once more:\n"
            + SIGN_IN_REMEDY,
        )
    if "404" in said or "not found" in said.lower():
        return DictateError(
            f"GitHub would not show dictate's source ({repo}).\n"
            f"It said: {said}",
            f"{repo} is a private repository, so this is what it looks like "
            "when the account\nthis PC is signed in as cannot see it - or when "
            "it is not signed in at all.\n" + SIGN_IN_REMEDY,
        )
    if "403" in said:
        return DictateError(
            f"GitHub refused access to dictate's source ({repo}).\n"
            f"It said: {said}",
            "If your organisation uses single sign-on, the stored sign-in may "
            "need authorising\nfor it. Check with:\n  gh auth status\n"
            "and then:\n  gh auth login",
        )
    return DictateError(
        f"dictate could not reach GitHub for {path}.\n"
        f"The GitHub CLI said: {said or '(nothing)'}",
        "Check you are online and run `dictate update` again. Nothing has been "
        "changed.\nIf it keeps happening:  gh auth status",
    )


class GitHub:
    """Everything dictate asks GitHub for, which is three things.

    `run` is the seam: it takes the arguments for `gh` and returns what it said,
    so every path through this class is driven in the tests without a network, a
    credential or the CLI being installed.
    """

    def __init__(self, repo: str = DEFAULT_REPO, *, run=None) -> None:
        self.repo = repo
        self._run: Callable[[list[str]], ToolResult] = run or run_gh

    def require_sign_in(self) -> None:
        """Ask before doing anything, so the first-time message arrives before a
        failure rather than as one."""
        result = self._run(["auth", "status", "--hostname", "github.com"])
        if result.code != 0:
            raise DictateError(
                "dictate needs to sign in to GitHub once before it can fetch "
                "its own source.\n"
                f"The GitHub CLI said: {result.said or '(nothing)'}",
                SIGN_IN_REMEDY,
            )

    def _api(self, path: str) -> ToolResult:
        result = self._run(["api", path])
        if result.code != 0:
            raise api_failure(path, result, self.repo)
        return result

    def head(self, branch: str) -> Revision:
        result = self._api(f"repos/{self.repo}/commits/{branch}")
        try:
            revision = parse_revision(result.text)
        except (ValueError, AttributeError, TypeError) as exc:
            raise DictateError(
                f"dictate could not make sense of what GitHub said about "
                f"{branch}: {exc}",
                "Run `dictate update` again. If it says the same thing, the "
                "safe way in is the\nold one: download the ZIP from GitHub and "
                "run setup.ps1.",
            ) from exc
        if not revision.sha:
            raise DictateError(
                f"GitHub did not say which revision {branch} is on.",
                "Run `dictate update` again in a moment. Nothing has been "
                "changed.",
            )
        return revision

    def changes(self, base: str, head: str, branch: str, *, limit: int = 15) -> Changes:
        """What is between his copy and the current one.

        A base GitHub does not recognise (a branch that was rebased away, a
        revision from a fork) is not a failure: it falls back to the recent
        history and SAYS that is what it is showing.
        """
        if base:
            result = self._run(["api", f"repos/{self.repo}/compare/{base}...{head}"])
            if result.code == 0:
                try:
                    return parse_comparison(result.text)
                except (ValueError, AttributeError, TypeError):
                    log.debug("could not parse the comparison", exc_info=True)
            else:
                log.info("comparing %s...%s did not work: %s", base, head, result.said)
        result = self._run(
            ["api", f"repos/{self.repo}/commits?sha={branch}&per_page={int(limit)}"])
        if result.code != 0:
            # Not fatal: not being able to LIST the changes is no reason to
            # refuse to make them. The update says so and carries on.
            return Changes(exact=False,
                           note="GitHub would not list what changed")
        try:
            changes = parse_recent(result.text)
        except (ValueError, AttributeError, TypeError):
            return Changes(exact=False, note="GitHub's answer could not be read")
        if base:
            return Changes(subjects=changes.subjects, exact=False,
                           note="GitHub could not compare this copy's recorded "
                                "revision with the current one, so these are the "
                                "most recent changes rather than only the ones "
                                "you are missing")
        return changes

    def source(self, sha: str) -> bytes:
        """The repository at one revision, as a .tar.gz, in memory.

        It is a few hundred kilobytes. Holding it means a download that fails
        half way leaves nothing at all on disk, rather than a truncated archive
        for the next run to trip over.
        """
        result = self._run(["api", f"repos/{self.repo}/tarball/{sha}"])
        if result.code != 0:
            raise api_failure(f"the source at {sha[:7]}", result, self.repo)
        # A gzipped source tree is hundreds of kilobytes; an error page or an
        # empty body is not. This is the "GitHub answered with nothing useful"
        # guard, not a size expectation.
        if len(result.out) < 128:
            raise DictateError(
                f"GitHub sent back {len(result.out)} bytes instead of dictate's "
                "source.",
                "Run `dictate update` again. Nothing has been changed.",
            )
        return result.out


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------


def safe_members(members: list[tarfile.TarInfo]) -> list[tarfile.TarInfo]:
    """The members of an archive that may be written to disk.

    Everything that is not an ordinary file or directory is refused, and so is
    any path that leaves the folder it is being unpacked into. This is not
    theatre: an archive is the one thing in dictate that arrives from outside and
    is written to disk by path, and `..` in a member name is how that becomes
    "write anywhere on the PC".
    """
    kept: list[tarfile.TarInfo] = []
    for member in members:
        name = member.name.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts:
            continue
        if name.startswith("/") or ".." in parts or ":" in parts[0]:
            raise DictateError(
                f"The source archive from GitHub contains an unsafe path "
                f"({member.name}), so nothing was unpacked.",
                "This should be impossible. Do not run it - report it, and "
                "install by hand from\nthe ZIP on GitHub in the meantime.",
            )
        if member.issym() or member.islnk():
            continue
        if not (member.isfile() or member.isdir()):
            continue
        kept.append(member)
    return kept


def archive_root(names: list[str]) -> str:
    """GitHub wraps a source archive in one folder named after the revision.

    Everything is under it, and it is not part of the source, so it is stripped.
    """
    tops = {name.replace("\\", "/").split("/")[0] for name in names if name.strip("/")}
    if len(tops) != 1:
        raise DictateError(
            "The source archive from GitHub is not shaped the way a source "
            f"archive is ({len(tops)} top-level folders).",
            "Run `dictate update` again. Nothing has been changed.",
        )
    return tops.pop()


def extract_source(data: bytes, into: Path) -> Path:
    """Unpack a GitHub source archive and return the folder the source is in."""
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = safe_members(archive.getmembers())
            top = archive_root([m.name for m in members])
            # `data` is the standard filter: no device files, no absolute paths,
            # no permissions carried over. safe_members has already refused
            # anything it would refuse, and this is the belt to that pair of
            # braces.
            extra = {"filter": "data"} if sys.version_info >= (3, 12) else {}
            archive.extractall(into, members=members, **extra)
    except tarfile.TarError as exc:
        raise DictateError(
            f"The source dictate downloaded could not be unpacked: {exc}",
            "Run `dictate update` again - a download can be cut short. Nothing "
            "has been changed.",
        ) from exc
    except OSError as exc:
        # A full disk, most likely. This is still outside the install folder, so
        # "nothing has been changed" is the literal truth rather than a hope.
        raise DictateError(
            f"dictate could not write the new version to {into}: {exc}",
            "Nothing has been changed. Free some space on that drive and run "
            "`dictate update`\nagain.",
        ) from exc
    return into / top


def check_source_tree(root: Path) -> None:
    """Is what came back actually dictate? Checked before anything is copied out
    of it, because "it unpacked" and "it is the program" are different facts."""
    missing = [name for name in REQUIRED_IN_SOURCE if not (root / name).exists()]
    if missing:
        raise DictateError(
            "What GitHub sent back does not look like dictate's source: "
            f"{', '.join(missing)} {'are' if len(missing) > 1 else 'is'} not in it.",
            "Nothing has been changed. Run `dictate update` again, and if it "
            "says the same\nthing, install from the ZIP on GitHub by hand.",
        )
    text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
    if 'name = "dictate"' not in text:
        raise DictateError(
            "What GitHub sent back is some other project, not dictate.",
            "Nothing has been changed. Check the repository name and run:\n"
            "  dictate update",
        )


# ---------------------------------------------------------------------------
# Writing files, one at a time, never half of one
# ---------------------------------------------------------------------------


def _write_atomically(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + TEMP_SUFFIX)
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def copy_file_atomically(source: Path, target: Path) -> None:
    """Put `source` at `target` without `target` ever being a partial file.

    Written beside it and renamed over it: a rename within one folder is the one
    file operation Windows will not leave half-done, so an interruption leaves
    either the old file or the new one and never something in between.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + TEMP_SUFFIX)
    shutil.copyfile(source, temp)
    os.replace(temp, target)


def _prune_empty_parents(path: Path, stop: Path) -> None:
    parent = path.parent
    while parent != stop and stop in parent.parents:
        try:
            next(parent.iterdir())
        except StopIteration:
            try:
                parent.rmdir()
            except OSError:
                return
            parent = parent.parent
            continue
        except OSError:
            return
        return


def apply_plan(source: Path, target: Path, plan: Plan) -> None:
    """Carry out a plan. Every write is atomic; nothing else is touched."""
    for relative in plan.write:
        copy_file_atomically(source / relative, target / relative)
    for relative in plan.delete:
        path = target / relative
        try:
            path.unlink()
        except OSError:
            log.info("could not remove %s", path, exc_info=True)
            continue
        _prune_empty_parents(path, target)


def copy_tree(source: Path, target: Path) -> None:
    """A complete copy of an install folder, without its build leavings."""
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skip = set()
        for name in names:
            if name in IGNORED_NAMES or name.endswith(IGNORED_SUFFIXES) \
                    or name.endswith(".egg-info"):
                skip.add(name)
        return skip

    shutil.copytree(source, target, ignore=ignore, symlinks=False)


def restore_tree(backup: Path, target: Path) -> Plan:
    """Put a backup back, exactly - including removing files that were not in it.

    Returns what it did, so the report is the truth rather than "restored".
    """
    kept = tree_files(backup)
    current = tree_files(target)
    plan = Plan(
        write=tuple(sorted(rel for rel, digest in kept.items()
                           if current.get(rel) != digest)),
        delete=tuple(sorted(rel for rel in current if rel not in kept)),
    )
    apply_plan(backup, target, plan)
    _restore_stamp(backup, target)
    return plan


def _restore_stamp(backup: Path, target: Path) -> None:
    """Put back which revision the restored files are.

    Missed once, and it is the quiet kind of wrong: the files would go back and
    the record would still name the version that had just been taken away, so
    `dictate update` would answer "already up to date" for a copy it had just
    rolled back.
    """
    source, destination = stamp_path(backup), stamp_path(target)
    if source.exists():
        copy_file_atomically(source, destination)
        return
    try:
        destination.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The journal: what makes "interrupted" different from "broken"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Journal:
    root: str = ""
    backup: str = ""
    from_revision: str = ""
    to_revision: str = ""
    started: str = ""

    def describe(self) -> str:
        where = f" of {self.root}" if self.root else ""
        when = f" started {self.started}" if self.started else ""
        return f"an update{where}{when}"


def journal_path(updates: Path | None = None) -> Path:
    return (updates or updates_dir()) / JOURNAL_NAME


def write_journal(journal: Journal, updates: Path | None = None) -> None:
    path = journal_path(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(path, json.dumps({
        "root": journal.root,
        "backup": journal.backup,
        "from_revision": journal.from_revision,
        "to_revision": journal.to_revision,
        "started": journal.started,
        "note": "An update was in progress when this was written. If it is still "
                "here, that update did not finish - run `dictate update` and it "
                "will put the backup above back.",
    }, indent=1).encode("utf-8"))


def read_journal(updates: Path | None = None) -> Journal | None:
    try:
        raw = journal_path(updates).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        # An unreadable journal still means an update did not finish, which is
        # the only thing its existence is for.
        return Journal()
    if not isinstance(data, dict):
        return Journal()
    return Journal(
        root=str(data.get("root") or ""),
        backup=str(data.get("backup") or ""),
        from_revision=str(data.get("from_revision") or ""),
        to_revision=str(data.get("to_revision") or ""),
        started=str(data.get("started") or ""),
    )


def clear_journal(updates: Path | None = None) -> None:
    try:
        journal_path(updates).unlink()
    except OSError:
        pass


def backups(updates: Path) -> list[Path]:
    """Every kept version, newest first. They are named by the time they were
    taken, so sorting the names sorts them by age."""
    try:
        found = [p for p in updates.iterdir() if p.is_dir() and p.name.startswith("backup-")]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name, reverse=True)


def prune_backups(updates: Path, keep: int = KEEP_BACKUPS) -> None:
    for old in backups(updates)[keep:]:
        try:
            shutil.rmtree(old)
        except OSError:
            log.info("could not remove the old backup %s", old, exc_info=True)


# ---------------------------------------------------------------------------
# Proving the new code works
# ---------------------------------------------------------------------------

#: Importing the command line pulls in the whole application: `cli` imports the
#: app - and so the pipeline, both engines and the platform seam - as well as
#: autostart, doctor, recovery and this module. A syntax error or a renamed
#: module anywhere in that graph fails here.
#:
#: It deliberately names nothing that only the newest version has. A check that
#: asked for a module added last week would call every older version broken, and
#: an older version is exactly what a roll-back puts back.
IMPORT_CHECK = ("import dictate, dictate.cli; "
                "print(dictate.__file__); print(dictate.__version__)")


@dataclass
class Health:
    imports: bool = False
    loaded_from: str = ""
    version: str = ""
    said: str = ""
    doctor_ok: bool | None = None


def verify_install(root: Path, *, python: str | None = None,
                   config_path: str | None = None,
                   spawn=subprocess.run, run_doctor: bool = True) -> Health:
    """Make the code that was just written prove itself, in a fresh interpreter.

    A fresh one matters: this process imported dictate minutes ago and would go
    on running the old code however badly the new code is broken. The subprocess
    is the only witness that can tell the difference, and "an update that quietly
    installed nothing" is exactly what it is here to catch.
    """
    exe = python or sys.executable
    health = Health()
    try:
        proc = spawn([exe, "-c", IMPORT_CHECK], capture_output=True, text=True,
                     errors="replace", check=False, timeout=120.0, cwd=str(root.parent))
    except (OSError, subprocess.SubprocessError) as exc:
        health.said = str(exc)
        return health
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    health.said = output
    if proc.returncode != 0:
        return health
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    health.imports = True
    if lines:
        health.loaded_from = lines[0]
        health.version = lines[-1]
    if not run_doctor:
        return health

    argv = [exe, "-m", "dictate"]
    if config_path:
        argv += ["--config", str(config_path)]
    argv += ["doctor"]
    try:
        check = spawn(argv, capture_output=True, text=True, errors="replace",
                      check=False, timeout=180.0, cwd=str(root.parent))
    except (OSError, subprocess.SubprocessError):
        log.debug("could not run doctor after the update", exc_info=True)
        return health
    health.doctor_ok = check.returncode == 0
    return health


def loaded_from_matches(loaded_from: str, root: Path) -> bool:
    """Did the fresh interpreter load dictate out of the folder we just wrote?

    The failure this exists for: updating one folder while Python imports
    another. Everything would report success and nothing would have changed.
    """
    if not loaded_from:
        return False
    try:
        return Path(loaded_from).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


Say = Callable[[str], None]


@dataclass
class Outcome:
    ok: bool = True
    up_to_date: bool = False
    checked_only: bool = False
    from_revision: str = ""
    to_revision: str = ""
    written: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    backup: str = ""
    restarted_pid: int | None = None
    reinstalled: bool = False


def _stamp_for(root: Path, revision: Revision, branch: str, repo: str) -> Stamp:
    return Stamp(
        revision=revision.sha,
        branch=branch,
        repo=repo,
        installed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        installed_by="dictate update",
        files=tree_files(root),
    )


def _recover_interrupted(updates: Path, root: Path, say: Say) -> bool:
    """An update that did not finish. Put the backup back before anything else.

    This is what the journal is for. The backup is a complete copy of a working
    install taken before the first file was touched, so putting it back is a
    file copy with no decisions in it.
    """
    journal = read_journal(updates)
    if journal is None:
        return False
    say("")
    say(f"An earlier update did not finish ({journal.describe()}).")
    backup = Path(journal.backup) if journal.backup else None
    if backup is None or not backup.exists():
        clear_journal(updates)
        say("dictate has no copy of what was there before it, so it cannot put "
            "it back. The")
        say("update below replaces every file anyway, which is the way out of "
            "this.")
        return False
    if journal.root and Path(journal.root) != root:
        say(f"It was an update of {journal.root}, which is not the folder this "
            f"dictate loads from,")
        say("so it has been left alone.")
        return False
    plan = restore_tree(backup, root)
    clear_journal(updates)
    say(f"The version that was there before it has been put back "
        f"({len(plan.write)} file(s) restored, {len(plan.delete)} removed).")
    say("")
    return True


def _stop_running_copy(say: Say, timeout_s: float) -> instance.Holder | None:
    """Stop the copy that is running, or refuse - never force.

    `dictate stop` escalates to ending a copy that will not answer, and it is
    right to: he asked for it to be gone. An update has not been asked for that.
    A copy that will not stop cleanly is one that is busy, and taking it away
    mid-sentence to install something is the opposite of what this command is
    for. So this asks, waits, and gives up with nothing changed.
    """
    holder = instance.running_instance()
    if holder is None:
        return None

    activity = instance.read_activity()
    if activity is not None and activity.busy and activity.belongs_to(holder):
        raise DictateError(
            f"dictate is in the middle of {activity.what or 'something'}, so it "
            "has been left alone and nothing has been changed.",
            "Wait until it has finished and run `dictate update` again - it "
            "takes seconds.",
        )

    say(f"dictate is running ({holder.describe()}); asking it to stop…")
    instance.request_stop()
    if not instance.wait_until_stopped(timeout_s):
        instance.clear_stop_request()
        raise DictateError(
            f"dictate did not stop within {timeout_s:.0f} seconds of being "
            "asked, so nothing has been changed.",
            "It is probably busy. Try once more in a moment:\n"
            "  dictate update\n"
            "If it will not stop at all, stop it yourself and then update:\n"
            "  dictate stop\n"
            "  dictate update",
        )
    say("dictate has stopped, and whisper-server with it.")
    return holder


def _restart(holder: instance.Holder, config_path: str | None, say: Say) -> int | None:
    """Start dictate again the way it was started before.

    A copy that came up at logon has no console: it must come back under
    `pythonw.exe` with `--autostart`, or its first `print` would fall over in a
    window that does not exist. That is the same reasoning as
    `recovery.relaunch_argv`, and this is the one caller that has to choose.
    """
    at_logon = holder.started_by == "logon"
    executable: str | None = None
    if at_logon:
        from .autostart import windowless_python  # noqa: PLC0415

        try:
            executable = str(windowless_python())
        except DictateError as exc:
            say("")
            say("dictate is not running again yet: " + exc.message)
            say("It will start by itself the next time you log in, or type "
                "`dictate run`.")
            return None
    try:
        pid = recovery.relaunch(config_path, executable=executable,
                                autostart=at_logon)
    except DictateError as exc:
        say("")
        say(exc.report())
        return None
    say(f"dictate has been started again on the new version (process {pid}).")
    return pid


def _go_back(backup: Path, root: Path, updates: Path, say: Say) -> Plan | None:
    """Put the backup back, and never raise while doing it.

    Every caller is already on a failure path. Something going wrong *here* must
    not replace the message he needs with a traceback, so what it could not do is
    said rather than thrown - and the journal is left in place when the restore
    itself failed, so the next `dictate update` tries again.
    """
    try:
        plan = restore_tree(backup, root)
    except OSError as exc:
        say(f"  (putting it back did not work either: {exc})")
        say(f"  A complete copy of your install is at {backup}.")
        return None
    clear_journal(updates)
    return plan


def _report_changes(changes: Changes, say: Say) -> None:
    if changes.note:
        say(f"({changes.note})")
    if not changes.subjects:
        say("  (GitHub listed no commit subjects for this change)")
        return
    for line in changes.subjects:
        say(f"  - {line}")


def update(*, say: Say, root: Path | None = None, repo: str = DEFAULT_REPO,
           branch: str = DEFAULT_BRANCH, config_path: str | None = None,
           check_only: bool = False, force: bool = False,
           timeout_s: float = 30.0, github: GitHub | None = None,
           python: str | None = None, restart: bool = True,
           spawn=subprocess.run) -> Outcome:
    """The whole of `dictate update`, in the order that risks the least.

    Read the module docstring for why the order is what it is. In one line:
    everything that can fail happens outside the install folder, and the install
    folder is not touched at all until there is a complete, checked copy of the
    new version on disk and a complete copy of the old one beside it.
    """
    outcome = Outcome()
    root = Path(root) if root else find_install_root()
    check_install_root(root)
    updates = updates_dir()
    updates.mkdir(parents=True, exist_ok=True)

    say(f"install folder:  {root}")
    say("                 (this is the folder Python loads dictate from)")

    _recover_interrupted(updates, root, say)

    stamp = read_stamp(root) or Stamp(installed_by="setup.ps1")
    say(f"this copy:       {stamp.describe()}")
    outcome.from_revision = stamp.revision

    source = github or GitHub(repo)
    source.require_sign_in()
    head = source.head(branch)
    outcome.to_revision = head.sha
    say(f"latest on {branch}:   {head.short}"
        + (f"  {head.subject}" if head.subject else ""))
    say("")

    if stamp.revision and stamp.revision == head.sha:
        say("dictate is already up to date. Nothing has been changed.")
        outcome.up_to_date = True
        return outcome

    changes = source.changes(stamp.revision, head.sha, branch)
    say("What changed:")
    _report_changes(changes, say)
    say("")

    # -- what is in the folder now ------------------------------------------
    current = tree_files(root)
    edits = local_edits(current, stamp.files)
    if not stamp.files:
        say("dictate did not install this copy itself, so it cannot tell which "
            "files you have")
        say("edited. A complete copy of the folder is kept before anything is "
            "changed.")
    elif edits.any:
        if not force:
            raise DictateError(
                "You have changed files in the install folder since the last "
                "update, and updating would overwrite them:\n"
                + "\n".join(edits.describe()),
                "Copy anything you want to keep out of that folder first, then:\n"
                "  dictate update --force\n"
                "That keeps a complete copy of the folder as it is now, and "
                "says where.",
            )
        say("You have changed these files, and --force was given, so they will "
            "be overwritten:")
        for line in edits.describe():
            say(line)
        say("A complete copy of the folder as it is now is kept - the path is "
            "below.")
        say("")

    # -- fetch, unpack and check, all outside the install folder -------------
    say("Fetching the new version…")
    staging = updates / f"source-{head.short}"
    incoming_root = extract_source(source.source(head.sha), staging)
    check_source_tree(incoming_root)
    incoming = tree_files(incoming_root)

    plan = plan_apply(current, incoming, stamp.files or None)
    outcome.written, outcome.deleted = plan.write, plan.delete

    if plan.empty:
        # His files already ARE the new version - the usual reason is that the
        # ZIP he installed from was current. Nothing needs stopping or writing;
        # the revision is recorded so that from now on this is answerable
        # without downloading anything.
        shutil.rmtree(staging, ignore_errors=True)
        outcome.up_to_date = True
        say(f"Every file in {root} already matches {head.short}, so nothing "
            "needs changing.")
        if check_only:
            outcome.checked_only = True
            say("Running `dictate update` would record that, so the answer comes "
                "back in a second")
            say("from then on without downloading anything.")
            return outcome
        write_stamp(root, _stamp_for(root, head, branch, repo))
        say("Its revision has been recorded, so `dictate update` can answer this "
            "in a second from now on.")
        return outcome

    say(f"{len(plan.write)} file(s) to update"
        + (f", {len(plan.delete)} to remove" if plan.delete else "") + ".")
    if check_only:
        say("")
        say("--check was given, so nothing has been changed. To do it:")
        say("  dictate update")
        shutil.rmtree(staging, ignore_errors=True)
        outcome.checked_only = True
        return outcome
    say("")

    # -- from here on the install folder is touched -------------------------
    holder = _stop_running_copy(say, timeout_s) if restart else None

    backup = updates / f"backup-{time.strftime('%Y%m%d-%H%M%S')}-{stamp.short or 'unknown'}"
    try:
        copy_tree(root, backup)
    except OSError as exc:
        # Not one file has been touched at this point, and none will be: the
        # backup is the thing that makes the rest of this safe.
        raise DictateError(
            f"dictate could not take a copy of your install before changing it "
            f"({exc}).",
            "Nothing has been changed. The usual causes are no space left on "
            "the disk, or a\nvirus scanner holding a file open. Free some "
            "space, or try again in a moment:\n"
            "  dictate update",
        ) from exc
    outcome.backup = str(backup)
    say(f"The version you have now is kept at:\n  {backup}")
    write_journal(Journal(
        root=str(root), backup=str(backup), from_revision=stamp.revision,
        to_revision=head.sha, started=time.strftime("%Y-%m-%d %H:%M:%S"),
    ), updates)

    # Replacing this very process's own source is safe and deliberate: every
    # module it will still need is already in `sys.modules` (`cli` imports the
    # application, autostart, recovery and this module before anything runs), so
    # the rest of this update runs the code it started with. The only thing that
    # deliberately loads the NEW code is the fresh interpreter below.
    try:
        apply_plan(incoming_root, root, plan)
    except OSError as exc:
        # A file that would not be written - held open by a virus scanner is the
        # realistic one. The install is now part old and part new, which is the
        # state nobody may be left in, so it goes straight back.
        say(f"A file could not be written ({exc}), so the version you had is "
            "being put back.")
        _go_back(backup, root, updates, say)
        raise DictateError(
            f"dictate could not write the new version into {root}: {exc}",
            "The version you had before has been put back. Close anything that "
            "might be holding\na file in that folder open - an editor, a virus "
            "scanner - and try again:\n"
            "  dictate update",
        ) from exc
    say(f"{len(plan.write)} file(s) written"
        + (f", {len(plan.delete)} removed" if plan.delete else "") + ".")

    # -- the dependency list, and only if it changed ------------------------
    if "pyproject.toml" in plan.write:
        say("")
        say("The list of Python packages dictate needs has changed, so pip is "
            "being run once.")
        outcome.reinstalled = _reinstall(root, python=python, say=say, spawn=spawn)

    # -- prove it works, and go back if it does not -------------------------
    say("")
    health = verify_install(root, python=python, config_path=config_path,
                            spawn=spawn)
    if not health.imports or not loaded_from_matches(health.loaded_from, root):
        say("The new version does not load, so it is being put back:")
        say(_indent(redact(health.said) or "(it said nothing)"))
        restored = _go_back(backup, root, updates, say)
        again = verify_install(root, python=python, run_doctor=False, spawn=spawn)
        outcome.ok = False
        if again.imports:
            raise DictateError(
                "The new version of dictate would not load, so the version you "
                f"had before has been put back "
                f"({len(restored.write) if restored else 0} file(s)) "
                "and it works. Nothing was lost.",
                "Nothing you can do about this here - it is a fault in the new "
                "version. Report it,\nand carry on: dictate is working.\n"
                "  dictate doctor",
            )
        raise DictateError(
            "The new version of dictate would not load, and putting the "
            f"previous one back did not fix it.\nIt said:\n{_indent(redact(again.said))}",
            f"A complete copy of your install as it was is at:\n  {backup}\n"
            "Copy the contents of that folder over "
            f"{root} and then run:\n"
            "  dictate doctor",
        )

    write_stamp(root, _stamp_for(root, head, branch, repo))
    clear_journal(updates)
    shutil.rmtree(staging, ignore_errors=True)
    prune_backups(updates)

    say(f"dictate is now at {head.short} and loads from {health.loaded_from}.")
    if health.doctor_ok is False:
        say("")
        say("`dictate doctor` reports a problem. It may have been there before "
            "this update -")
        say("run it to see what it is:")
        say("  dictate doctor")
    elif health.doctor_ok:
        say("Everything `dictate doctor` checks is still in place.")

    if holder is not None:
        outcome.restarted_pid = _restart(holder, config_path, say)
    elif restart:
        say("dictate was not running, so it has not been started.")
    return outcome


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in (text or "").splitlines())


def _reinstall(root: Path, *, python: str | None, say: Say,
               spawn=subprocess.run) -> bool:
    """Run pip once, and only because `pyproject.toml` changed.

    Not on every update: an editable install is a pointer at this folder, so
    replacing the files under it IS the update. pip only has to be involved when
    the list of packages, or the `dictate` command itself, has changed.
    """
    exe = python or sys.executable
    target = f"{root}[windows]" if sys.platform == "win32" else str(root)
    try:
        proc = spawn([exe, "-m", "pip", "install", "-e", target],
                     capture_output=True, text=True, errors="replace",
                     check=False, timeout=900.0)
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"  pip could not be run ({exc}).")
        say("  If dictate misbehaves, run this once:")
        say(f"    \"{exe}\" -m pip install -e \"{target}\"")
        return False
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-8:]
        say("  pip did not finish. It said:")
        say(_indent("\n".join(tail), "    "))
        say("  The new code is installed; only the package list may be behind. "
            "Run this once:")
        say(f"    \"{exe}\" -m pip install -e \"{target}\"")
        return False
    say("  pip finished.")
    return True


# ---------------------------------------------------------------------------
# Going back on purpose
# ---------------------------------------------------------------------------


def restore(*, say: Say, root: Path | None = None, timeout_s: float = 30.0,
            config_path: str | None = None, restart: bool = True,
            python: str | None = None, spawn=subprocess.run) -> Outcome:
    """`dictate update --restore`: put the previous version back.

    The same copy an interrupted update would have used, chosen deliberately
    rather than after a failure.
    """
    outcome = Outcome()
    root = Path(root) if root else find_install_root()
    check_install_root(root)
    updates = updates_dir()

    say(f"install folder:  {root}")
    if _recover_interrupted(updates, root, say):
        say("Nothing else to do - that WAS the previous version.")
        return outcome

    kept = backups(updates)
    if not kept:
        raise DictateError(
            "dictate has no copy of a previous version to put back.",
            "A copy is kept every time `dictate update` changes anything, so "
            "there will be one\nafter the next update. To reinstall from "
            "scratch:\n"
            "  powershell -ExecutionPolicy Bypass -File setup.ps1 -Only install",
        )
    backup = kept[0]
    say(f"putting back:    {backup}")
    say("")

    holder = _stop_running_copy(say, timeout_s) if restart else None
    # The journal goes down first here too: a restore that stops half way is the
    # same half-replaced folder an update would leave, and the way out of it is
    # the same one.
    write_journal(Journal(root=str(root), backup=str(backup),
                          started=time.strftime("%Y-%m-%d %H:%M:%S")), updates)
    plan = _go_back(backup, root, updates, say)
    if plan is None:
        outcome.ok = False
        raise DictateError(
            f"dictate could not put {backup} back over {root}.",
            "Close anything that might be holding a file in that folder open - "
            "an editor, a\nvirus scanner - and run `dictate update --restore` "
            "again. Nothing is lost: the\ncomplete copy is still in the folder "
            "named above.",
        )
    outcome.written, outcome.deleted = plan.write, plan.delete

    stamp = read_stamp(root)
    outcome.to_revision = stamp.revision if stamp else ""
    say(f"{len(plan.write)} file(s) restored"
        + (f", {len(plan.delete)} removed" if plan.delete else "") + ".")

    health = verify_install(root, python=python, config_path=config_path,
                            run_doctor=False, spawn=spawn)
    if not health.imports:
        outcome.ok = False
        raise DictateError(
            f"The version that was put back does not load. It said:\n"
            f"{_indent(redact(health.said))}",
            f"The copy it came from is still at:\n  {backup}\n"
            "Reinstall from GitHub:\n"
            "  powershell -ExecutionPolicy Bypass -File setup.ps1 -Only install",
        )
    say(f"dictate is back at {stamp.describe() if stamp else 'the previous version'}.")
    if holder is not None:
        outcome.restarted_pid = _restart(holder, config_path, say)
    return outcome
