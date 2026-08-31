#!/usr/bin/env python3
"""Fixture coverage for the SessionEnd worktree closeout path."""

from __future__ import annotations

import faulthandler
import json
import os
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

    def reap(self) -> str:
        environment = os.environ.copy()
        environment["WORKTREE_REAPER_LOG"] = str(self.log)
        environment["WORKTREE_REAPER_ARTIFACT_IDLE_HOURS"] = "0"
        subprocess.run(
            [sys.executable, str(SCRIPT), "reap"],
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

if __name__ == "__main__":
    unittest.main()
