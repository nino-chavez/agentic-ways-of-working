#!/usr/bin/env python3
"""Fixture coverage for the UserPromptSubmit closeout hook, including its
advisory screen-composition-reviewer pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("session-closeout.py")
SUBPROCESS_TIMEOUT = 10  # bound on the *test's* subprocess.run call itself


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_hook(self, prompt: str, cwd: Path, env: dict | None = None) -> dict:
        payload = {"prompt": prompt, "cwd": str(cwd)}
        full_env = os.environ.copy()
        full_env.pop("BLUEPRINT_HOME", None)  # never let the real machine leak in
        if env:
            full_env.update(env)
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload),
            stdin=None,  # `input=` supersedes; explicit per repo subprocess-test rule
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            env=full_env,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout.strip()
        if not stdout:
            return {}
        return json.loads(stdout)

    def fake_blueprint_cli(self, home: Path, body: str) -> None:
        """Write a stub `$home/bin/blueprint.mjs` that prints `body` to stdout."""
        bin_dir = home / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "blueprint.mjs"
        script.write_text(
            "#!/usr/bin/env node\n" + body + "\n",
            encoding="utf-8",
        )
        script.chmod(0o755)


class NonClosoutPromptTests(_Fixture):
    def test_non_closeout_prompt_prints_nothing(self) -> None:
        (self.root / "blueprint.yml").write_text("design_intent: rethink\n", encoding="utf-8")
        out = self.run_hook("what does this function do?", self.root)
        self.assertEqual(out, {}, "a non-closeout prompt must print nothing at all")


class NoPatternAdoptedTests(_Fixture):
    def test_no_blueprint_yml_no_advisory(self) -> None:
        out = self.run_hook("close out", self.root)
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("session-closeout skill", context)
        self.assertNotIn("screen-composition-reviewer", context)

    def test_blueprint_yml_without_design_intent_no_advisory(self) -> None:
        (self.root / "blueprint.yml").write_text("tier: 1\n", encoding="utf-8")
        out = self.run_hook("wrap this up", self.root)
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("screen-composition-reviewer", context)

    def test_walks_up_ancestors_for_blueprint_yml(self) -> None:
        (self.root / "blueprint.yml").write_text("design_intent: refit\n", encoding="utf-8")
        nested = self.root / "prototype" / "deep"
        nested.mkdir(parents=True)
        env = {"BLUEPRINT_HOME": str(self.root / "no-such-home")}
        out = self.run_hook("please archive-ready this", nested, env=env)
        context = out["hookSpecificOutput"]["additionalContext"]
        # design_intent IS declared at an ancestor, so the walk must find it
        # and attempt the reviewer (and report the missing CLI), not stay silent.
        self.assertIn("screen-composition-reviewer", context)
        self.assertIn("skipped", context)


class AdvisoryPassTests(_Fixture):
    def test_reviewer_output_is_appended_verbatim(self) -> None:
        (self.root / "blueprint.yml").write_text("design_intent: preserve\n", encoding="utf-8")
        home = self.root / "blueprint-home"
        self.fake_blueprint_cli(
            home,
            "console.log('! WARN — screen-composition-reviewer  (fixture)');",
        )
        out = self.run_hook("end this session", self.root, env={"BLUEPRINT_HOME": str(home)})
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Blueprint screen-composition-reviewer (advisory, never blocking):", context)
        self.assertIn("! WARN — screen-composition-reviewer  (fixture)", context)
        # the closeout-routing sentence must still be present alongside it
        self.assertIn("session-closeout skill", context)

    def test_missing_cli_reports_skip_reason(self) -> None:
        (self.root / "blueprint.yml").write_text("design_intent: rethink\n", encoding="utf-8")
        env = {"BLUEPRINT_HOME": str(self.root / "does-not-exist")}
        out = self.run_hook("close out", self.root, env=env)
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("screen-composition-reviewer: skipped (blueprint.mjs not found", context)

    def test_reviewer_timeout_reports_skip_reason(self) -> None:
        (self.root / "blueprint.yml").write_text("design_intent: refit\n", encoding="utf-8")
        home = self.root / "blueprint-home"
        # sleep longer than the (test-shortened) timeout so the hook's own
        # subprocess.run raises TimeoutExpired.
        self.fake_blueprint_cli(home, "setTimeout(() => {}, 5000);")
        env = {"BLUEPRINT_HOME": str(home), "BLUEPRINT_REVIEW_TIMEOUT_SECONDS": "1"}
        out = self.run_hook("wrap up this task", self.root, env=env)
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("screen-composition-reviewer: skipped (timed out after 1s)", context)

    def test_blocked_verdict_is_advisory_only_hook_still_exits_clean(self) -> None:
        # BLOCKED (exit 1) from the reviewer must not change this hook's own
        # exit code or suppress the closeout-routing context.
        (self.root / "blueprint.yml").write_text("design_intent: rethink\n", encoding="utf-8")
        home = self.root / "blueprint-home"
        self.fake_blueprint_cli(
            home,
            textwrap.dedent(
                """
                console.log('\\u2717 BLOCKED — screen-composition-reviewer  (fixture)');
                process.exit(1);
                """
            ).strip(),
        )
        out = self.run_hook("prepare this task for archive", self.root, env={"BLUEPRINT_HOME": str(home)})
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("BLOCKED", context)
        self.assertIn("session-closeout skill", context)


if __name__ == "__main__":
    unittest.main()
