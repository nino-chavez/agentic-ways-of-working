#!/usr/bin/env python3
"""Regression test for output-trim.py. Run: python3 -m unittest test_output_trim"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).with_name("output-trim.py")


def run(payload, state_dir, env_extra=None):
    env = {**os.environ, "OUTPUT_TRIM_STATE_DIR": state_dir, **(env_extra or {})}
    # input= closes the child's stdin and timeout= bounds it; without both a
    # hook that reads to EOF hangs on whatever stdin the runner inherited.
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10, env=env)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)


def bash(stdout, **kw):
    return {"tool_name": "Bash", "tool_use_id": "toolu_test1",
            "tool_response": {"stdout": stdout, "stderr": "keep me",
                              "interrupted": False, "isImage": False, **kw}}


class OutputTrim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_small_output_untouched(self):
        self.assertEqual(run(bash("ok\n" * 10), self.dir), (0, None))

    def test_other_tools_untouched(self):
        big = "\n".join(f"line {i}" for i in range(5000))
        for tool in ("Read", "Edit", "Agent", "WebFetch", "mcp__x__y"):
            p = bash(big)
            p["tool_name"] = tool
            self.assertEqual(run(p, self.dir), (0, None), tool)

    def test_elide_keeps_shape_head_tail_and_salvages_errors(self):
        lines = [f"line {i}" for i in range(5000)]
        lines[2500] = "TypeError: boom at widget.ts:41"
        code, out = run(bash("\n".join(lines)), self.dir)
        self.assertEqual(code, 0)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PostToolUse")
        new = hso["updatedToolOutput"]
        self.assertEqual(set(new), {"stdout", "stderr", "interrupted", "isImage"})
        self.assertEqual(new["stderr"], "keep me")
        self.assertIn("line 0\n", new["stdout"])
        self.assertTrue(new["stdout"].endswith("line 4999"))
        self.assertIn("TypeError: boom at widget.ts:41", new["stdout"])
        self.assertNotIn("line 2000\n", new["stdout"])
        self.assertLess(len(new["stdout"]), 8000)

    def test_spill_holds_the_original(self):
        original = "\n".join(f"line {i}" for i in range(5000))
        run(bash(original), self.dir)
        spilled = Path(self.dir, "spill", "toolu_test1.txt")
        self.assertEqual(spilled.read_text(), original)

    def test_harness_persisted_path_wins_over_spill(self):
        big = "\n".join(f"line {i}" for i in range(5000))
        _, out = run(bash(big, persistedOutputPath="/tmp/full.txt"), self.dir)
        new = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("full output: /tmp/full.txt]", new["stdout"])
        self.assertEqual(new["persistedOutputPath"], "/tmp/full.txt")
        self.assertFalse(Path(self.dir, "spill").exists())

    def test_scrub_is_lossless_below_elide_threshold(self):
        text = "\x1b[32mgreen\x1b[0m\n" + "same\n" * 300 + "\n\n\n\nend"
        _, out = run(bash(text), self.dir)
        got = out["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
        self.assertEqual(got, "green\nsame\n[repeated 300x]\n\nend")

    def test_not_smaller_means_no_rewrite(self):
        text = "\n".join(f"unique {i}" for i in range(150))
        self.assertGreater(len(text), 1000)
        self.assertEqual(run(bash(text), self.dir), (0, None))

    def test_off_switches(self):
        big = bash("\n".join(f"line {i}" for i in range(5000)))
        self.assertEqual(run(big, self.dir, {"OUTPUT_TRIM_OFF": "1"}), (0, None))
        Path(self.dir, ".guard-off").touch()
        self.assertEqual(run(big, self.dir), (0, None))

    def test_garbage_payload_fails_open(self):
        p = subprocess.run([sys.executable, str(HOOK)], input="not json",
                           capture_output=True, text=True, timeout=10,
                           env={**os.environ, "OUTPUT_TRIM_STATE_DIR": self.dir})
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertEqual(run({"tool_name": "Bash", "tool_response": "str"}, self.dir), (0, None))


if __name__ == "__main__":
    unittest.main()
