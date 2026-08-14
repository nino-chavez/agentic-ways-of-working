#!/usr/bin/env python3
"""Fixture coverage for the SessionEnd worktree closeout path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("worktree-reaper.py")


class WorktreeCloseoutTests(unittest.TestCase):
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
        )

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


if __name__ == "__main__":
    unittest.main()
