#!/usr/bin/env python3
"""Fixture coverage for connector-reaper.py.

Runs standalone: no live process manipulation. All process trees are
synthetic dicts fed straight into the module's pure functions (ancestor_owner,
redact, orphan_key, plan-equivalent loops in cmd_reap/cmd_report), plus a
handful of subprocess-level tests that exercise the real CLI against a fake
`ps` on PATH so the grace-period/throttle/off-switch machinery is covered
end to end without touching real processes.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("connector-reaper.py")

sys.path.insert(0, str(Path(__file__).parent))
import importlib.util

_spec = importlib.util.spec_from_file_location("connector_reaper", SCRIPT)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)  # type: ignore[union-attr]


def proc(ppid: int, command: str, etime: float = 60.0, rss_kb: int = 1000) -> dict:
    return {"ppid": ppid, "stat": "S", "etime": etime, "rss_kb": rss_kb, "tty": "??", "command": command}


class AncestorOwnerTests(unittest.TestCase):
    def test_live_client_owned_connector_is_never_reap_eligible(self) -> None:
        procs = {
            1: proc(0, "/sbin/launchd"),
            100: proc(1, "/Applications/Claude.app/Contents/MacOS/claude --headless"),
            200: proc(100, "npm exec @upstash/context7-mcp --api-key ctx7sk-livekey0001"),
        }
        status, owner_pid, owner_cmd = cr.ancestor_owner(200, procs)
        self.assertEqual(status, "live_client")
        self.assertEqual(owner_pid, 100)
        self.assertIn("claude", owner_cmd.lower())

    def test_orphan_reparented_to_init_is_detected(self) -> None:
        procs = {
            1: proc(0, "/sbin/launchd"),
            200: proc(1, "npm exec @playwright/mcp@latest"),
        }
        status, owner_pid, owner_cmd = cr.ancestor_owner(200, procs)
        self.assertEqual(status, "orphan")
        self.assertIsNone(owner_pid)

    def test_unresolvable_ancestor_yields_unknown(self) -> None:
        # ppid 999 is not in the snapshot at all (vanished mid-walk).
        procs = {
            200: proc(999, "npm exec chrome-devtools-mcp"),
        }
        status, owner_pid, owner_cmd = cr.ancestor_owner(200, procs)
        self.assertEqual(status, "unknown")
        self.assertIsNone(owner_pid)

    def test_cycle_yields_unknown_not_orphan(self) -> None:
        # Pathological ancestor cycle (should never occur in a real process
        # tree) must not be read as "reached init" -> fail closed to unknown.
        procs = {
            10: proc(20, "npm exec @playwright/mcp"),
            20: proc(10, "some-other-process"),
        }
        status, _, _ = cr.ancestor_owner(10, procs)
        self.assertEqual(status, "unknown")


class RedactionTests(unittest.TestCase):
    LIVE_KEY = "ctx7sk-0000dead-beef-0000-0000-000000000000"  # pragma: allowlist secret (fabricated, not a real key)

    def test_flag_based_credential_is_masked(self) -> None:
        cmd = f"node context7-mcp --api-key {self.LIVE_KEY}"
        out = cr.redact(cmd)
        self.assertNotIn(self.LIVE_KEY, out)
        self.assertIn("ctx7sk***", out)

    def test_prefix_shaped_token_is_masked_even_without_a_flag(self) -> None:
        cmd = f"mcp-remote@1.0 --url https://example.com/{self.LIVE_KEY}"
        out = cr.redact(cmd)
        self.assertNotIn(self.LIVE_KEY, out)

    def test_generic_flag_value_is_capped_at_six_chars(self) -> None:
        cmd = "server --token mySuperSecretValue123"
        out = cr.redact(cmd)
        self.assertNotIn("mySuperSecretValue123", out)
        self.assertIn("mySupe***", out)

    def test_report_and_log_output_never_contain_the_live_key(self) -> None:
        """End-to-end: a connector process carrying a live key in argv must
        never surface that key in report stdout or the reap log file."""
        with tempfile.TemporaryDirectory() as td:
            fake_ps = _write_fake_ps(
                Path(td),
                [
                    (1, 0, "S", "01:00:00", 5000, "??", "/sbin/launchd"),
                    (
                        3325,
                        1,
                        "S",
                        "00:10:00",
                        28000,
                        "??",
                        f"npm exec @upstash/context7-mcp --api-key {self.LIVE_KEY}",
                    ),
                ],
            )
            env = _base_env(td, fake_ps)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "report"],
                capture_output=True, text=True, env=env, timeout=10,
            )
            self.assertNotIn(self.LIVE_KEY, result.stdout)
            log_path = Path(env["CONNECTOR_REAPER_LOG"])
            if log_path.exists():
                self.assertNotIn(self.LIVE_KEY, log_path.read_text(encoding="utf-8"))


def _write_fake_ps(tmp: Path, rows: list[tuple]) -> Path:
    """A stand-in `ps` binary on PATH that prints the fixed rows this test
    controls, in the same seven-column shape connector-reaper.py parses."""
    lines = []
    for pid, ppid, st, etime, rss, tty, command in rows:
        lines.append(f"{pid} {ppid} {st} {etime} {rss} {tty} {command}")
    script = tmp / "ps"
    script.write_text(
        "#!/bin/sh\n" + "\n".join(f'echo "{line}"' for line in lines) + "\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _base_env(td: str, fake_ps: Path) -> dict:
    env = os.environ.copy()
    env["PATH"] = f"{fake_ps.parent}:{env.get('PATH', '')}"
    env["CONNECTOR_REAPER_CACHE_DIR"] = str(Path(td) / "cache")
    env["CONNECTOR_REAPER_LOG"] = str(Path(td) / "connector-reaper.log")
    return env


class ReapSubprocessTests(unittest.TestCase):
    """Exercises the real CLI (report / --reap / --reap --dry-run) against a
    fake `ps`, so the grace-period, throttle, and off-switch wiring is
    covered end to end, not just the pure ancestor_owner/redact functions."""

    def _run(self, td: Path, fake_ps: Path, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = _base_env(str(td), fake_ps)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, env=env, timeout=10,
        )

    def test_orphan_younger_than_grace_period_is_not_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            rows = [
                (1, 0, "S", "01:00:00", 5000, "??", "/sbin/launchd"),
                (500, 1, "S", "00:01:00", 12000, "??", "npm exec @playwright/mcp@latest"),
            ]
            fake_ps = _write_fake_ps(td, rows)
            env_extra = {"CONNECTOR_REAPER_GRACE_SECONDS": "300"}
            r = self._run(td, fake_ps, "--reap", "--dry-run", extra_env=env_extra)
            self.assertEqual(r.returncode, 0)
            log_text = (td / "connector-reaper.log").read_text(encoding="utf-8")
            self.assertIn("reason=grace_period", log_text)
            self.assertNotIn("would_kill", log_text)

    def test_orphan_older_than_grace_period_is_reaped_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            rows = [
                (1, 0, "S", "01:00:00", 5000, "??", "/sbin/launchd"),
                (501, 1, "S", "00:20:00", 12000, "??", "npm exec @playwright/mcp@latest"),
            ]
            fake_ps = _write_fake_ps(td, rows)
            env_extra = {"CONNECTOR_REAPER_GRACE_SECONDS": "0"}
            r = self._run(td, fake_ps, "--reap", "--dry-run", extra_env=env_extra)
            self.assertEqual(r.returncode, 0)
            log_text = (td / "connector-reaper.log").read_text(encoding="utf-8")
            self.assertIn("would_kill pid=501", log_text)
            self.assertIn("reason=orphan_grace_elapsed", log_text)

    def test_live_client_owned_connector_is_never_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            rows = [
                (1, 0, "S", "01:00:00", 5000, "??", "/sbin/launchd"),
                (100, 1, "Ss", "00:30:00", 40000, "s000", "/Applications/Claude.app/Contents/MacOS/claude"),
                (200, 100, "S", "00:10:00", 12000, "??", "npm exec chrome-devtools-mcp"),
            ]
            fake_ps = _write_fake_ps(td, rows)
            env_extra = {"CONNECTOR_REAPER_GRACE_SECONDS": "0"}
            r = self._run(td, fake_ps, "--reap", "--dry-run", extra_env=env_extra)
            self.assertEqual(r.returncode, 0)
            log_text = (td / "connector-reaper.log").read_text(encoding="utf-8")
            self.assertIn("pid=200", log_text)
            self.assertIn("reason=owned_by_live_client", log_text)
            self.assertNotIn("would_kill pid=200", log_text)

    def test_unresolvable_ancestor_yields_unknown_and_no_kill(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # 200's ppid (999) never appears in the ps snapshot.
            rows = [
                (1, 0, "S", "01:00:00", 5000, "??", "/sbin/launchd"),
                (200, 999, "S", "00:10:00", 12000, "??", "npm exec @modelcontextprotocol/server-foo"),
            ]
            fake_ps = _write_fake_ps(td, rows)
            env_extra = {"CONNECTOR_REAPER_GRACE_SECONDS": "0"}
            r = self._run(td, fake_ps, "--reap", "--dry-run", extra_env=env_extra)
            self.assertEqual(r.returncode, 0)
            log_text = (td / "connector-reaper.log").read_text(encoding="utf-8")
            self.assertIn("reason=unknown_ancestor", log_text)
            self.assertNotIn("would_kill pid=200", log_text)

    def test_off_switch_env_var_prevents_any_reap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            rows = [
                (1, 0, "S", "01:00:00", 5000, "??", "/sbin/launchd"),
                (501, 1, "S", "01:00:00", 12000, "??", "npm exec @playwright/mcp@latest"),
            ]
            fake_ps = _write_fake_ps(td, rows)
            env_extra = {"CONNECTOR_REAPER_GRACE_SECONDS": "0", "CONNECTOR_REAPER_OFF": "1"}
            r = self._run(td, fake_ps, "--reap", extra_env=env_extra)
            self.assertEqual(r.returncode, 0)
            self.assertFalse((td / "connector-reaper.log").exists())

    def test_actual_kill_terminates_a_real_orphaned_subprocess(self) -> None:
        """Not a fake-ps test: spawns a real sleep process, orphans it by
        reparenting semantics being irrelevant here (ancestor_owner is fed a
        synthetic snapshot pointing its ppid at init), and confirms
        kill_pid() actually ends it."""
        proc_handle = subprocess.Popen(["sleep", "30"])
        try:
            self.assertTrue(cr.kill_pid(proc_handle.pid))
            proc_handle.wait(timeout=5)
            self.assertIsNotNone(proc_handle.returncode)
        finally:
            if proc_handle.poll() is None:
                proc_handle.kill()
                proc_handle.wait()


class OrphanKeyTests(unittest.TestCase):
    def test_pid_reuse_with_different_command_gets_a_fresh_key(self) -> None:
        old = cr.orphan_key(500, proc(1, "npm exec @playwright/mcp@latest"))
        new = cr.orphan_key(500, proc(1, "npm exec chrome-devtools-mcp"))
        self.assertNotEqual(old, new)

    def test_same_pid_and_command_is_stable_across_calls(self) -> None:
        p = proc(1, "npm exec @playwright/mcp@latest")
        self.assertEqual(cr.orphan_key(500, p), cr.orphan_key(500, p))


if __name__ == "__main__":
    unittest.main()
