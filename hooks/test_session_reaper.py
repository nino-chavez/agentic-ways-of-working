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


def phased(before, after):
    """A still_running stub: the first call is the pre-signal liveness read,
    every later call is the post-signal exit check."""
    calls = {"n": 0}

    def still_running(pids):
        calls["n"] += 1
        return (before if calls["n"] == 1 else after)(pids)

    return still_running


def ALL_ALIVE(pids):
    return set(pids)


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

    # Measured 2026-09-24 in apps/volley-watch: the argv wrangler ACTUALLY runs
    # as, none of which contained the old "wrangler dev" substring.
    WT = "/Users/nino/Workspace/dev/apps/volley-watch/.worktrees/codex/apple-tv-release-ready-20260921"
    WRANGLER_ROOT = (
        f"node {WT}/node_modules/wrangler/bin/wrangler.js pages dev "
        "/private/tmp/rotation-delivery-reviewed-20260922 --port 8773 --ip 127.0.0.1 "
        "--inspector-port 0 --binding ROTATION_DATA_EXPOSURE=controlled "
        "--persist-to /private/tmp/rotation-delivery-state-20260922 --log-level warn"
    )
    WRANGLER_CLI = (
        f"/opt/homebrew/Cellar/node/26.0.0/bin/node --no-warnings "
        f"{WT}/node_modules/wrangler/wrangler-dist/cli.js pages dev "
        "/private/tmp/rotation-delivery-reviewed-20260922 --port 8773 --ip 127.0.0.1"
    )
    WORKERD = (
        f"{WT}/node_modules/@cloudflare/workerd-darwin-arm64/bin/workerd serve "
        "--binary --experimental --socket-addr=entry=127.0.0.1:8773 "
        "--external-addr=loopback=127.0.0.1:55337 --control-fd=3 -"
    )

    def test_wranglers_real_argv_matches_at_every_level_of_its_tree(self) -> None:
        """pid 22907 -> 22910 -> 22916. The `.js` suffix broke the substring,
        the `pages` subcommand broke it again, and workerd's argv0 was outside
        the runtime set, so `report` listed none of the three."""
        self.assertEqual(sr.dev_server_kind(self.WRANGLER_ROOT), "wrangler pages dev")
        self.assertEqual(sr.dev_server_kind(self.WRANGLER_CLI), "wrangler pages dev")
        self.assertEqual(sr.dev_server_kind(self.WORKERD), "workerd serve")

    def test_a_bare_vite_is_the_dev_server_flags_or_not(self) -> None:
        for cmd in (
            "node /r/node_modules/.bin/vite",
            "node /r/node_modules/.bin/vite --port 5000 --host",
            "node /r/node_modules/vite/bin/vite.js",
        ):
            self.assertEqual(sr.dev_server_kind(cmd), "vite", cmd)

    def test_the_tools_real_entry_script_matches_not_only_its_bin_shim(self) -> None:
        self.assertEqual(
            sr.dev_server_kind("node /r/node_modules/vite/bin/vite.js dev"), "vite dev"
        )
        self.assertEqual(
            sr.dev_server_kind("node /r/node_modules/vinext/dist/cli.js dev"), "vinext dev"
        )

    def test_a_script_merely_inside_a_tool_package_is_not_a_bare_server(self) -> None:
        """Package-directory identification may not stand for a bare
        invocation, or every vite chunk and next worker would be a server."""
        for cmd in (
            "node /r/node_modules/vite/dist/node/chunks/worker.js",
            "node /r/node_modules/next/dist/compiled/jest-worker/processChild.js",
        ):
            self.assertIsNone(sr.dev_server_kind(cmd), cmd)

    def test_preview_and_production_servers_match(self) -> None:
        for cmd, kind in (
            ("node /r/node_modules/.bin/vite preview", "vite preview"),
            ("node /r/node_modules/.bin/next start", "next start"),
            ("node /r/node_modules/.bin/vinext start", "vinext start"),
            ("npx astro preview", "astro preview"),
            ("node /r/node_modules/.bin/wrangler dev --test-scheduled", "wrangler dev"),
        ):
            self.assertEqual(sr.dev_server_kind(cmd), kind, cmd)

    def test_pythons_http_server_matches_and_other_modules_do_not(self) -> None:
        """What ~/.local/bin/preview starts, with the argv0 macOS gives it."""
        self.assertEqual(
            sr.dev_server_kind(
                "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
                "Versions/3.14/Resources/Python.app/Contents/MacOS/Python "
                "-m http.server 8765 -d /r/.worktrees/chooser-answers"
            ),
            "http.server",
        )
        self.assertEqual(sr.dev_server_kind("python3 -m http.server"), "http.server")
        self.assertEqual(sr.dev_server_kind("python3.14 -m http.server 9000"), "http.server")
        self.assertIsNone(sr.dev_server_kind("python3 -m pytest tests"))
        self.assertIsNone(sr.dev_server_kind("python3 serve.py http.server"))

    def test_deploys_builds_and_other_subcommands_are_not_dev_servers(self) -> None:
        for cmd in (
            "node /r/node_modules/wrangler/bin/wrangler.js pages deploy ./dist",
            "node /r/node_modules/wrangler/bin/wrangler.js deploy",
            "node /r/node_modules/wrangler/bin/wrangler.js",
            "node /r/node_modules/.bin/next build",
            "node /r/node_modules/.bin/next",
            "node /r/node_modules/.bin/svelte-kit sync",
            "/r/node_modules/@cloudflare/workerd-darwin-arm64/bin/workerd compile x.capnp",
        ):
            self.assertIsNone(sr.dev_server_kind(cmd), cmd)


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

    def test_a_native_binary_run_from_a_codex_branch_worktree_is_not_a_client(self) -> None:
        """Measured 2026-09-24: workerd and esbuild under
        `.worktrees/codex/apple-tv-release-ready-20260921/node_modules/`. The
        executable IS the worktree path, so the branch name sat in token 0,
        which is never dropped — and the tree the fix was for came back as
        `descendant_live_client`. Judged by the remainder after the worktree
        instead: the executable's own name, not the branch it sits under."""
        wt = "/Users/n/dev/volley-watch/.worktrees/codex/apple-tv-release-ready-20260921"
        prefixes = (
            "/Users/n/dev/volley-watch", "/Users/n/dev/volley-watch/", wt, wt + "/",
            "/Users/n/.claude", "/Users/n/.codex",
        )
        workerd = f"{wt}/node_modules/@cloudflare/workerd-darwin-arm64/bin/workerd serve --binary"
        esbuild = f"{wt}/node_modules/@esbuild/darwin-arm64/bin/esbuild --service=0.25.0 --ping"
        self.assertTrue(sr._is_client(workerd))              # the bug
        self.assertFalse(sr._is_client(workerd, prefixes))   # the fix
        self.assertFalse(sr._is_client(esbuild, prefixes))
        # Longest prefix first — with only the repo stripped, the remainder
        # would still begin `.worktrees/codex/`.
        self.assertTrue(sr._client_text(workerd, prefixes).startswith("node_modules/"))

    def test_a_client_binary_inside_a_repo_worktree_is_still_a_client(self) -> None:
        """The remainder keeps the executable's own name: a repo-local
        `node_modules/.bin/claude` is not hidden by the rule above."""
        wt = "/Users/n/dev/app/.worktrees/codex/x"
        prefixes = ("/Users/n/dev/app", "/Users/n/dev/app/", wt, wt + "/")
        self.assertTrue(sr._is_client(f"{wt}/node_modules/.bin/claude --model x", prefixes))

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


class StillRunningTests(unittest.TestCase):
    """Against real children this test spawns and owns, never anyone else's."""

    def test_a_live_child_is_running_and_a_reaped_one_is_not(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
        )
        try:
            self.assertEqual(sr.still_running([child.pid]), {child.pid})
        finally:
            child.terminate()
            child.wait(timeout=CHILD_TIMEOUT_SECONDS)
        self.assertEqual(sr.still_running([child.pid]), set())

    def test_a_zombie_counts_as_exited(self) -> None:
        """Exited but not yet collected by its parent: what esbuild and the
        small workerd became under a wrangler that ignored SIGTERM."""
        child = subprocess.Popen(
            [sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
        )
        deadline = time.time() + 10
        while time.time() < deadline:  # exited, deliberately not wait()ed
            r = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child.pid)],
                capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_SECONDS,
            )
            if r.stdout.strip().upper().startswith("Z"):
                break
            time.sleep(0.05)
        try:
            self.assertEqual(sr.still_running([child.pid]), set())
        finally:
            child.wait(timeout=CHILD_TIMEOUT_SECONDS)

    def test_no_pids_needs_no_ps(self) -> None:
        self.assertEqual(sr.still_running([]), set())

    def _with_fake_ps(self, body: str) -> set[int] | None:
        with tempfile.TemporaryDirectory() as d:
            _write_fake(Path(d), "ps", body)
            saved = os.environ["PATH"]
            os.environ["PATH"] = f"{d}:{saved}"
            try:
                return sr.still_running([4242, 4243])
            finally:
                os.environ["PATH"] = saved

    def test_a_ps_that_fails_is_unverified_not_all_exited(self) -> None:
        """The commit reviewer's catch on da0297b: a ps that errors prints
        nothing to stdout, which parsed as "nobody running" and counted every
        signalled process as freed."""
        self.assertIsNone(
            self._with_fake_ps('echo "ps: Invalid process id: 4242" >&2; exit 1')
        )

    def test_every_pid_gone_is_exit_1_with_a_silent_stderr(self) -> None:
        """What macOS ps actually does when none of the pids exist."""
        self.assertEqual(self._with_fake_ps("exit 1"), set())

    def test_one_pid_gone_one_running(self) -> None:
        self.assertEqual(self._with_fake_ps('echo "4243 S"'), {4243})


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
        wt.parent.mkdir(parents=True, exist_ok=True)
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

    WRANGLER_WT = "codex/apple-tv-release-ready-20260921"

    def _wrangler_tree(self, wt: Path, age: float = 2 * 86400 + 14 * 3600) -> dict:
        """The 2026-09-24 volley-watch tree: wrangler (PPID 1) -> node cli.js ->
        two workerd runtimes (the older one at ~95% CPU and 888 MB) and an
        esbuild service. The worktree is a `codex/…` branch, which is what
        made the native binaries read as a client on the first dry run."""
        procs = dict(self.ORPHAN_TREE)
        procs[22907] = proc(
            1,
            f"node {wt}/node_modules/wrangler/bin/wrangler.js pages dev "
            "/private/tmp/out --port 8773 --ip 127.0.0.1",
            age, rss_kb=3408,
        )
        procs[22910] = proc(
            22907,
            f"/opt/homebrew/Cellar/node/26.0.0/bin/node --no-warnings "
            f"{wt}/node_modules/wrangler/wrangler-dist/cli.js pages dev "
            "/private/tmp/out --port 8773 --ip 127.0.0.1",
            age, rss_kb=1472,
        )
        procs[22916] = proc(
            22910,
            f"{wt}/node_modules/@cloudflare/workerd-darwin-arm64/bin/workerd serve "
            "--binary --experimental --socket-addr=entry=127.0.0.1:8773 --control-fd=3 -",
            age, rss_kb=909088,
        )
        procs[21860] = proc(
            22910,
            f"{wt}/node_modules/@cloudflare/workerd-darwin-arm64/bin/workerd serve "
            "--binary --socket-addr=entry=127.0.0.1:0",
            86400 + 13 * 3600, rss_kb=8320,
        )
        procs[22911] = proc(
            22910,
            f"{wt}/node_modules/@esbuild/darwin-arm64/bin/esbuild --service=0.25.0 --ping",
            age, rss_kb=3072,
        )
        return procs

    def test_an_orphaned_wrangler_tree_is_planned_as_one_root_with_its_descendants(self) -> None:
        """The incident: PPID 1, no lock, 2.5 days old — and `report` said
        `dev servers to SIGTERM (0)` and `keeping (0)`."""
        wt = self.add_worktree(self.WRANGLER_WT)
        procs = self._wrangler_tree(wt)
        self.stub_processes(procs, {p: str(wt) for p in procs if p != 1})
        kill, keep = self.plan_dev()
        self.assertEqual([r["pid"] for r in kill], [22907])
        self.assertEqual(keep, [], "descendants are part of the root's row, not rows")
        row = kill[0]
        self.assertEqual(row["kind"], "wrangler pages dev")
        self.assertEqual(row["worktree"], str(wt))
        self.assertEqual({s["pid"] for s in row["tree"]}, {22910, 22916, 21860, 22911})
        self.assertEqual(row["tree"][0]["pid"], 22910, "parents before children")
        self.assertEqual(row["tree_rss_kb"], 1472 + 909088 + 8320 + 3072)

    def test_a_kept_root_keeps_its_descendants_with_it_and_reports_them_once(self) -> None:
        wt = self.add_worktree(self.WRANGLER_WT)
        procs = self._wrangler_tree(wt, age=60.0)
        self.stub_processes(procs, {p: str(wt) for p in procs if p != 1})
        kill, keep = self.plan_dev()
        self.assertEqual(kill, [])
        self.assertEqual([r["pid"] for r in keep], [22907])
        self.assertIn("younger_than", keep[0]["reason"])
        self.assertEqual(len(keep[0]["tree"]), 4)

    def test_a_workerd_that_outlived_its_wrangler_is_its_own_root(self) -> None:
        wt = self.add_worktree(self.WRANGLER_WT)  # a `codex/` branch, on purpose
        procs = dict(self.ORPHAN_TREE)
        procs[22916] = proc(
            1,
            f"{wt}/node_modules/@cloudflare/workerd-darwin-arm64/bin/workerd serve "
            "--binary --socket-addr=entry=127.0.0.1:8773",
            rss_kb=909088,
        )
        self.stub_processes(procs, {22916: str(wt)})
        kill, keep = self.plan_dev()
        self.assertEqual([r["pid"] for r in kill], [22916])
        self.assertEqual(kill[0]["kind"], "workerd serve")
        self.assertEqual(kill[0]["tree"], [])

    def test_a_descendant_that_is_a_live_client_keeps_the_whole_tree(self) -> None:
        """No dev server spawns an agent — which is why this must fail closed:
        a subtree is killed on its root's evidence alone."""
        wt = self.add_worktree("odd")
        procs = self._orphan_server(wt)
        procs[301] = proc(300, "/Applications/Claude.app/Contents/MacOS/Claude")
        self.stub_processes(procs, {300: str(wt)})
        kill, keep = self.plan_dev()
        self.assertEqual(kill, [])
        self.assertEqual(keep[0]["reason"], "descendant_live_client")

    def test_the_preview_helpers_http_server_is_planned_for_kill(self) -> None:
        wt = self.add_worktree("chooser-answers")
        procs = dict(self.ORPHAN_TREE)
        procs[73882] = proc(
            1,
            "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
            "Versions/3.14/Resources/Python.app/Contents/MacOS/Python "
            f"-m http.server 8765 -d {wt}",
            5 * 3600,
        )
        self.stub_processes(procs, {73882: str(wt)})
        kill, _keep = self.plan_dev()
        self.assertEqual([r["pid"] for r in kill], [73882])
        self.assertEqual(kill[0]["kind"], "http.server")


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

    def test_report_lists_a_wrangler_tree_under_its_root(self) -> None:
        """The 2026-09-24 chain through the real CLI: wrangler.js (PPID 1) ->
        node cli.js -> workerd, no lock on the worktree. Before the fix this
        printed `dev servers to SIGTERM (0)` and `keeping (0)`."""
        wt = self.add_worktree("codex/apple-tv-release-ready-20260921")
        _write_fake(self.bin, "ps", (
            'echo "1 0 10:00:00 5000 /sbin/launchd"\n'
            f'echo "22907 1 62:28:25 3408 node {wt}/node_modules/wrangler/bin/wrangler.js'
            ' pages dev /private/tmp/out --port 8773 --ip 127.0.0.1"\n'
            f'echo "22910 22907 62:28:25 1472 /opt/homebrew/bin/node --no-warnings'
            f' {wt}/node_modules/wrangler/wrangler-dist/cli.js pages dev /private/tmp/out --port 8773"\n'
            f'echo "22916 22910 62:28:25 909088 {wt}/node_modules/@cloudflare/workerd-darwin-arm64/bin/workerd'
            ' serve --binary --experimental --socket-addr=entry=127.0.0.1:8773 --control-fd=3 -"'
        ))
        _write_fake(self.bin, "lsof", f'echo "p22907"; echo "fcwd"; echo "n{wt}"')
        r = self.run_cli("report", str(self.root))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dev servers to SIGTERM (1)", r.stdout)
        self.assertIn("pid=22907", r.stdout)
        self.assertIn("tree: 2 descendant(s), 889MB, signalled with the root", r.stdout)
        self.assertIn("pid=22910", r.stdout)
        self.assertIn("pid=22916", r.stdout)
        self.assertIn("keeping (0)", r.stdout)
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
        saved_running = sr.still_running
        self.addCleanup(lambda: setattr(sr, "CDP_URL", saved_url))
        self.addCleanup(lambda: setattr(sr, "docker", saved_docker))
        self.addCleanup(lambda: setattr(sr, "still_running", saved_running))
        sr.CDP_URL = self.cdp.url          # the fixture server, serving no targets
        sr.docker = lambda *a, **k: None   # reads as "docker unavailable"
        # Fixture pids (22907 …) are real pids on the machine this was written
        # on. The exit check must never read the real process table here.
        sr.still_running = phased(ALL_ALIVE, lambda pids: set())
        # The hook's 15s budget is measured from module import, and under
        # `discover` the other suites can spend longer than that before this
        # runs — cmd_reap then skips the dev action as past its deadline and
        # the test asserts on a pass that never happened. Measured: 2 to 6
        # of these failed per full run, never in isolation. Restart the clock.
        saved_started = sr._STARTED_AT
        self.addCleanup(lambda: setattr(sr, "_STARTED_AT", saved_started))
        sr._STARTED_AT = time.time()

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

    def test_a_reap_signals_the_root_first_then_every_descendant(self) -> None:
        """Root first so wrangler's own teardown runs; then each descendant,
        because workerd does not die with its parent on its own."""
        self.isolate_in_process()
        wt = self.add_worktree("codex/apple-tv-release-ready-20260921")
        procs = {
            1: proc(0, "/sbin/launchd"),
            22907: proc(1, f"node {wt}/node_modules/wrangler/bin/wrangler.js pages dev ./out --port 8773", rss_kb=3408),
            22910: proc(22907, f"node --no-warnings {wt}/node_modules/wrangler/wrangler-dist/cli.js pages dev ./out --port 8773", rss_kb=1472),
            22916: proc(22910, f"{wt}/node_modules/@cloudflare/workerd-darwin-arm64/bin/workerd serve --binary", rss_kb=909088),
        }
        self.stub_processes(procs, {22907: str(wt)})
        killed: list[int] = []
        saved_kill = sr.kill_pid
        sr.kill_pid = lambda pid: killed.append(pid) or True
        try:
            sr.cmd_reap({"cwd": str(self.root)})
        finally:
            sr.kill_pid = saved_kill
        self.assertEqual(killed, [22907, 22910, 22916])
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("sigterm_dev_server pid=22907", text)
        self.assertIn("sigterm_dev_server_child pid=22910 root=22907", text)
        self.assertIn("sigterm_dev_server_child pid=22916 root=22907", text)
        self.assertIn("dev_server_exited pid=22916 root=22907", text)
        self.assertIn(
            "dev_servers_signalled=1 dev_children_signalled=2 "
            "dev_exited=3 dev_survived=0 dev_rss_freed=892MB",
            text,
        )

    def _signal_wrangler_tree(self, running, before=ALL_ALIVE) -> str:
        """Run an in-process reap over the wrangler tree with signals stubbed
        and `still_running` answered by `running`; return the log."""
        self.isolate_in_process()
        wt = self.add_worktree("codex/apple-tv-release-ready-20260921")
        procs = {
            1: proc(0, "/sbin/launchd"),
            22907: proc(1, f"node {wt}/node_modules/wrangler/bin/wrangler.js pages dev ./out --port 8773", rss_kb=3408),
            22910: proc(22907, f"node --no-warnings {wt}/node_modules/wrangler/wrangler-dist/cli.js pages dev ./out --port 8773", rss_kb=1472),
            22911: proc(22910, f"{wt}/node_modules/@esbuild/darwin-arm64/bin/esbuild --service=0.25.0", rss_kb=15360),
            22916: proc(22910, f"{wt}/node_modules/@cloudflare/workerd-darwin-arm64/bin/workerd serve --binary", rss_kb=1203200),
        }
        self.stub_processes(procs, {22907: str(wt)})
        saved = (sr.kill_pid, sr.DEV_EXIT_GRACE_SECONDS)
        sr.kill_pid = lambda pid: True
        sr.DEV_EXIT_GRACE_SECONDS = 0.3
        sr.still_running = phased(before, running)
        try:
            sr.cmd_reap({"cwd": str(self.root)})
        finally:
            sr.kill_pid, sr.DEV_EXIT_GRACE_SECONDS = saved
        return self.log.read_text(encoding="utf-8")

    def test_a_process_that_ignores_sigterm_is_not_reported_as_freed(self) -> None:
        """The 2026-09-25 reap-now: the log said `killed_dev_server` and
        `dev_rss_freed=1204MB`; wrangler, its node child and the 1 GB workerd
        were all still running. Only esbuild actually exited."""
        text = self._signal_wrangler_tree(lambda pids: {22907, 22910, 22916} & set(pids))
        self.assertNotIn("killed_dev_server", text)
        self.assertIn("dev_server_survived_sigterm pid=22916 root=22907", text)
        self.assertIn("dev_server_survived_sigterm pid=22907 rss=", text)
        self.assertIn("dev_server_exited pid=22911 root=22907 rss=15MB", text)
        self.assertIn("dev_exited=1 dev_survived=3 dev_rss_freed=15MB", text)

    def test_the_exit_check_waits_out_a_slow_teardown(self) -> None:
        """A server that needs a moment to close its sockets counts as exited
        once it goes within the grace, not as a survivor on the first look."""
        calls = []

        def slow(pids):
            calls.append(1)
            return set(pids) if len(calls) < 2 else set()

        text = self._signal_wrangler_tree(slow)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("dev_exited=4 dev_survived=0", text)

    def test_the_exit_wait_runs_after_tabs_and_stacks(self) -> None:
        """The commit review of cd77c14: waiting right after the signals let a
        server that ignores SIGTERM spend the budget the stack action needed,
        which then skipped without marking the run truncated."""
        order: list[str] = []
        saved = (sr.plan_tabs, sr.plan_stacks, sr.confirm_exits)
        self.addCleanup(lambda: setattr(sr, "plan_tabs", saved[0]))
        self.addCleanup(lambda: setattr(sr, "plan_stacks", saved[1]))
        self.addCleanup(lambda: setattr(sr, "confirm_exits", saved[2]))
        saved_env = os.environ.pop("SESSION_REAPER_ACTIONS", None)
        if saved_env is not None:
            self.addCleanup(lambda: os.environ.__setitem__("SESSION_REAPER_ACTIONS", saved_env))
        sr.plan_tabs = lambda: (order.append("tabs"), ([], []))[1]
        sr.plan_stacks = lambda *a, **k: (order.append("stacks"), ([], []))[1]
        sr.confirm_exits = lambda pids: (order.append("confirm"), (set(), False))[1]
        self._signal_wrangler_tree(lambda pids: set())
        self.assertEqual(order, ["tabs", "stacks", "confirm"])

    def _truncated_reap_over_two_trees(self, running, before=None) -> Path:
        """Signal the first of two orphaned servers, run out of budget before
        the second, answer the exit check with `running`; return the stamp."""
        self.isolate_in_process()
        a, b = self.add_worktree("first"), self.add_worktree("second")
        procs = {
            1: proc(0, "/sbin/launchd"),
            300: proc(1, f"node {a}/node_modules/.bin/vite dev --port 5198"),
            302: proc(300, "<defunct>", rss_kb=0),
            301: proc(1, f"node {b}/node_modules/.bin/vite dev --port 5199"),
        }
        self.stub_processes(procs, {300: str(a), 301: str(b)})
        killed: list[int] = []
        saved = (sr.kill_pid, sr.past_deadline, sr.budget_left, sr.still_running)
        sr.kill_pid = lambda pid: killed.append(pid) or True
        # Out of time after one kill, and out of budget with it: that is what
        # truncation means, and it is what clips the exit grace to zero. The
        # review of e21607d found the first version of this helper ran out of
        # time with the budget still full, so the grace was never cut short.
        sr.past_deadline = lambda: bool(killed)
        sr.budget_left = lambda: 0.0 if killed else 3600.0
        # 302 is a zombie child of 300 in every variant: already exited.
        before = before or (lambda pids: set(pids) - {302})
        sr.still_running = phased(before, running)
        try:
            sr.cmd_reap({"cwd": str(self.root)})
        finally:
            (sr.kill_pid, sr.past_deadline, sr.budget_left,
             sr.still_running) = saved
        text = self.log.read_text(encoding="utf-8")
        if running([300]):
            self.assertIn("dev_server_not_yet_exited pid=300", text)
        self.assertEqual(killed, [300])
        self.assertIn("TRUNCATED", self.log.read_text(encoding="utf-8"))
        return self.locks / sr.STAMP_FILENAME

    def test_a_truncated_run_whose_servers_ignored_sigterm_still_stamps(self) -> None:
        """Otherwise every later turn re-signals the same survivors."""
        stamp = self._truncated_reap_over_two_trees(lambda pids: set(pids))
        self.assertTrue(stamp.exists())

    def test_a_truncated_run_that_freed_a_server_leaves_the_repo_unstamped(self) -> None:
        stamp = self._truncated_reap_over_two_trees(lambda pids: set())
        self.assertFalse(stamp.exists())

    def test_a_zombie_from_an_earlier_pass_is_not_signalled_or_counted(self) -> None:
        """The second pass over the 2026-09-25 tree: esbuild (22911) is
        already <defunct> under the wrangler that ignored SIGTERM. Signalling
        it succeeds and the exit check reads it as gone, so without the
        pre-signal read it was a fresh "exit" on every pass."""
        survivors = lambda pids: {22907, 22910, 22916} & set(pids)
        text = self._signal_wrangler_tree(survivors, before=survivors)
        self.assertNotIn("sigterm_dev_server_child pid=22911", text)
        self.assertIn("dev_server_already_exited pid=22911 root=22907", text)
        self.assertIn("dev_exited=0 dev_survived=3 dev_rss_freed=0MB", text)

    def test_a_truncated_run_over_survivors_and_zombies_still_stamps(self) -> None:
        """Zombies must not count as progress, or the loop comes back."""
        stamp = self._truncated_reap_over_two_trees(
            lambda pids: {300} & set(pids), before=lambda pids: {300, 301} & set(pids),
        )
        self.assertTrue(stamp.exists())
        self.assertIn("dev_server_already_exited pid=302 root=300",
                      self.log.read_text(encoding="utf-8"))

    def test_a_grace_clipped_by_the_hook_budget_is_reported_as_cut_short(self) -> None:
        """Near the hook deadline the grace clips toward zero."""
        saved = (sr._DEADLINE_ON, sr.budget_left, sr.still_running,
                 sr.DEV_EXIT_GRACE_SECONDS)
        try:
            sr._DEADLINE_ON = True
            sr.budget_left = lambda: 1.1        # grace 0.1s, below the default
            sr.DEV_EXIT_GRACE_SECONDS = 3.0
            sr.still_running = lambda pids: {22916}
            self.assertEqual(sr.confirm_exits([22916]), ({22916}, True))
            sr.budget_left = lambda: 3600.0     # full grace available
            sr.DEV_EXIT_GRACE_SECONDS = 0.3
            self.assertEqual(sr.confirm_exits([22916]), ({22916}, False))
        finally:
            (sr._DEADLINE_ON, sr.budget_left, sr.still_running,
             sr.DEV_EXIT_GRACE_SECONDS) = saved

    def test_a_process_left_by_a_cut_short_grace_is_not_yet_exited(self) -> None:
        """Still in teardown is not the same as survived SIGTERM."""
        saved = sr.confirm_exits
        self.addCleanup(lambda: setattr(sr, "confirm_exits", saved))
        sr.confirm_exits = lambda pids: ({22916}, True)
        text = self._signal_wrangler_tree(lambda pids: set())
        self.assertIn("dev_server_not_yet_exited pid=22916 root=22907", text)
        self.assertNotIn("dev_server_survived_sigterm", text)
        self.assertIn("dev_exited=3 dev_survived=0 dev_not_yet_exited=1", text)

    def test_an_exit_check_that_cannot_read_ps_frees_nothing(self) -> None:
        text = self._signal_wrangler_tree(lambda pids: None)
        self.assertIn("dev_server_exit_unverified pid=22916 root=22907", text)
        self.assertIn("dev_exited=0 dev_survived=0 dev_unverified=4 dev_rss_freed=0MB", text)

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
        self.assertIn("dev_servers_signalled=0", text)  # the summary line still lands

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
