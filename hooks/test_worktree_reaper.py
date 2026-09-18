#!/usr/bin/env python3
"""Fixture coverage for worktree-reaper.py: closeout, reap accounting, nested
artifact paths, reap-now, and the stdin bound."""

from __future__ import annotations

import faulthandler
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("worktree-reaper.py")

# Every child spawned here gets its own timeout, so a hang fails as a test
# failure rather than a stalled run. This is the backstop for the ones nobody
# has written yet: a hang anywhere in this module dumps every thread's stack and
# exits, instead of parking a `unittest discover` run indefinitely with no
# output. Scoped to this module via setUpModule/tearDownModule so it cannot cut
# short a sibling module's tests in the same discover run. Generous — this
# module runs in seconds; the number only has to beat a human's patience.
MODULE_TIMEOUT_SECONDS = 180
# Long enough that a healthy child is never cut off, short enough that a
# regression is a fast red instead of a coffee break.
CHILD_TIMEOUT_SECONDS = 60


def setUpModule() -> None:
    faulthandler.dump_traceback_later(MODULE_TIMEOUT_SECONDS, exit=True)


def tearDownModule() -> None:
    faulthandler.cancel_dump_traceback_later()


class _RepoFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.log = Path(self.temporary.name) / "closeout.log"
        self.git("init", "--initial-branch=main", str(self.root))
        self.git("config", "user.name", "Worktree Test", cwd=self.root)
        self.git("config", "user.email", "worktree@example.invalid", cwd=self.root)
        (self.root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=self.root)
        self.git("commit", "-m", "base", cwd=self.root)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def add_worktree(self, name: str) -> Path:
        worktree = self.root / ".worktrees" / name
        worktree.parent.mkdir()
        self.git("worktree", "add", "-b", name, str(worktree), "main", cwd=self.root)
        return worktree

    def closeout(self, worktree: Path) -> None:
        environment = os.environ.copy()
        environment["WORKTREE_CLOSEOUT_LOG"] = str(self.log)
        subprocess.run(
            [sys.executable, str(SCRIPT), "closeout"],
            cwd=self.root,
            input=json.dumps({"cwd": str(worktree)}),
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=CHILD_TIMEOUT_SECONDS,
        )

    def ignore(self, pattern: str) -> None:
        """Commit a .gitignore line on main; call BEFORE add_worktree so the
        branch inherits it."""
        gitignore = self.root / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        gitignore.write_text(existing + pattern + "\n", encoding="utf-8")
        self.git("add", ".gitignore", cwd=self.root)
        self.git("commit", "-m", f"ignore {pattern}", cwd=self.root)

    def reap(self, mode: str = "reap", idle_hours: str = "0") -> str:
        """Run `reap` (or `reap-now`) against the fixture repo; return the log."""
        environment = os.environ.copy()
        environment["WORKTREE_REAPER_LOG"] = str(self.log)
        environment["WORKTREE_REAPER_ARTIFACT_IDLE_HOURS"] = idle_hours
        subprocess.run(
            [sys.executable, str(SCRIPT), mode, str(self.root)],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            # Not decoration. Without stdin= this child inherits the RUNNER's
            # stdin, and the reaper reads its payload to EOF — so the test hung
            # or passed purely on whether that stdin happened to be closed. The
            # closeout helper above never hung only because `input=` closes the
            # pipe for it. See StdinPayloadTests.
            stdin=subprocess.DEVNULL,
            timeout=CHILD_TIMEOUT_SECONDS,
        )
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


class WorktreeCloseoutTests(_RepoFixture):
    def test_main_checkout_is_never_removed(self) -> None:
        self.closeout(self.root)

        self.assertTrue(self.root.exists())
        self.assertIn("state=main action=keep", self.log.read_text(encoding="utf-8"))

    def test_merged_clean_linked_worktree_is_removed_without_deleting_branch(self) -> None:
        worktree = self.add_worktree("task")
        (worktree / "task.txt").write_text("done\n", encoding="utf-8")
        self.git("add", "task.txt", cwd=worktree)
        self.git("commit", "-m", "task", cwd=worktree)
        self.git("merge", "--no-ff", "task", "-m", "merge task", cwd=self.root)
        task_head = self.git("rev-parse", "task", cwd=self.root)

        self.closeout(worktree)

        self.assertFalse(worktree.exists())
        self.assertEqual(self.git("rev-parse", "--verify", "task", cwd=self.root), task_head)
        self.assertIn("state=merged-clean action=removed", self.log.read_text(encoding="utf-8"))

    def test_dirty_linked_worktree_is_preserved_for_handoff(self) -> None:
        worktree = self.add_worktree("dirty-task")
        (worktree / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        self.closeout(worktree)

        self.assertTrue(worktree.exists())
        self.assertIn("branch=dirty-task state=dirty action=handoff", self.log.read_text(encoding="utf-8"))

    def test_clean_unmerged_linked_worktree_is_preserved_for_pr_or_hold(self) -> None:
        worktree = self.add_worktree("unmerged-task")
        (worktree / "task.txt").write_text("needs review\n", encoding="utf-8")
        self.git("add", "task.txt", cwd=worktree)
        self.git("commit", "-m", "unmerged task", cwd=worktree)

        self.closeout(worktree)

        self.assertTrue(worktree.exists())
        self.assertIn(
            "branch=unmerged-task default=main state=unmerged-clean action=open-pr-or-hold",
            self.log.read_text(encoding="utf-8"),
        )



class ReapAccountingTests(_RepoFixture):
    """The log is the only record of what this hook did, so its numbers must hold."""

    def test_freed_reports_the_deleted_tree_not_a_volume_delta(self) -> None:
        worktree = self.add_worktree("stale-build")
        (self.root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        self.git("add", ".gitignore", cwd=self.root)
        self.git("commit", "-m", "ignore build output", cwd=self.root)
        self.git("merge", "main", cwd=worktree)

        build = worktree / "node_modules"
        build.mkdir()
        # ~4 MB, large enough that a wrong number is unambiguous.
        (build / "blob.bin").write_bytes(b"x" * 4_000_000)
        old = 1_600_000_000  # well past any idle gate
        os.utime(build / "blob.bin", (old, old))
        os.utime(build, (old, old))

        line = self.reap()

        self.assertFalse(build.exists(), "the stale build dir should be gone")
        self.assertIn("artifacts=1", line)
        # Volume free space is contaminated by any concurrent writer; the size of
        # the tree that was deleted is not.
        freed = int(line.split("freed")[1].lstrip("=>").split("MB")[0])
        self.assertGreaterEqual(freed, 3)
        self.assertLessEqual(freed, 8)


OLD = 1_600_000_000  # 2020 — well past any idle gate


def backdate(path: Path) -> None:
    """Set every file and directory under `path` (inclusive) to OLD."""
    for entry in sorted(path.rglob("*"), key=lambda e: len(e.parts), reverse=True):
        os.utime(entry, (OLD, OLD), follow_symlinks=False)
    os.utime(path, (OLD, OLD))


class NestedArtifactTests(_RepoFixture):
    """ARTIFACT_DIRS entries are worktree-relative paths, matched at exactly that
    location. Measured 2026-09-07: minder redirects Xcode DerivedData into
    `.artifacts/derived-data` under each worktree — 127 GB across 67 worktrees,
    98 GB of it idle past the gate — and `report` said "artifact dirs to delete
    (0)" because the tuple only knew root-level names."""

    def derived_data_worktree(self, ignored: bool = True) -> tuple[Path, Path, Path]:
        if ignored:
            self.ignore(".artifacts/")
        worktree = self.add_worktree("xcode-task")
        derived = worktree / ".artifacts" / "derived-data" / "Build" / "Intermediates.noindex"
        derived.mkdir(parents=True)
        (derived / "Module.pcm").write_bytes(b"x" * 100_000)
        sibling = worktree / ".artifacts" / "captures"
        sibling.mkdir()
        (sibling / "screen.png").write_bytes(b"png")
        backdate(worktree / ".artifacts")
        return worktree, worktree / ".artifacts" / "derived-data", sibling

    def test_idle_ignored_nested_derived_data_is_reaped_and_sibling_survives(self) -> None:
        worktree, derived, sibling = self.derived_data_worktree()

        line = self.reap(idle_hours="48")

        self.assertFalse(derived.exists(), "idle .artifacts/derived-data should be gone")
        self.assertTrue((sibling / "screen.png").exists(), "the parent's other children are not ours")
        self.assertTrue((worktree / "tracked.txt").exists())
        self.assertIn("artifacts=1", line)

    def test_nested_tree_with_a_fresh_deep_file_is_kept(self) -> None:
        """The idle gate is deep. A build writing into Intermediates.noindex leaves
        derived-data's own mtime untouched, so a root-mtime check would delete a
        tree mid-build; the deep find must see the fresh file and skip."""
        worktree, derived, _ = self.derived_data_worktree()
        (derived / "Build" / "Intermediates.noindex" / "fresh.o").write_bytes(b"o")
        # The write refreshed the leaf dir; put the dir mtimes back so only the
        # file itself is what the scan can find.
        os.utime(derived / "Build" / "Intermediates.noindex", (OLD, OLD))

        line = self.reap(idle_hours="48")

        self.assertTrue((derived / "Build" / "Intermediates.noindex" / "fresh.o").exists())
        # A run that finds no work stamps and logs nothing.
        self.assertNotIn("artifacts=", line)

    def test_nested_tree_that_git_does_not_ignore_is_kept(self) -> None:
        worktree, derived, _ = self.derived_data_worktree(ignored=False)

        line = self.reap(idle_hours="48")

        self.assertTrue(derived.exists(), "an un-ignored dir is never build output to us")
        self.assertNotIn("artifacts=", line)

    def test_nested_tree_in_a_worktree_with_a_live_session_is_kept(self) -> None:
        worktree, derived, _ = self.derived_data_worktree()
        common = Path(self.git("rev-parse", "--git-common-dir", cwd=self.root))
        if not common.is_absolute():
            common = self.root / common
        locks = common / ".claude-sessions"
        locks.mkdir(parents=True, exist_ok=True)
        (locks / "live.json").write_text(json.dumps({"cwd": str(worktree)}), encoding="utf-8")

        line = self.reap(mode="reap-now", idle_hours="48")

        self.assertTrue(derived.exists(), "a worktree with a fresh session lock is occupied")
        self.assertNotIn("artifacts=", line)


class ReapNowTests(_RepoFixture):
    """`reap-now` is the hand-run reap: no throttle, no deadline, same gates."""

    def stale_build(self) -> Path:
        self.ignore("node_modules/")
        worktree = self.add_worktree("stale")
        build = worktree / "node_modules"
        build.mkdir()
        (build / "blob.bin").write_bytes(b"x" * 10_000)
        backdate(build)
        return build

    def stamp_recent_reap(self) -> None:
        common = Path(self.git("rev-parse", "--git-common-dir", cwd=self.root))
        if not common.is_absolute():
            common = self.root / common
        (common / ".claude-sessions").mkdir(parents=True, exist_ok=True)
        (common / ".claude-sessions" / ".last-reap").write_text("now", encoding="utf-8")

    def test_hook_reap_honours_the_throttle_and_reap_now_does_not(self) -> None:
        build = self.stale_build()
        self.stamp_recent_reap()

        throttled_log = self.reap(mode="reap")
        self.assertTrue(build.exists(), "a throttled hook run must not delete")
        self.assertEqual(throttled_log, "", "a throttled hook run is silent")

        line = self.reap(mode="reap-now")
        self.assertFalse(build.exists())
        self.assertIn("reap-now artifacts=1", line)
        self.assertNotIn("TRUNCATED", line)

    def test_reap_now_still_never_touches_the_main_checkout(self) -> None:
        self.ignore("node_modules/")
        build = self.root / "node_modules"
        build.mkdir()
        (build / "blob.bin").write_bytes(b"x")
        backdate(build)

        line = self.reap(mode="reap-now")

        self.assertTrue(build.exists())
        self.assertNotIn("artifacts=", line)


class RecentlyTouchedPruneTests(unittest.TestCase):
    """The removal-path scan prunes ARTIFACT_DIRS with -path, so a nested entry
    is actually excluded. The old -name form could never match a slash."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("worktree_reaper", SCRIPT)
        cls.reaper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.reaper)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "wt"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "main.swift").write_text("", encoding="utf-8")
        (self.root / ".artifacts" / "derived-data" / "Build").mkdir(parents=True)
        (self.root / "target" / "debug").mkdir(parents=True)
        backdate(self.root)
        self.reaper.LOG_PATH = Path(self.temporary.name) / "reap.log"

    def test_fresh_file_inside_a_pruned_nested_path_reads_idle(self) -> None:
        (self.root / ".artifacts" / "derived-data" / "Build" / "x.o").write_bytes(b"o")
        os.utime(self.root / ".artifacts" / "derived-data" / "Build", (OLD, OLD))
        os.utime(self.root / ".artifacts" / "derived-data", (OLD, OLD))

        self.assertTrue(self.reaper.recently_touched(str(self.root), 48))
        self.assertFalse(
            self.reaper.recently_touched(str(self.root), 48, prune=(".artifacts/derived-data",))
        )

    def test_top_level_prune_still_works_with_path_matching(self) -> None:
        (self.root / "target" / "debug" / "build.o").write_bytes(b"o")
        os.utime(self.root / "target" / "debug", (OLD, OLD))
        os.utime(self.root / "target", (OLD, OLD))

        self.assertTrue(self.reaper.recently_touched(str(self.root), 48))
        self.assertFalse(self.reaper.recently_touched(str(self.root), 48, prune=("target",)))

    def test_pruning_a_nested_path_does_not_hide_a_fresh_sibling(self) -> None:
        (self.root / ".artifacts" / "captures.png").write_bytes(b"png")

        self.assertTrue(
            self.reaper.recently_touched(str(self.root), 48, prune=(".artifacts/derived-data",))
        )


class StdinPayloadTests(unittest.TestCase):
    """A child must never outlive the stdin it was handed.

    `json.load(sys.stdin)` reads to EOF, so a reaper that inherits a stdin which
    never closes parks forever at 0% CPU with nothing in any log. Measured
    2026-08-27: that hung a `python3 -m unittest discover` over this directory
    for 7+ minutes, and the intermittency was nothing but the runner's stdin —
    /dev/null exits in 0.13s, a held-open pipe never exits at all.

    The opposite direction — that bounding the read did not cost the payload the
    hook depends on — is already covered by WorktreeCloseoutTests: each of those
    runs the child with cwd=repo root and a payload naming a LINKED worktree, so
    a payload that failed to arrive would log `state=main action=keep` and fail
    the assertion.
    """

    def elapsed_with_stdin_held_open(self, mode: str) -> float:
        read_fd, write_fd = os.pipe()
        # The write end stays open for the whole test, so the child's stdin is
        # readable-but-never-EOF: the exact shape that used to block forever.
        self.addCleanup(os.close, write_fd)
        with tempfile.TemporaryDirectory() as workdir:
            environment = os.environ.copy()
            environment["WORKTREE_REAPER_STDIN_TIMEOUT"] = "1"
            environment["WORKTREE_REAPER_LOG"] = str(Path(workdir) / "reap.log")
            environment["WORKTREE_CLOSEOUT_LOG"] = str(Path(workdir) / "closeout.log")
            started = time.time()
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), mode],
                cwd=workdir,
                stdin=read_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            os.close(read_fd)
            try:
                process.communicate(timeout=CHILD_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail(f"`{mode}` blocked on an stdin that never reaches EOF")
            return time.time() - started

    def test_reap_gives_up_on_an_stdin_that_never_closes(self) -> None:
        self.assertLess(self.elapsed_with_stdin_held_open("reap"), 30)

    def test_closeout_gives_up_on_an_stdin_that_never_closes(self) -> None:
        self.assertLess(self.elapsed_with_stdin_held_open("closeout"), 30)

class DerivedDataByContents(unittest.TestCase):
    """The name of an Xcode build cache is whatever the session chose; the
    contents are not. Regression for minder 2026-09-18, where the literal
    `.artifacts/derived-data` entry matched none of the 16 real caches on disk
    and the reaper reported "artifact dirs to delete (0)" against 17.23 GB."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("worktree_reaper", SCRIPT)
        cls.reaper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.reaper)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "wt"
        self.artifacts = self.root / ".artifacts"
        self.artifacts.mkdir(parents=True)

    def _make(self, name: str, children: tuple[str, ...]) -> None:
        for child in children:
            (self.artifacts / name / child).mkdir(parents=True, exist_ok=True)

    def test_catches_caches_under_any_name(self) -> None:
        # Every name here was on disk in minder, and the literal entry missed
        # all of them.
        for name in (
            "DerivedData",
            "deriveddata-central-mount",
            "DerivedData-preferences-final-r2",
            "nitpick-remediation-derived",
            "remediation-physical-build",
            "nitpick-device",
            "app-store-build35-derived",
        ):
            with self.subTest(name=name):
                for stale in self.artifacts.iterdir():
                    shutil.rmtree(stale)
                self._make(name, ("Build", "ModuleCache.noindex"))
                self.assertEqual(
                    self.reaper.derived_data_children(str(self.root)),
                    (f".artifacts/{name}",),
                )

    def test_spares_evidence_including_build_named_captures(self) -> None:
        # All observed in minder, all kept. The captures dirs carry "build" in
        # the name, so a substring rule would have destroyed device evidence,
        # and the .xcarchive holds the only dSYMs for a shipped build.
        self._make("nitpick-copy-build25-verified-captures", ("frames",))
        self._make("remediation-unit.xcresult", ("Data",))
        self._make("Minder-1.0-build37.xcarchive", ("dSYMs", "Products"))
        self._make("build-results", ("reports",))
        self._make("remediation-phone-backup", ("AppGroup", "Documents", "Library"))
        self.assertEqual(self.reaper.derived_data_children(str(self.root)), ())

    def test_build_alone_is_not_a_cache(self) -> None:
        self._make("some-output", ("Build",))
        self.assertEqual(self.reaper.derived_data_children(str(self.root)), ())

    def test_parent_artifacts_is_never_returned(self) -> None:
        self._make("DerivedData", ("Build", "SDKStatCaches.noindex"))
        for got in self.reaper.derived_data_children(str(self.root)):
            self.assertNotEqual(got, ".artifacts")
            self.assertTrue(got.startswith(".artifacts/"))

    def test_literal_entry_and_contents_test_yield_one_target(self) -> None:
        """A cache named exactly `.artifacts/derived-data` matches the literal
        ARTIFACT_DIRS entry AND the contents test. Counting it twice inflated
        the report's reclaimable total (caught in review of 9f92e0d)."""
        self._make("derived-data", ("Build", "ModuleCache.noindex"))
        combined = dict.fromkeys(
            self.reaper.ARTIFACT_DIRS
            + self.reaper.derived_data_children(str(self.root))
        )
        self.assertEqual(
            sum(1 for name in combined if name == ".artifacts/derived-data"), 1
        )

    def test_missing_artifacts_dir_is_empty_not_an_error(self) -> None:
        shutil.rmtree(self.artifacts)
        self.assertEqual(self.reaper.derived_data_children(str(self.root)), ())


if __name__ == "__main__":
    unittest.main()
