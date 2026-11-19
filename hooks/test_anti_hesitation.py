#!/usr/bin/env python3
"""Fixture coverage for the Stop hook: permission questions and command hand-offs.

The hand-off check exists because a closing that gives the user a shell command
to run is the same stop as "want me to continue?". It must not fire when the
command was refused first, or when the closing says why only the user can run
it. The second half matters most: a hook that blocks a legitimate hand-off tells
the agent to run a command it was right to leave alone.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).with_name("anti-hesitation.py")

HANDOFF = (
    "The PR is merged. Run this yourself to bring the checkout up to date:\n\n"
    "```bash\ngit -C ~/project merge --ff-only origin/main\n```\n"
)
REFUSED = "PreToolUse:Bash hook error: Worktree isolation guard: BLOCKED.\n\nYou are about to run..."
FEEDBACK = {"type": "user", "message": {"content": "Stop hook feedback:\nrestate the closing"}}


def load_hook():
    spec = importlib.util.spec_from_file_location("anti_hesitation", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prompt(text: str) -> dict:
    return {"type": "user", "origin": {"kind": "human"}, "message": {"content": text}}


def tool_error(text: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "is_error": True, "content": text}]}}


def tool_ok(text: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "content": text}]}}


def assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class ClassifyHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hook = load_hook()

    def test_an_unexplained_handoff_is_flagged_with_its_command(self) -> None:
        self.assertEqual(
            self.hook.classify_handoff(HANDOFF, []),
            "git -C ~/project merge --ff-only origin/main",
        )

    def test_other_wordings_of_a_handoff_are_flagged(self) -> None:
        for closing in (
            "Two commands left.\n\n```bash\nnpm run build\n```\n\nReply once it has run.",
            "```sh\nmake sync\n```\nYou will need to run that from your terminal.",
            "Run these from your own shell:\n```zsh\ngit pull --ff-only\n```",
        ):
            with self.subTest(closing=closing[:30]):
                self.assertIsNotNone(self.hook.classify_handoff(closing, []))

    def test_a_command_shown_without_asking_the_user_to_run_it_passes(self) -> None:
        closing = "The dev server starts with:\n\n```bash\nnpm run dev\n```\n\nIt listens on 5173."
        self.assertIsNone(self.hook.classify_handoff(closing, []))

    def test_ordinary_status_and_usage_wording_passes(self) -> None:
        for closing in (
            "Merging once it lands.\n\n```bash\ngh pr merge 12 --merge\n```",
            "You can run this anytime to recheck:\n```bash\ngit status\n```",
            "You should run the suite before a release:\n```bash\nnpm test\n```",
        ):
            with self.subTest(closing=closing[:30]):
                self.assertIsNone(self.hook.classify_handoff(closing, []))

    def test_handoff_wording_without_a_shell_block_passes(self) -> None:
        self.assertIsNone(self.hook.classify_handoff("Run this yourself when you are ready.", []))
        self.assertIsNone(self.hook.classify_handoff(
            "Run this yourself:\n```json\n{\"a\": 1}\n```", []))

    def test_a_closing_that_gives_the_reason_passes(self) -> None:
        # One reason word per sentence, so each can fail on its own.
        for why in (
            "The worktree guard stopped my fast-forward.",
            "The classifier turned the merge down.",
            "It asks for your sudo password.",
            "This writes to production.",
            "The command is interactive.",
            "It needs your sign-in.",
            "This publishes the release notes to the live site.",
            "It posts to the team channel.",
            "It sends the email to the whole list.",
            "That is a deploy to the live site.",
            "Whether to run it is your call.",
            "This one belongs on the work Mac.",
            "Run it on the other machine after it syncs.",
        ):
            with self.subTest(why=why):
                self.assertIsNone(self.hook.classify_handoff(why + "\n\n" + HANDOFF, []))

    def test_a_refused_tool_call_makes_the_handoff_legitimate(self) -> None:
        for refusal in (
            REFUSED,
            "Permission for this action was denied by the Claude Code auto mode classifier.",
            "The user doesn't want to proceed with this tool use.",
            "rm: /usr/local/x: Operation not permitted",
        ):
            with self.subTest(refusal=refusal[:30]):
                self.assertIsNone(self.hook.classify_handoff(HANDOFF, [refusal]))

    def test_an_ordinary_command_failure_is_not_a_refusal(self) -> None:
        self.assertIsNotNone(
            self.hook.classify_handoff(HANDOFF, ["Exit code 1\nfatal: not a git repository"]))


class _Transcript(unittest.TestCase):
    def setUp(self) -> None:
        self.hook = load_hook()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "session.jsonl"

    def write(self, *rows: dict) -> None:
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class RecentRefusalsTests(_Transcript):
    def test_a_refusal_in_this_turn_is_found(self) -> None:
        self.write(prompt("merge it"), tool_error(REFUSED), assistant(HANDOFF))
        self.assertEqual(len(self.hook.recent_refusals(self.path)), 1)

    def test_a_refusal_one_prompt_back_is_found(self) -> None:
        self.write(prompt("merge it"), tool_error(REFUSED), assistant("Blocked."),
                   prompt("next"), tool_ok("ok"), assistant(HANDOFF))
        self.assertTrue(any("BLOCKED" in r for r in self.hook.recent_refusals(self.path)))

    def test_a_refusal_older_than_two_prompts_is_not(self) -> None:
        self.write(prompt("merge it"), tool_error(REFUSED), assistant("Blocked."),
                   prompt("next"), assistant("ok"), prompt("and now"), assistant(HANDOFF))
        self.assertEqual(self.hook.recent_refusals(self.path), [])

    def test_hook_feedback_does_not_count_as_a_prompt(self) -> None:
        self.write(prompt("merge it"), tool_error(REFUSED), assistant("Blocked."),
                   prompt("next"), assistant(HANDOFF), FEEDBACK, assistant(HANDOFF))
        self.assertEqual(len(self.hook.recent_refusals(self.path)), 1)

    def test_lines_the_harness_wrote_do_not_end_the_lookback(self) -> None:
        skill = {"type": "user", "isMeta": True, "message": {"content": "Base directory for this skill: /x"}}
        notice = {"type": "user", "origin": {"kind": "task-notification"},
                  "message": {"content": "<task-notification>done</task-notification>"}}
        echo = {"type": "user", "message": {"content": "<command-name>/model</command-name>"}}
        self.write(prompt("merge it"), tool_error(REFUSED), assistant("Blocked."),
                   prompt("next"), skill, notice, echo, assistant(HANDOFF))
        self.assertTrue(any("BLOCKED" in r for r in self.hook.recent_refusals(self.path)))

    def test_a_transcript_with_no_origin_marks_counts_plain_text_prompts(self) -> None:
        def old(text: str) -> dict:
            return {"type": "user", "message": {"content": text}}
        self.write(old("merge it"), tool_error(REFUSED), assistant("Blocked."),
                   old("next"), assistant("ok"), old("and now"), assistant(HANDOFF))
        self.assertEqual(self.hook.recent_refusals(self.path), [])
        self.write(old("merge it"), tool_error(REFUSED), assistant("Blocked."),
                   old("next"), assistant(HANDOFF))
        self.assertEqual(len(self.hook.recent_refusals(self.path)), 1)
        # Even there, a meta line (a skill body) is not a prompt.
        meta = {"type": "user", "isMeta": True, "message": {"content": "Base directory for this skill: /x"}}
        self.write(old("merge it"), tool_error(REFUSED), assistant("Blocked."),
                   old("next"), meta, assistant(HANDOFF))
        self.assertEqual(len(self.hook.recent_refusals(self.path)), 1)
        # Nor is hook feedback, which carries no origin mark either.
        self.write(old("merge it"), tool_error(REFUSED), assistant("Blocked."),
                   old("next"), assistant(HANDOFF), FEEDBACK, assistant(HANDOFF))
        self.assertEqual(len(self.hook.recent_refusals(self.path)), 1)

    def test_an_unreadable_transcript_yields_nothing(self) -> None:
        self.assertEqual(self.hook.recent_refusals(Path(self.tmp.name) / "missing.jsonl"), [])


class EndToEndTests(_Transcript):
    """Run the hook the way the harness does: a payload on stdin, JSON on stdout."""

    def run_hook(self, *rows: dict) -> dict | None:
        self.write(*rows)
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"transcript_path": str(self.path)}),
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_an_unexplained_handoff_blocks_the_stop(self) -> None:
        out = self.run_hook(prompt("merge it"), tool_ok("merged"), assistant(HANDOFF))
        self.assertEqual(out["decision"], "block")
        self.assertIn("run it and report", out["reason"])
        self.assertIn("Do not run it if it publishes", out["reason"])
        self.assertIn("git -C ~/project merge --ff-only origin/main", out["reason"])

    def test_a_handoff_after_a_refusal_is_allowed(self) -> None:
        self.assertIsNone(self.run_hook(prompt("merge it"), tool_error(REFUSED), assistant(HANDOFF)))

    def test_the_rewrite_of_a_blocked_handoff_is_not_blocked_again(self) -> None:
        self.assertIsNone(self.run_hook(prompt("merge it"), assistant(HANDOFF), FEEDBACK, assistant(HANDOFF)))

    def test_a_permission_question_still_blocks_the_stop(self) -> None:
        out = self.run_hook(prompt("do step one"), assistant("Step one is done. Want me to continue?"))
        self.assertEqual(out["decision"], "block")
        self.assertIn("hesitation question", out["reason"])

    def test_the_rewrite_of_a_blocked_question_is_not_blocked_again(self) -> None:
        question = assistant("Step one is done. Want me to continue?")
        self.assertIsNone(self.run_hook(prompt("do step one"), question, FEEDBACK, question))

    def test_a_new_prompt_after_feedback_can_be_blocked_again(self) -> None:
        question = assistant("Step one is done. Want me to continue?")
        out = self.run_hook(prompt("do step one"), question, FEEDBACK, assistant("Continuing."),
                            prompt("do step two"), question)
        self.assertEqual(out["decision"], "block")

    def test_an_ordinary_closing_is_allowed(self) -> None:
        self.assertIsNone(self.run_hook(prompt("merge it"), assistant("Merged and synced. Nothing is pending.")))


if __name__ == "__main__":
    unittest.main()
