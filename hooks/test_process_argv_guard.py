#!/usr/bin/env python3
"""Regression test for process-argv-guard.py. Run: python3 -m unittest test_process_argv_guard

Every case feeds a command string to the hook; nothing here runs ps or pgrep.
HOME points at a per-test tmpdir so the .guard-off escape hatch is isolated.
Malformed payloads are fed as raw strings, not built by a helper, because a
helper can only ever produce well-shaped payloads (json-shape-guard gap,
2026-09-19).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).with_name("process-argv-guard.py")
REDACTOR = r"sed -E 's/([A-Za-z0-9_.-]+=)[^[:space:]]+/\1REDACTED/g'"
# The narrower redactor first proposed; it leaks R2_ACCESS_KEY_ID= and
# DATABASE_URL=, so it must not unlock a pipeline.
NARROW_REDACTOR = (r"sed -E 's/(--binding [A-Z_]+=)[^ ]+/\1REDACTED/g; "
                   r"s/(KEY|TOKEN|SECRET)=[^ ]+/\1=REDACTED/g'")

DENY = [
    # the spec's forms
    "pgrep -fl wrangler",
    "pgrep -lf wrangler",
    "pgrep -l wrangler",
    "pgrep -a node",
    "pgrep -f -l 'wrangler pages dev'",
    "pgrep -afl node",
    "pgrep --list-full node",
    "pgrep -u nino -fl node",
    "ps aux",
    "ps auxww",
    "ps -ef",
    "ps eww 123",
    "ps -o args= -p 123",
    "ps -o command -p 123",
    "ps -o pid,command",
    "ps -ww -p 123",
    "cat /proc/123/cmdline",
    "cat /proc/*/cmdline",
    "tr '\\0' ' ' < /proc/1/cmdline",
    "strings /proc/self/environ",
    # beyond the flag list: plain ps prints the command column on macOS
    "ps -p 123",
    "ps",
    "ps -axo pid,command",
    "ps axo pid,args",
    "ps -O ppid -p 123",
    "ps -c -E -p 123",
    # format sets that append COMMAND to an -o list (commit review of c7dc75f)
    "ps -j -o pid -p 123",
    "ps -l -o pid -p 123",
    "ps -v -o pid -p 123",
    "ps -f -o pid -p 123",
    "ps uo pid",
    # pipelines that still print argv
    "ps aux | grep wrangler",
    "ps aux | grep wrangler | head -5",
    "ps aux | sed 's/foo/bar/'",
    "ps aux | sed 's/x/REDACTED/'",
    f"ps aux | {NARROW_REDACTOR}",
    "pgrep -f wrangler | xargs ps -o args= -p",
    "pgrep -f wrangler | xargs ps -p",
    # wrappers, separators, nesting
    "sudo ps aux",
    "FOO=1 ps aux",
    "env -i ps aux",
    "timeout 5 ps aux",
    "cd /tmp && ps aux",
    "echo hi; pgrep -fl wrangler",
    "true || ps -ef",
    "bash -c 'ps aux'",
    "zsh -lc \"pgrep -fl wrangler\"",
    "eval 'ps aux'",
    "echo \"$(ps aux)\"",
    "x=$(ps aux | grep node); echo $x",
    "echo `ps aux`",
    # a heredoc fed to a shell is the command, not prose
    "bash <<'EOF'\nps aux\nEOF",
    "cat <<'EOF' | sh\npgrep -fl wrangler\nEOF",
    "sudo zsh -s <<EOF\necho hi\nps -ef\nEOF",
    # commit review of 95d64a3
    "cat <<EOF\n$(ps aux)\nEOF",
    "cat <<EOF\n`pgrep -fl wrangler`\nEOF",
    "bash <<< 'ps aux'",
    "sh <<< \"pgrep -fl wrangler\"",
    "ps aux | sed -E -e p -e 's/([A-Za-z0-9_.-]+=)[^[:space:]]+/\\1REDACTED/g'",
    "ps aux | sed -n -E 's/([A-Za-z0-9_.-]+=)[^[:space:]]+/\\1REDACTED/gp'",
    "/bin/ps aux",
    "if pgrep -fl wrangler; then echo up; fi",
    "pstree -p 123",
    "procs wrangler",
    # unparseable by shlex: an apostrophe in a comment
    "ls # don't print argv\nps aux",
    "echo 'unbalanced\nps aux",
]

ALLOW = [
    "pgrep -f wrangler",
    "pgrep -f 'wrangler pages dev'",
    "pgrep -f wrangler | wc -l",
    "pgrep -fu nino node",
    "ps -o pid=,comm= -p 123",
    "ps -o pid= -o comm= -p 123",
    "ps -p 123 -o pid,ppid,etime,comm",
    "ps -axc",
    "ps axc",
    "ps -c -p 123",
    "ps -u eric -o pid,comm",
    "ps aux | wc -l",
    "ps aux | grep -c wrangler",
    "ps aux | grep -q wrangler && echo running",
    f"ps aux | {REDACTOR}",
    f"ps aux | grep wrangler | {REDACTOR}",
    f"ps aux |\n  {REDACTOR}",
    f"ps aux | \n  {REDACTOR}",
    f"pgrep -fl wrangler | {REDACTOR}",
    "pgrep -f wrangler | xargs ps -o pid=,comm= -p",
    "pgrep -f wrangler >/dev/null 2>&1 && echo up",
    # prose about the rule must pass
    "git commit -m \"deny pgrep -fl and ps aux; allow pgrep -f\"",
    "echo \"never run ps aux\"",
    "echo 'echo $(ps aux) leaks'",
    "cat > note.md <<'EOF'\nNever run ps aux or pgrep -fl here.\ncat /proc/1/cmdline\nEOF",
    "grep -n 'ps aux' notes.md",
    "rg 'pgrep -fl' ~/.claude",
    "# ps aux would leak\nls",
    "bash <<'EOF'\npgrep -f wrangler | wc -l\nEOF",
    "cat <<'EOF' > script.sh\nps aux\nEOF",
    "python3 - <<'EOF'\nprint('ps aux')\nEOF",
    # escaped substitutions are literal text
    'git commit -m "docs: \\`ps aux | grep x\\` is denied"',
    'echo \\`ps aux\\`',
    'echo "\\$(ps aux) is literal"',
    "cat <<'EOF'\n$(ps aux) is documented here\nEOF",
    "bash <<< 'pgrep -f wrangler | wc -l'",
    "ps aux | sed -E -e 's/([A-Za-z0-9_.-]+=)[^[:space:]]+/\\1REDACTED/g'",
    # names that merely start with ps
    "psql -c 'select 1'",
    "pstree -p 123 | wc -l",
    "ls /proc",
    "git status",
]


class ProcessArgvGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_raw(self, raw: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, HOME=self.home)
        return subprocess.run([sys.executable, str(HOOK), "check"], input=raw,
                              capture_output=True, text=True, timeout=10, env=env)

    def decide(self, payload: dict) -> tuple[str, str]:
        result = self.run_raw(json.dumps(payload))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        if not result.stdout.strip():
            return "allow", ""
        spec = json.loads(result.stdout)["hookSpecificOutput"]
        return spec["permissionDecision"], spec["permissionDecisionReason"]

    def bash(self, command: str) -> tuple[str, str]:
        return self.decide({"tool_name": "Bash", "tool_input": {"command": command}})

    def test_denied_forms(self):
        for command in DENY:
            with self.subTest(command=command):
                self.assertEqual(self.bash(command)[0], "deny")

    def test_allowed_forms(self):
        for command in ALLOW:
            with self.subTest(command=command):
                self.assertEqual(self.bash(command)[0], "allow")

    def test_retry_is_denied_again(self):
        self.assertEqual(self.bash("pgrep -fl wrangler")[0], "deny")
        self.assertEqual(self.bash("pgrep -fl wrangler")[0], "deny")

    def test_message_names_the_safe_forms_and_the_redactor(self):
        _, reason = self.bash("ps aux")
        for needle in ("pgrep -f <pattern>", "| wc -l", "ps -o pid=,comm= -p <pid>", REDACTOR,
                       ".guard-off"):
            with self.subTest(needle=needle):
                self.assertIn(needle, reason)

    def test_codex_exec_shape(self):
        leak = 'await tools.exec_command({cmd: "pgrep -fl wrangler"})'
        safe = "await tools.exec_command({'cmd': 'pgrep -f wrangler | wc -l'})"
        self.assertEqual(self.decide({"tool_name": "exec", "tool_input": {"code": leak}})[0], "deny")
        self.assertEqual(self.decide({"tool_name": "exec", "tool_input": {"code": safe}})[0], "allow")
        # A freeform `exec` may send its source bare.
        self.assertEqual(self.decide({"tool_name": "exec", "tool_input": leak})[0], "deny")
        self.assertEqual(self.decide({"tool_name": "exec", "tool_input": safe})[0], "allow")

    def test_other_tools_ignored(self):
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/proc/1/cmdline"}}
        self.assertEqual(self.decide(payload)[0], "allow")

    def test_guard_off_allows(self):
        off = Path(self.home, ".claude", "cache", "process-argv-guard", ".guard-off")
        off.parent.mkdir(parents=True)
        off.touch()
        self.assertEqual(self.bash("ps aux")[0], "allow")

    def test_malformed_payloads_fail_open_silently(self):
        for raw in ("", "not json", "null", "[]", '"x"', "123",
                    '{"tool_name":"Bash","tool_input":"a string"}',
                    '{"tool_name":"Bash","tool_input":{"command":5}}',
                    '{"tool_name":"Bash","tool_input":null}',
                    '{"tool_name":"exec","tool_input":{"code":["pgrep -fl x"]}}'):
            with self.subTest(raw=raw):
                result = self.run_raw(raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
