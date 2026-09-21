#!/usr/bin/env python3
"""Fixture coverage for session-reaper.py.

Three shapes, matching the two sibling suites:

  - pure functions fed synthetic process snapshots (test_connector_reaper.py's
    model), so no real process is ever signalled;
  - a real throwaway git repo with real linked worktrees and real session-lock
    files (test_worktree_reaper.py's model), with the process and docker layers
    stubbed;
  - a handful of real CLI runs against fake `ps` / `lsof` / `docker` binaries on
    PATH and a real loopback HTTP server standing in for CDP, so the throttle,
    off switch, and stdin bound are covered end to end.

Nothing here touches the real agent browser's port, the shared browse-tool profile, docker, or any
process this suite did not itself spawn.

EVERY subprocess.run gets BOTH `stdin=` and `timeout=`. Not decoration: the
reaper reads its hook payload from stdin, so a child without `stdin=` inherits
the RUNNER's, and whether the suite hangs then depends on nothing but whether
that stdin happens to be at EOF. See commit 56087f7 and worktree-reaper.py's
read_payload() docstring.
"""

from __future__ import annotations

import faulthandler
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).with_name("session-reaper.py")

_spec = importlib.util.spec_from_file_location("session_reaper", SCRIPT)
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)  # type: ignore[union-attr]

MODULE_TIMEOUT_SECONDS = 180
CHILD_TIMEOUT_SECONDS = 60


def setUpModule() -> None:
    faulthandler.dump_traceback_later(MODULE_TIMEOUT_SECONDS, exit=True)


def tearDownModule() -> None:
    faulthandler.cancel_dump_traceback_later()


def proc(ppid: int, command: str, etime: float = 7200.0, rss_kb: int = 1000) -> dict:
    return {"ppid": ppid, "etime": etime, "rss_kb": rss_kb, "command": command}


# --- pure functions ---------------------------------------------------------


class DevServerMatchTests(unittest.TestCase):
    def test_node_launched_vite_dev_matches(self) -> None:
        self.assertTrue(
            sr.looks_like_dev_server("node /repo/node_modules/.bin/vite dev --port 5198")
        )

    def test_npm_exec_wrapper_matches(self) -> None:
        self.assertTrue(sr.looks_like_dev_server("npm exec vite dev --port 5213"))

    def test_each_named_framework_matches(self) -> None:
        for cmd in (
            "node /r/node_modules/.bin/next dev",
            "npx astro dev --port 4321",
            "node /r/node_modules/.bin/wrangler dev",
        ):
            self.assertTrue(sr.looks_like_dev_server(cmd), cmd)

    def test_a_shell_wrapper_mentioning_a_dev_server_is_not_one(self) -> None:
        """The measured shape: a `zsh -c` carrying the command as a string.

        Matching the substring alone would SIGTERM the shell — and, in the
        observed tree, a shell holding session environment exports."""
        self.assertFalse(
            sr.looks_like_dev_server(
                "/bin/zsh -c source /x/snapshot.sh && npx vite dev --port 5213"
            )
        )

    def test_a_grep_looking_for_one_is_not_one(self) -> None:
        self.assertFalse(sr.looks_like_dev_server("grep -E vite dev"))

    def test_a_build_is_not_a_dev_server(self) -> None:
        self.assertFalse(sr.looks_like_dev_server("node /r/node_modules/.bin/vite build"))


class ClientScanTests(unittest.TestCase):
    """The measured false positive: a BRANCH NAME containing "codex"."""

    CMD = (
        "node /Users/n/dev/rally-hq/.worktrees/codex/bloom-application-journey"
        "/node_modules/.bin/vite dev --host 127.0.0.1 --port 5198"
    )
    REPO = ("/Users/n/dev/rally-hq/",)

    def test_repo_path_containing_codex_is_not_read_as_a_client(self) -> None:
        self.assertTrue(sr._is_client(self.CMD))          # the bug
        self.assertFalse(sr._is_client(self.CMD, self.REPO))  # the fix

    def test_a_real_client_outside_the_repo_still_matches(self) -> None:
        self.assertTrue(
            sr._is_client("/Applications/Claude.app/Contents/MacOS/Claude", self.REPO)
        )

    def test_stripping_is_confined_to_path_tokens(self) -> None:
        self.assertIn("--port", sr._client_text(self.CMD, self.REPO))

    def test_an_agent_state_path_after_the_executable_is_not_an_owner(self) -> None:
        """The Bash-tool shell wrapper outlives its session and carries a
        transcript path under ~/.claude. Left matching, it would exempt every
        dev server beneath it forever."""
        wrapper = (
            "/bin/zsh -c source /Users/n/.claude/shell-snapshots/snap.sh "
            "&& export CODEX_COMPANION_TRANSCRIPT_PATH=/Users/n/.claude/projects/x.jsonl "
            "&& npx vite dev --port 5213"
        )
        prefixes = ("/Users/n/.claude", "/Users/n/.codex")
        self.assertTrue(sr._is_client(wrapper))              # the bug
        self.assertFalse(sr._is_client(wrapper, prefixes))   # the fix

    def test_an_agent_binary_living_in_a_state_dir_is_still_a_client(self) -> None:
        """`~/.codex/computer-use/Codex Computer Use.app/…` is running on this
        machine. Stripping the EXECUTABLE would hide a live client — the one
        direction that destroys work — so token 0 is never stripped."""
        exe = "/Users/n/.codex/computer-use/Codex.app/Contents/MacOS/Codex --flag"
        self.assertTrue(sr._is_client(exe, ("/Users/n/.claude", "/Users/n/.codex")))

    def test_a_node_launched_claude_cli_is_still_a_client(self) -> None:
        """The shape that rules out "test the executable only" (`ps -o comm=`):
        the CLI's comm is the node binary, and only argv[1] says claude."""
        cmd = (
            "node /Users/n/.nvm/versions/node/v22/lib/node_modules"
            "/@anthropic-ai/claude-code/cli.js"
        )
        self.assertTrue(sr._is_client(cmd, ("/Users/n/.claude", "/Users/n/.codex")))

    def test_a_flag_with_an_equals_sign_is_not_mistaken_for_an_env_assignment(self) -> None:
        self.assertIn("--port=5198", sr._client_text("node x --port=5198", ("/nope",)))

    def test_a_macos_agent_path_with_spaces_survives(self) -> None:
        """Why this is not "test argv[0] only": the whitespace split puts the
        identifying part of `~/Library/Application Support/Claude/…` in a later
        token, so an argv[0]-only rule would miss the real Claude Code."""
        cmd = (
            "/Users/n/Library/Application Support/Claude/claude-code/2.1.266"
            "/claude.app/Contents/MacOS/claude"
        )
        self.assertTrue(sr._is_client(cmd, ("/Users/n/.claude", "/Users/n/.codex")))


class AncestorOwnerTests(unittest.TestCase):
    def test_chain_through_a_live_client_is_owned(self) -> None:
        procs = {
            1: proc(0, "/sbin/launchd"),
            50: proc(1, "/Applications/Claude.app/Contents/MacOS/Claude"),
            60: proc(50, "/bin/zsh -c npx vite dev --port 5213"),
            70: proc(60, "node /r/node_modules/.bin/vite dev --port 5213"),
        }
        self.assertEqual(sr.ancestor_owner(70, procs), "live_client")

    def test_chain_reaching_init_is_an_orphan(self) -> None:
        procs = {
            1: proc(0, "/sbin/launchd"),
            30: proc(1, "npm exec vite dev --port 5198"),
            31: proc(30, "node /r/node_modules/.bin/vite dev --port 5198"),
        }
        self.assertEqual(sr.ancestor_owner(31, procs), "orphan")

    def test_missing_ancestor_is_unknown_not_orphan(self) -> None:
        self.assertEqual(sr.ancestor_owner(31, {31: proc(999, "node vite dev")}), "unknown")

    def test_cycle_is_unknown_not_orphan(self) -> None:
        procs = {10: proc(20, "node vite dev"), 20: proc(10, "other")}
        self.assertEqual(sr.ancestor_owner(10, procs), "unknown")


class ParsingTests(unittest.TestCase):
    def test_etime_forms(self) -> None:
        self.assertEqual(sr.parse_etime("30"), 30.0)
        self.assertEqual(sr.parse_etime("05:55"), 355.0)
        self.assertEqual(sr.parse_etime("20:03:55"), 72235.0)
        self.assertEqual(sr.parse_etime("4-01:00:00"), 4 * 86400 + 3600.0)

    def test_local_port_extraction(self) -> None:
        self.assertEqual(sr.local_port("http://localhost:5226/login?x=1"), 5226)
        self.assertEqual(sr.local_port("http://127.0.0.1:5230/"), 5230)
        self.assertEqual(sr.local_port("https://localhost/"), 443)
        self.assertIsNone(sr.local_port("https://rallyhq.app/"))
        self.assertIsNone(sr.local_port("about:blank"))
        self.assertIsNone(sr.local_port("chrome-extension://abc/background.js"))

    def test_docker_timestamps(self) -> None:
        self.assertIsNotNone(sr.parse_docker_time("2026-09-11T13:56:48.292255717Z"))
        self.assertIsNone(sr.parse_docker_time("0001-01-01T00:00:00Z"))
        self.assertIsNone(sr.parse_docker_time(""))

    def test_credentials_never_reach_a_log_line(self) -> None:
        secret = "sk-ant-0000deadbeef0000"  # pragma: allowlist secret (fabricated)
        out = sr.safe_cmd(f"node server.js --token {secret}")
        self.assertNotIn(secret, out)


class PortProbeTests(unittest.TestCase):
    def test_a_listening_port_reads_listening(self) -> None:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            self.assertTrue(sr.port_listening("127.0.0.1", s.getsockname()[1]))
        finally:
            s.close()

    def test_a_closed_port_reads_not_listening(self) -> None:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertIs(sr.port_listening("127.0.0.1", port), False)


# --- repo fixture: dev servers and stacks -----------------------------------


class _RepoFixture(unittest.TestCase):
    """A real git repo with real linked worktrees and real session locks."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.log = Path(self.tmp.name) / "session-reaper.log"
        self.git("init", "--initial-branch=main", str(self.root))
        self.git("config", "user.name", "Session Test", cwd=self.root)
        self.git("config", "user.email", "session@example.invalid", cwd=self.root)
        (self.root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=self.root)
        self.git("commit", "-m", "base", cwd=self.root)
        self.common = Path(
            self.git("rev-parse", "--absolute-git-dir", cwd=self.root)
        )
        self.locks = self.common / sr.LOCK_DIRNAME
        self.addCleanup(self._restore)
        self._saved = (sr.snapshot_processes, sr.process_cwds, sr.docker, sr.LOG_PATH)
        sr.LOG_PATH = self.log

    def _restore(self) -> None:
        sr.snapshot_processes, sr.process_cwds, sr.docker, sr.LOG_PATH = self._saved

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_SECONDS,
        ).stdout.strip()

    def add_worktree(self, name: str) -> Path:
        wt = self.root / ".worktrees" / name
        wt.parent.mkdir(exist_ok=True)
        self.git("worktree", "add", "-b", name, str(wt), "main", cwd=self.root)
        return Path(os.path.realpath(wt))

    def add_lock(self, cwd: Path) -> None:
        self.locks.mkdir(parents=True, exist_ok=True)
        (self.locks / f"{cwd.name}.json").write_text(
            json.dumps({"cwd": str(cwd)}), encoding="utf-8"
        )

    def set_project_id(self, where: Path, project: str) -> None:
        (where / "supabase").mkdir(parents=True, exist_ok=True)
        (where / "supabase" / "config.toml").write_text(
            f'project_id = "{project}"\n', encoding="utf-8"
        )

    def stub_processes(self, procs: dict[int, dict], cwds: dict[int, str]) -> None:
        sr.snapshot_processes = lambda: procs
        sr.process_cwds = lambda pids: {p: cwds[p] for p in pids if p in cwds}

    def plan_dev(self) -> tuple[list[dict], list[dict]]:
        return sr.plan_dev_servers(str(self.root), str(self.common))

    def plan_stacks(self, sweep: bool = True) -> tuple[list[dict], list[dict]]:
        """Defaults to sweep: most of these tests exercise a gate OTHER than
        scope, and hook mode's declaration requirement would mask it. The two
        scope tests pass sweep=False explicitly."""
        return sr.plan_stacks(str(self.root), str(self.common), sweep=sweep)


class DevServerPlanTests(_RepoFixture):
    ORPHAN_TREE = {1: proc(0, "/sbin/launchd")}

    def _orphan_server(self, wt: Path, age: float = 7200.0) -> dict:
        procs = dict(self.ORPHAN_TREE)
        procs[300] = proc(1, f"node {wt}/node_modules/.bin/vite dev --port 5198", age)
        return procs

    def test_orphaned_lockless_worktree_server_is_planned_for_kill(self) -> None:
        wt = self.add_worktree("stale")
        self.stub_processes(self._orphan_server(wt), {300: str(wt)})
        kill, _keep = self.plan_dev()
        self.assertEqual([r["pid"] for r in kill], [300])

    def test_a_live_session_lock_on_that_worktree_keeps_it(self) -> None:
        wt = self.add_worktree("busy")
        self.add_lock(wt)
        self.stub_processes(self._orphan_server(wt), {300: str(wt)})
        kill, keep = self.plan_dev()
        self.assertEqual(kill, [])
        self.assertEqual(keep[0]["reason"], "live_session_lock")

    def test_a_live_ancestor_keeps_a_server_whose_worktree_has_no_lock(self) -> None:
        """The correction to the brief, measured on this machine.

        A session in worktree A starts a dev server in worktree B. B has no
        lock, so the specified gate (linked worktree + no live lock + >1h)
        SIGTERMs a server whose owner is alive. Only the ancestor walk sees it.
        """
        owner = self.add_worktree("owner")
        served = self.add_worktree("served")
        self.add_lock(owner)
        procs = {
            1: proc(0, "/sbin/launchd"),
            88: proc(1, "/Applications/Claude.app/Contents/MacOS/Claude"),
            300: proc(88, f"node {served}/node_modules/.bin/vite dev --port 5213"),
        }
        self.stub_processes(procs, {300: str(served)})
        kill, keep = self.plan_dev()
        self.assertEqual(kill, [])
        self.assertEqual(keep[0]["reason"], "ancestor_live_client")

    def test_the_main_checkout_is_never_killed(self) -> None:
        procs = dict(self.ORPHAN_TREE)
        procs[300] = proc(1, f"node {self.root}/node_modules/.bin/vite dev --port 5173")
        self.stub_processes(procs, {300: str(os.path.realpath(self.root))})
        kill, keep = self.plan_dev()
        self.assertEqual(kill, [])
        self.assertEqual(keep[0]["reason"], "not_in_a_linked_worktree")

    def test_a_server_younger_than_the_idle_gate_is_kept(self) -> None:
        wt = self.add_worktree("fresh")
        self.stub_processes(self._orphan_server(wt, age=60.0), {300: str(wt)})
        kill, keep = self.plan_dev()
        self.assertEqual(kill, [])
        self.assertIn("younger_than", keep[0]["reason"])

    def test_an_unresolvable_cwd_is_kept(self) -> None:
        wt = self.add_worktree("opaque")
        self.stub_processes(self._orphan_server(wt), {})  # lsof said nothing
        kill, keep = self.plan_dev()
        self.assertEqual(kill, [])
        self.assertEqual(keep[0]["reason"], "cwd_unresolved")

    def test_an_unreadable_session_lock_skips_the_whole_action(self) -> None:
        wt = self.add_worktree("stale")
        self.locks.mkdir(parents=True, exist_ok=True)
        (self.locks / "corrupt.json").write_text("{not json", encoding="utf-8")
        self.stub_processes(self._orphan_server(wt), {300: str(wt)})
        kill, keep = self.plan_dev()
        self.assertEqual(kill, [])
        self.assertEqual(keep[0]["reason"], "session_locks_unreadable")

    def test_a_stale_lock_does_not_protect_a_worktree(self) -> None:
        wt = self.add_worktree("abandoned")
        self.add_lock(wt)
        old = time.time() - sr.STALE_SECONDS - 60
        os.utime(self.locks / f"{wt.name}.json", (old, old))
        self.stub_processes(self._orphan_server(wt), {300: str(wt)})
        kill, _keep = self.plan_dev()
        self.assertEqual([r["pid"] for r in kill], [300])

    def test_ps_failure_skips_the_whole_action(self) -> None:
        self.add_worktree("stale")
        sr.snapshot_processes = lambda: None
        kill, keep = self.plan_dev()
        self.assertEqual(kill, [])
        self.assertEqual(keep[0]["reason"], "ps_enumeration_failed")


class StackPlanTests(_RepoFixture):
    DAY = 86400

    def stub_docker(self, names: list[str], started: dict[str, float],
                    record: list | None = None) -> None:
        def fake(args: list[str], timeout: float, clip: bool = True):
            if args[0] == "ps":
                return "\n".join(names) + "\n"
            if args[0] == "inspect":
                out = []
                for n in args[2:]:
                    if n in started:
                        stamp = time.strftime(
                            "%Y-%m-%dT%H:%M:%S", time.gmtime(started[n])
                        ) + ".0Z"
                        out.append(f"/{n}|{stamp}|{stamp}")
                return "\n".join(out) + "\n"
            if args[0] == "stop":
                if record is not None:
                    record.append(list(args))
                return "stopped\n"
            return ""
        sr.docker = fake

    def _stack(self, project: str) -> list[str]:
        return [f"supabase_{svc}_{project}" for svc in ("db", "kong", "analytics")]

    def test_unclaimed_stack_running_over_the_gate_is_planned_for_stop(self) -> None:
        self.set_project_id(self.root, "main-proj")
        names = self._stack("orphan-proj")
        self.stub_docker(names, {"supabase_db_orphan-proj": time.time() - 4 * self.DAY})
        stop, _keep = self.plan_stacks()
        self.assertEqual([r["project"] for r in stop], ["orphan-proj"])
        self.assertCountEqual(stop[0]["containers"], names)

    def test_the_main_checkouts_project_is_exempt(self) -> None:
        self.set_project_id(self.root, "main-proj")
        self.stub_docker(
            self._stack("main-proj"),
            {"supabase_db_main-proj": time.time() - 4 * self.DAY},
        )
        stop, keep = self.plan_stacks()
        self.assertEqual(stop, [])
        self.assertEqual(keep[0]["reason"], "claimed_by_live_session_or_main")

    def test_a_project_claimed_by_a_live_session_worktree_is_kept(self) -> None:
        self.set_project_id(self.root, "main-proj")
        wt = self.add_worktree("busy")
        self.set_project_id(wt, "wave-proj")
        self.add_lock(wt)
        self.stub_docker(
            self._stack("wave-proj"),
            {"supabase_db_wave-proj": time.time() - 4 * self.DAY},
        )
        stop, keep = self.plan_stacks()
        self.assertEqual(stop, [])
        self.assertEqual(keep[0]["reason"], "claimed_by_live_session_or_main")

    def test_the_same_project_is_reaped_once_its_worktree_has_no_live_session(self) -> None:
        self.set_project_id(self.root, "main-proj")
        wt = self.add_worktree("idle")
        self.set_project_id(wt, "wave-proj")  # no lock written
        self.stub_docker(
            self._stack("wave-proj"),
            {"supabase_db_wave-proj": time.time() - 4 * self.DAY},
        )
        # sweep=False: hook mode, and the worktree DECLARES wave-proj, so this
        # is the one stack shape the unattended hook is entitled to stop.
        stop, _keep = self.plan_stacks(sweep=False)
        self.assertEqual([r["project"] for r in stop], ["wave-proj"])
        self.assertTrue(wt.exists())

    def test_a_stack_started_under_the_gate_is_kept(self) -> None:
        self.set_project_id(self.root, "main-proj")
        self.stub_docker(
            self._stack("fresh-proj"),
            {"supabase_db_fresh-proj": time.time() - 3600},
        )
        stop, keep = self.plan_stacks()
        self.assertEqual(stop, [])
        self.assertIn("running_less_than", keep[0]["reason"])

    def test_an_unknown_uptime_is_kept(self) -> None:
        self.set_project_id(self.root, "main-proj")
        self.stub_docker(self._stack("mystery-proj"), {})
        stop, keep = self.plan_stacks()
        self.assertEqual(stop, [])
        self.assertEqual(keep[0]["reason"], "uptime_unknown")

    def test_docker_unavailable_skips_the_whole_action(self) -> None:
        sr.docker = lambda args, timeout, clip=True: None
        stop, keep = self.plan_stacks()
        self.assertEqual(stop, [])
        self.assertEqual(keep[0]["reason"], "docker_unavailable")

    def test_a_repo_with_no_supabase_never_stops_another_repos_stack(self) -> None:
        """The blocking scope bug. `docker ps` is machine-wide while the locks
        and worktrees are this repo's, so an unqualified rule let a SessionEnd
        in blog or dotfiles stop rally-hq's database."""
        # This fixture repo declares no project_id anywhere.
        self.stub_docker(
            self._stack("someone-elses-project"),
            {"supabase_db_someone-elses-project": time.time() - 4 * self.DAY},
        )
        stop, keep = self.plan_stacks(sweep=False)
        self.assertEqual(stop, [])
        self.assertEqual(keep[0]["reason"], "not_declared_by_this_repo")

    def test_reap_now_sweeps_what_the_hook_leaves_alone(self) -> None:
        """The operator-present path reaches a stack whose creating worktree has
        since been reset — measured, `rally-bloom-journey-20260910` is in no
        config.toml on disk."""
        self.stub_docker(
            self._stack("orphaned-by-a-reset"),
            {"supabase_db_orphaned-by-a-reset": time.time() - 4 * self.DAY},
        )
        stop, _keep = sr.plan_stacks(str(self.root), str(self.common), sweep=True)
        self.assertEqual([r["project"] for r in stop], ["orphaned-by-a-reset"])

    def test_sweep_still_never_touches_a_claimed_stack(self) -> None:
        self.set_project_id(self.root, "main-proj")
        self.stub_docker(
            self._stack("main-proj"),
            {"supabase_db_main-proj": time.time() - 4 * self.DAY},
        )
        stop, keep = sr.plan_stacks(str(self.root), str(self.common), sweep=True)
        self.assertEqual(stop, [])
        self.assertEqual(keep[0]["reason"], "claimed_by_live_session_or_main")

    def test_a_short_project_id_does_not_steal_a_longer_ones_containers(self) -> None:
        """"hq" is a suffix of "supabase_db_rally-hq". Longest project wins."""
        self.set_project_id(self.root, "main-proj")
        names = self._stack("rally-hq") + self._stack("hq")
        old = time.time() - 4 * self.DAY
        self.stub_docker(
            names, {"supabase_db_rally-hq": old, "supabase_db_hq": old}
        )
        stop, _keep = self.plan_stacks()
        by_project = {r["project"]: r["containers"] for r in stop}
        self.assertCountEqual(by_project["rally-hq"], self._stack("rally-hq"))
        self.assertCountEqual(by_project["hq"], self._stack("hq"))

    def test_stop_uses_docker_stop_with_a_short_grace_and_never_rm(self) -> None:
        record: list = []
        self.stub_docker([], {}, record=record)
        self.assertTrue(sr.stop_stack(["supabase_db_x", "supabase_kong_x"]))
        self.assertEqual(record[0][0], "stop")
        self.assertIn("-t", record[0])
        self.assertNotIn("rm", record[0])
        self.assertNotIn("-v", record[0])
        self.assertIn("supabase_kong_x", record[0])


# --- CDP tabs ----------------------------------------------------------------


class _FakeCDP:
    """A loopback stand-in for the browser's DevTools endpoint."""

    def __init__(self, targets: list[dict]) -> None:
        self.targets = targets
        self.closed: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/json":
                    body = json.dumps(outer.targets).encode()
                elif self.path.startswith("/json/close/"):
                    outer.closed.append(self.path.rsplit("/", 1)[-1])
                    body = b"Target is closing"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class TabPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_url = sr.CDP_URL
        self.addCleanup(lambda: setattr(sr, "CDP_URL", self._saved_url))

    def _serve(self, targets: list[dict]) -> _FakeCDP:
        cdp = _FakeCDP(targets)
        self.addCleanup(cdp.stop)
        sr.CDP_URL = cdp.url
        return cdp

    @staticmethod
    def _dead_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _live_port(self) -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def test_tabs_on_a_dead_local_port_are_planned_for_close(self) -> None:
        dead = self._dead_port()
        self._serve([
            {"id": "A", "type": "page", "url": f"http://localhost:{dead}/one"},
            {"id": "B", "type": "page", "url": f"http://127.0.0.1:{dead}/two"},
            {"id": "C", "type": "page", "url": "https://rallyhq.app/"},
        ])
        close, _keep = sr.plan_tabs()
        self.assertCountEqual([r["id"] for r in close], ["A", "B"])

    def test_a_tab_on_a_listening_port_is_kept(self) -> None:
        self._serve([
            {"id": "A", "type": "page", "url": f"http://127.0.0.1:{self._live_port()}/"},
            {"id": "B", "type": "page", "url": "about:blank"},
        ])
        close, keep = sr.plan_tabs()
        self.assertEqual(close, [])
        self.assertIn("port_listening", [r["reason"] for r in keep])

    def test_non_page_targets_are_never_touched(self) -> None:
        dead = self._dead_port()
        self._serve([
            {"id": "SW", "type": "service_worker", "url": f"http://localhost:{dead}/sw"},
            {"id": "UI", "type": "browser_ui", "url": "chrome://omnibox-popup.top-chrome/"},
            {"id": "P", "type": "page", "url": "about:blank"},
        ])
        close, _keep = sr.plan_tabs()
        self.assertEqual(close, [])

    def test_the_last_remaining_page_is_never_closed(self) -> None:
        """Chrome exits with its last tab, and ending the browser is the one
        thing this action must never do — the profile holds real logins."""
        dead = self._dead_port()
        self._serve([
            {"id": "A", "type": "page", "url": f"http://localhost:{dead}/one"},
        ])
        close, keep = sr.plan_tabs()
        self.assertEqual(close, [])
        self.assertEqual(keep[0]["reason"], "last_remaining_page")

    def test_one_of_two_dead_tabs_still_closes(self) -> None:
        dead = self._dead_port()
        self._serve([
            {"id": "A", "type": "page", "url": f"http://localhost:{dead}/one"},
            {"id": "B", "type": "page", "url": f"http://localhost:{dead}/two"},
        ])
        close, _keep = sr.plan_tabs()
        self.assertEqual(len(close), 1)

    def test_closing_actually_hits_the_cdp_close_endpoint(self) -> None:
        dead = self._dead_port()
        cdp = self._serve([
            {"id": "A", "type": "page", "url": f"http://localhost:{dead}/one"},
            {"id": "B", "type": "page", "url": "about:blank"},
        ])
        close, _keep = sr.plan_tabs()
        self.assertTrue(sr.close_tab(str(close[0]["id"])))
        self.assertEqual(cdp.closed, ["A"])

    def test_a_silent_cdp_endpoint_skips_the_action(self) -> None:
        sr.CDP_URL = f"http://127.0.0.1:{self._dead_port()}"
        close, keep = sr.plan_tabs()
        self.assertEqual(close, [])
        self.assertEqual(keep[0]["reason"], "cdp_not_answering")


class SharedBrowserPortTests(unittest.TestCase):
    """The browser's port is read from browse-tool's state files, never recalled.

    2026-09-21: the constant said 9339, the browser was on 9222, and the tab
    action skipped as cdp_not_answering with nothing to flag it.
    """

    def _state(self, d: str, port: int, profile: str, started: int) -> None:
        with open(os.path.join(d, f"browse-tool-state-{port}.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": 1, "port": port, "profileName": profile, "started": started}, fh)

    def test_reads_the_shared_profiles_port(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._state(d, 9223, "other", 300)
            self._state(d, 9444, "shared", 100)
            self.assertEqual(sr.shared_browser_port(d), 9444)

    def test_newest_shared_record_wins(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._state(d, 9339, "shared", 100)
            self._state(d, 9222, "shared", 200)
            self.assertEqual(sr.shared_browser_port(d), 9222)

    def test_no_record_or_a_corrupt_one_falls_back_to_browse_tools_default(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sr.shared_browser_port(d), sr.BROWSE_DEFAULT_PORT)
            with open(os.path.join(d, "browse-tool-state-9555.json"), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertEqual(sr.shared_browser_port(d), sr.BROWSE_DEFAULT_PORT)


class ActionSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("SESSION_REAPER_ACTIONS")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop("SESSION_REAPER_ACTIONS", None)
        else:
            os.environ["SESSION_REAPER_ACTIONS"] = self._saved

    def test_unset_means_every_action(self) -> None:
        os.environ.pop("SESSION_REAPER_ACTIONS", None)
        self.assertEqual(sr.selected_actions(), sr.ALL_ACTIONS)

    def test_empty_means_no_action_not_every_action(self) -> None:
        """`if not raw` collapsed these two, so the obvious way to write "do
        nothing" turned everything on."""
        os.environ["SESSION_REAPER_ACTIONS"] = ""
        self.assertEqual(sr.selected_actions(), ())

    def test_a_subset_selects_only_those(self) -> None:
        os.environ["SESSION_REAPER_ACTIONS"] = "tabs, stacks"
        self.assertEqual(sr.selected_actions(), ("tabs", "stacks"))


# --- real CLI: throttle, off switch, reap-now, stdin -------------------------


def _write_fake(tmp: Path, name: str, body: str) -> None:
    script = tmp / name
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class CliTests(_RepoFixture):
    """Real subprocess runs of the real CLI, with ps/lsof/docker faked on PATH."""

    def setUp(self) -> None:
        super().setUp()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.wt = self.add_worktree("stale")
        _write_fake(self.bin, "ps", (
            'echo "1 0 10:00:00 5000 /sbin/launchd"\n'
            f'echo "300 1 20:00:00 70000 node {self.wt}/node_modules/.bin/vite dev --port 5198"'
        ))
        _write_fake(self.bin, "lsof", f'echo "p300"; echo "fcwd"; echo "n{self.wt}"')
        _write_fake(self.bin, "docker", "exit 1")
        self.cdp = _FakeCDP([])
        self.addCleanup(self.cdp.stop)

    def run_cli(self, *args: str, extra: dict | None = None,
                stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["SESSION_REAPER_LOG"] = str(self.log)
        env["SESSION_REAPER_CDP_URL"] = self.cdp.url
        # `dev` only: the fake docker exits non-zero, which the stack action
        # correctly reads as "docker unavailable" — tested above, noise here.
        env["SESSION_REAPER_ACTIONS"] = "dev"
        if extra:
            env.update(extra)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.root, capture_output=True, text=True, env=env,
            stdin=stdin, timeout=CHILD_TIMEOUT_SECONDS,
        )

    def test_report_names_the_orphan_and_never_signals_it(self) -> None:
        r = self.run_cli("report", str(self.root))
        self.assertEqual(r.returncode, 0)
        self.assertIn("pid=300", r.stdout)
        self.assertIn("dev servers to SIGTERM (1)", r.stdout)
        self.assertFalse(self.log.exists(), "report must not write the reap log")

    def test_the_off_switch_prevents_any_pass(self) -> None:
        r = self.run_cli("reap", extra={"SESSION_REAPER_OFF": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_the_per_repo_guard_off_file_prevents_any_pass(self) -> None:
        self.locks.mkdir(parents=True, exist_ok=True)
        (self.locks / sr.OVERRIDE_FILENAME).touch()
        r = self.run_cli("reap")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_a_reap_stamps_the_repo_and_the_next_one_is_throttled(self) -> None:
        # SIGTERM to a pid this test does not own would be real. Point the fake
        # ps at a pid that cannot exist so the kill fails harmlessly, and assert
        # on the throttle, which is what this test is about.
        _write_fake(self.bin, "ps", 'echo "1 0 10:00:00 5000 /sbin/launchd"')
        self.run_cli("reap")
        stamp = self.locks / sr.STAMP_FILENAME
        self.assertTrue(stamp.exists())
        first = self.log.read_text(encoding="utf-8")
        self.run_cli("reap")
        self.assertEqual(self.log.read_text(encoding="utf-8"), first)

    def test_reap_now_ignores_the_throttle(self) -> None:
        _write_fake(self.bin, "ps", 'echo "1 0 10:00:00 5000 /sbin/launchd"')
        self.run_cli("reap")
        before = self.log.read_text(encoding="utf-8")
        self.run_cli("reap-now", str(self.root))
        after = self.log.read_text(encoding="utf-8")
        self.assertGreater(len(after), len(before))
        self.assertIn("reap-now", after)

    def isolate_in_process(self) -> None:
        """Point the two machine-global actions at nothing before calling
        cmd_reap() in-process.

        Without this the module defaults apply — sr.CDP_URL is the REAL browser
        and sr.docker shells out to the REAL daemon — so an in-process
        reap would act on the operator's live session. The gates happened to
        hold when this was first written (every stack was under the 24h gate and
        the only page target was about:blank), but a test that depends on the
        machine's state for its safety is not safe.
        """
        saved_url, saved_docker = sr.CDP_URL, sr.docker
        self.addCleanup(lambda: setattr(sr, "CDP_URL", saved_url))
        self.addCleanup(lambda: setattr(sr, "docker", saved_docker))
        sr.CDP_URL = self.cdp.url          # the fixture server, serving no targets
        sr.docker = lambda *a, **k: None   # reads as "docker unavailable"

    def test_a_session_that_starts_while_planning_saves_its_dev_server(self) -> None:
        """The plan->kill gap is the wide window, so the lock set is re-read.

        Simulated by writing the lock only after plan_dev_servers has returned,
        which is exactly what a session starting mid-pass looks like."""
        self.isolate_in_process()
        wt = self.wt
        procs = {
            1: proc(0, "/sbin/launchd"),
            300: proc(1, f"node {wt}/node_modules/.bin/vite dev --port 5198"),
        }
        planned = []

        def plan_then_lock(cwd: str, cd: str):
            result = ([{"pid": 300, "age_s": 72000.0, "rss_kb": 70000,
                        "command": "node vite dev", "worktree": str(wt)}], [])
            self.add_lock(wt)  # a session arrives after the plan
            planned.append(True)
            return result

        saved_plan, saved_kill = sr.plan_dev_servers, sr.kill_pid
        killed: list[int] = []
        sr.plan_dev_servers = plan_then_lock
        sr.kill_pid = lambda pid: killed.append(pid) or True
        sr.snapshot_processes = lambda: procs
        try:
            sr.cmd_reap({"cwd": str(self.root)})
        finally:
            sr.plan_dev_servers, sr.kill_pid = saved_plan, saved_kill
        self.assertTrue(planned)
        self.assertEqual(killed, [])
        self.assertIn(
            "reason=session_started_while_planning",
            self.log.read_text(encoding="utf-8"),
        )

    def test_a_failing_action_is_logged_and_never_fails_the_hook(self) -> None:
        self.isolate_in_process()
        saved = sr.plan_dev_servers

        def boom(cwd: str, cd: str):
            raise RuntimeError("synthetic")

        sr.plan_dev_servers = boom
        try:
            sr.cmd_reap({"cwd": str(self.root)})
        finally:
            sr.plan_dev_servers = saved
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("dev action failed: RuntimeError: synthetic", text)
        self.assertIn("dev_servers=0", text)  # the summary line still lands

    def test_an_open_stdin_that_never_closes_does_not_hang_the_hook(self) -> None:
        """The 56087f7 hang, from the other side: the reaper reads its payload
        from stdin, and an inherited pipe nobody closes must time out rather
        than park the process forever."""
        read_fd, write_fd = os.pipe()
        try:
            started = time.time()
            proc_handle = subprocess.Popen(
                [sys.executable, str(SCRIPT), "reap"],
                cwd=self.root, stdin=read_fd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # No actions at all: this test is about the stdin bound, and a
                # run that signals nothing cannot reach a pid it does not own.
                env={**os.environ, "SESSION_REAPER_LOG": str(self.log),
                     "SESSION_REAPER_ACTIONS": "", "PATH": f"{self.bin}:{os.environ['PATH']}"},
            )
            proc_handle.communicate(timeout=CHILD_TIMEOUT_SECONDS)
            self.assertEqual(proc_handle.returncode, 0)
            self.assertLess(time.time() - started, 30)
        finally:
            os.close(read_fd)
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
