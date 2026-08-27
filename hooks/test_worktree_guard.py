#!/usr/bin/env python3
"""Fixture coverage for the PreToolUse worktree-isolation guard.

The regression this file exists for (operator, 2026-08-27): two sessions in one
repo, session B correctly pushed into a worktree, session A then merging B's
branch in the shared main checkout and leaving MERGE_HEAD plus staged files
behind. Nothing blocked it — `merge` was not a contended op, and by then B was
no longer in main.

Both arrival orders are covered deliberately. The incident was first diagnosed
as an incumbency bug ("whichever session registered first keeps main"), and the
symmetry tests below are what falsify that: the incumbent is denied too, as soon
as a second lock appears. The real defect was op coverage plus a contention
basis that counted worktree-isolated sessions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("worktree-guard.py")


class _TwoSessionRepo(unittest.TestCase):
    """A repo with a shared main checkout and one linked worktree on 'feat/x'."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.git("init", "--initial-branch=main", str(self.root))
        self.git("config", "user.name", "Guard Test", cwd=self.root)
        self.git("config", "user.email", "guard@example.invalid", cwd=self.root)
        (self.root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=self.root)
        self.git("commit", "-m", "base", cwd=self.root)
        self.worktree = self.root / ".worktrees" / "feat-x"
        self.git("worktree", "add", "-b", "feat/x", str(self.worktree), "main", cwd=self.root)
        self.locks = self.root / ".git" / ".claude-sessions"

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    # -- driving the hook ---------------------------------------------------

    def _run(self, payload: dict) -> tuple[str, str]:
        """(decision, reason) for one PreToolUse call. Silent exit => allow."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "check"],
            input=json.dumps(payload), check=True, capture_output=True, text=True,
        )
        out = result.stdout.strip()
        if not out:
            return "allow", ""
        block = json.loads(out)["hookSpecificOutput"]
        return block["permissionDecision"], block.get("permissionDecisionReason", "")

    def bash(self, session: str, cwd: Path, command: str) -> tuple[str, str]:
        return self._run({
            "tool_name": "Bash", "session_id": session, "cwd": str(cwd),
            "tool_input": {"command": command},
        })

    def edit(self, session: str, cwd: Path, path: Path) -> tuple[str, str]:
        return self._run({
            "tool_name": "Edit", "session_id": session, "cwd": str(cwd),
            "tool_input": {"file_path": str(path), "old_string": "a", "new_string": "b"},
        })

    def arrive(self, session: str, cwd: Path) -> None:
        """Register/refresh a session's lock via a harmless heartbeat call."""
        decision, _ = self.bash(session, cwd, "git status")
        self.assertEqual(decision, "allow", "a heartbeat call must never block")


class ArrivalOrderTests(_TwoSessionRepo):
    """Both sessions in the shared main checkout: the guard must be symmetric."""

    def test_solo_session_in_main_is_never_blocked(self) -> None:
        # The baseline the whole guard is calibrated against: one that blocks
        # legitimate solo work gets ripped out.
        self.arrive("A", self.root)
        self.assertEqual(self.bash("A", self.root, "git commit -m x")[0], "allow")
        self.assertEqual(self.bash("A", self.root, "git merge feat/x")[0], "allow")

    def test_newcomer_is_denied_when_incumbent_holds_main(self) -> None:
        self.arrive("A", self.root)                      # A first
        decision, reason = self.bash("B", self.root, "git commit -m x")
        self.assertEqual(decision, "deny")
        self.assertIn("session A", reason)

    def test_incumbent_is_denied_once_newcomer_appears(self) -> None:
        # The falsifier for the incumbency hypothesis. A arrived first and was
        # alone; the moment B's lock exists, A's commit is denied too.
        self.arrive("A", self.root)                      # A first
        self.arrive("B", self.root)
        decision, reason = self.bash("A", self.root, "git commit -m x")
        self.assertEqual(decision, "deny")
        self.assertIn("session B", reason)

    def test_denial_is_identical_in_the_reverse_arrival_order(self) -> None:
        # Same two sessions, B registering first. Both are denied either way.
        self.arrive("B", self.root)                      # B first
        self.arrive("A", self.root)
        self.assertEqual(self.bash("B", self.root, "git commit -m x")[0], "deny")
        self.assertEqual(self.bash("A", self.root, "git commit -m x")[0], "deny")


class ContentionBasisTests(_TwoSessionRepo):
    """Only sessions sharing the main checkout contend for it."""

    def test_worktree_session_does_not_block_main(self) -> None:
        # Was DENY before 2026-08-27: the Bash path counted worktree sessions,
        # so A could not commit in main while B sat safely isolated.
        self.arrive("A", self.root)
        self.arrive("B", self.worktree)
        self.assertEqual(self.bash("A", self.root, "git commit -m x")[0], "allow")
        self.assertEqual(self.bash("A", self.root, "git pull")[0], "allow")

    def test_edit_and_bash_paths_agree(self) -> None:
        # These two paths disagreed on what counts as contention; they must not.
        self.arrive("A", self.root)
        self.arrive("B", self.worktree)
        self.assertEqual(self.edit("A", self.root, self.root / "tracked.txt")[0], "allow")

        self.arrive("B", self.root)  # B back in main => both paths deny
        self.assertEqual(self.edit("A", self.root, self.root / "tracked.txt")[0], "deny")
        self.assertEqual(self.bash("A", self.root, "git commit -m x")[0], "deny")

    def test_work_inside_a_worktree_is_always_allowed(self) -> None:
        self.arrive("A", self.root)
        self.arrive("B", self.worktree)
        self.assertEqual(self.bash("B", self.worktree, "git commit -m x")[0], "allow")

    def test_guard_off_disables_the_repo(self) -> None:
        self.arrive("A", self.root)
        self.arrive("B", self.root)
        (self.locks / ".guard-off").write_text("", encoding="utf-8")
        self.assertEqual(self.bash("A", self.root, "git commit -m x")[0], "allow")


class ContendedOpCoverageTests(_TwoSessionRepo):
    """The op set that stranded the operator merge. Both sessions in main."""

    def setUp(self) -> None:
        super().setUp()
        self.arrive("A", self.root)
        self.arrive("B", self.root)

    def test_working_tree_mutators_are_denied(self) -> None:
        for command in (
            "git commit -m x", "git merge feat/x", "git rebase feat/x",
            "git cherry-pick abc123", "git revert HEAD", "git am patch.eml",
            "git pull", "git reset --hard HEAD~1", "git stash pop",
            "git checkout -b new", "git switch feat/x", "git branch new",
            "git checkout feat/x",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.bash("A", self.root, command)[0], "deny")

    def test_non_mutating_ops_stay_allowed(self) -> None:
        # Guard against over-blocking: each of these leaves the working tree
        # and HEAD of the shared checkout alone.
        for command in (
            "git status", "git log --oneline", "git diff", "git fetch",
            "git reset --soft HEAD~1", "git stash push", "git push",
            "git checkout -- tracked.txt", "git checkout tracked.txt",
            "git branch --list", "git branch -d old",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.bash("A", self.root, command)[0], "allow")

    def test_op_in_an_uncontended_repo_is_not_blocked(self) -> None:
        # `git -C <other-repo>` must be judged against that repo, not this one.
        other = Path(self.temporary.name) / "other"
        self.git("init", "--initial-branch=main", str(other))
        decision, _ = self.bash("A", self.root, f"git -C {other} commit -m x")
        self.assertEqual(decision, "allow")


class BranchOwnershipTests(_TwoSessionRepo):
    """Folding in a branch another live session holds escalates to the user."""

    def test_merging_a_held_branch_asks(self) -> None:
        # The incident, exactly: B isolated on feat/x, A merges it from main.
        self.arrive("A", self.root)
        self.arrive("B", self.worktree)
        decision, reason = self.bash("A", self.root, "git merge feat/x")
        self.assertEqual(decision, "ask")
        self.assertIn("feat/x", reason)
        self.assertIn("session B", reason)

    def test_rebase_onto_a_held_branch_asks(self) -> None:
        self.arrive("A", self.root)
        self.arrive("B", self.worktree)
        self.assertEqual(self.bash("A", self.root, "git rebase feat/x")[0], "ask")

    def test_option_values_are_not_mistaken_for_refs(self) -> None:
        self.arrive("A", self.root)
        self.arrive("B", self.worktree)
        # -m consumes its value; feat/x is still found as the operand.
        self.assertEqual(self.bash("A", self.root, "git merge -m wip feat/x")[0], "ask")
        # A message that happens to equal a held branch name is not a ref.
        self.assertEqual(self.bash("A", self.root, "git merge -m feat/x main")[0], "allow")

    def test_unheld_branch_merges_without_asking(self) -> None:
        self.arrive("A", self.root)
        self.arrive("B", self.worktree)
        self.assertEqual(self.bash("A", self.root, "git merge origin/unrelated")[0], "allow")

    def test_check_applies_from_within_a_worktree(self) -> None:
        # The race is on the branch, so occupancy of main is irrelevant here.
        self.arrive("A", self.worktree)
        self.arrive("B", self.root)  # B holds 'main'
        self.assertEqual(self.bash("A", self.worktree, "git merge main")[0], "ask")

    def test_occupancy_denial_outranks_the_branch_ask(self) -> None:
        # Both sessions in main AND the merge target held: a stomped checkout is
        # the worse outcome, so the hard deny must win.
        self.arrive("A", self.root)
        self.arrive("B", self.root)
        self.assertEqual(self.bash("A", self.root, "git merge main")[0], "deny")


class UnwindGuidanceTests(_TwoSessionRepo):
    """A half-finished merge is the damage mode; the message must name the fix."""

    def test_clean_tree_gets_no_unwind_advice(self) -> None:
        # The guard fires pre-tool, so the call that first blocks a merge has no
        # MERGE_HEAD yet. Advice here would be noise.
        self.arrive("A", self.root)
        self.arrive("B", self.worktree)
        _, reason = self.bash("A", self.root, "git merge feat/x")
        self.assertNotIn("--abort", reason)

    def test_stranded_merge_is_detected_and_unwound(self) -> None:
        self.arrive("A", self.root)
        self.arrive("B", self.worktree)
        (self.root / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        _, reason = self.bash("A", self.root, "git merge feat/x")
        self.assertIn("ALREADY mid-merge", reason)
        self.assertIn("merge --abort", reason)

    def test_stranded_rebase_directory_is_detected(self) -> None:
        # `rebase -i` leaves a state directory, not REBASE_HEAD.
        self.arrive("A", self.root)
        self.arrive("B", self.root)
        (self.root / ".git" / "rebase-merge").mkdir()
        _, reason = self.bash("A", self.root, "git commit -m x")
        self.assertIn("ALREADY mid-rebase", reason)
        self.assertIn("rebase --abort", reason)

    def test_worktree_state_is_read_per_checkout(self) -> None:
        # MERGE_HEAD lives in the per-checkout git dir, not the common dir, so a
        # stranded merge in main must not be reported inside the worktree.
        self.arrive("A", self.root)
        self.arrive("B", self.root)
        (self.root / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        _, reason = self.bash("A", self.root, "git commit -m x")
        self.assertIn("ALREADY mid-merge", reason)
        self.assertEqual(self.bash("B", self.worktree, "git commit -m x")[0], "allow")


class FailOpenTests(_TwoSessionRepo):
    """Anything uncertain must allow."""

    def test_unparseable_command_allows(self) -> None:
        self.arrive("A", self.root)
        self.arrive("B", self.root)
        self.assertEqual(self.bash("A", self.root, "git commit -m 'unbalanced")[0], "allow")

    def test_outside_a_repo_allows(self) -> None:
        self.arrive("A", self.root)
        self.arrive("B", self.root)
        outside = Path(self.temporary.name) / "plain"
        outside.mkdir()
        self.assertEqual(self.bash("A", outside, "git commit -m x")[0], "allow")

    def test_readonly_sessions_never_contend(self) -> None:
        self.arrive("A", self.root)
        self.arrive("B", self.root)
        lock = json.loads((self.locks / "B.json").read_text(encoding="utf-8"))
        lock["role"] = "readonly"
        (self.locks / "B.json").write_text(json.dumps(lock), encoding="utf-8")
        self.assertEqual(self.bash("A", self.root, "git commit -m x")[0], "allow")


if __name__ == "__main__":
    unittest.main()
