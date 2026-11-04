"""Tests for secret-scan.py. Run: python3 -m unittest discover skills/session-closeout/scripts

Fake credentials are assembled at runtime so no key-shaped literal lands in the
repo (the pre-commit hook and GitGuardian would both flag one)."""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "secret-scan.py"


def rand(n: int = 32) -> str:
    return secrets.token_hex(n // 2)


def typed_password() -> str:
    """The shape a person types: short, mixed case, a digit, a symbol. Too short for token_shaped."""
    return f"Tulip{secrets.randbelow(10)}:{secrets.randbelow(90) + 10}moss"


def line(text: str) -> str:
    return json.dumps({"type": "user", "message": {"content": text}}) + "\n"


class SecretScanTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cwd = self.home / "work"
        self.cwd.mkdir()
        self.slug = re.sub(r"[^A-Za-z0-9]", "-", str(self.cwd.resolve()))
        self.project = self.home / ".claude" / "projects" / self.slug
        self.project.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def write(self, sid: str, *texts: str) -> Path:
        p = self.project / f"{sid}.jsonl"
        p.write_text("".join(line(t) for t in texts))
        return p

    def run_scan(self, *args, env_extra=None, script=SCRIPT):
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "")}
        env.update(env_extra or {})
        return subprocess.run([sys.executable, str(script), *args], cwd=self.cwd, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def test_reports_each_detector_and_never_prints_values(self):
        prefix_key = "sk-" + "ant-" + rand(40)
        assigned = rand(40)
        bearer = rand(48)
        side_key = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6Q7r8S9t0"[:16]
        self.write("s1", f"key is {prefix_key}", f"env: MY_SERVICE_TOKEN={assigned} USER=nino",
                   f"curl -H 'Authorization: Bearer {bearer}' https://x.test")
        side = self.project / "s1" / "tool-results"
        side.mkdir(parents=True)
        (side / "big-output.txt").write_text(f"saved output\n{side_key}\n")

        r = self.run_scan("--session-id", "s1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("prefix", r.stdout)
        self.assertIn("assign MY_SERVICE_TOKEN", r.stdout)
        self.assertIn("bearer", r.stdout)
        self.assertIn("tool-results/big-output.txt", r.stdout)
        for value in (prefix_key, assigned, bearer, side_key):
            self.assertNotIn(value[-12:], r.stdout)

    def test_live_value_from_environment(self):
        value = rand(64)
        self.write("s2", f"ps output ... {value} ...")
        r = self.run_scan("--session-id", "s2", env_extra={"DEMO_CONTEXT_TOKEN": value})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("live   DEMO_CONTEXT_TOKEN", r.stdout)
        self.assertIn("rotate it", r.stdout)
        self.assertNotIn(value[:8], r.stdout)

    def test_lookalikes_are_clean(self):
        self.write("s3",
                   'bearer_token_env_var = "RECALL_CONTEXT_TOKEN"',
                   "ANTHROPIC_API_KEY=$(with-secret 'Anthropic x')",
                   "API_KEY=op://Developer Secrets/Item/credential",
                   "export GH_TOKEN=${GH_TOKEN}",
                   f"QUANTIFAI_KEYRING_SERVICE=svc{rand(20)}",
                   f"VITE_SUPABASE_ANON_KEY=ey{rand(40)}",
                   f"PUBLIC_SUPABASE_PUBLISHABLE_DEFAULT_KEY=sb{rand(40)}",
                   f"TURNSTILE_SITE_KEY=0x{rand(24)}",
                   "STRIPE_SECRET_KEY=your-stripe-secret-key-here",
                   "Authorization: Bearer <token>")
        r = self.run_scan("--session-id", "s3")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("SECRET_SCAN_CLEAN", r.stdout)

    def test_json_escaped_env_dump_quoted_export_and_json_config(self):
        a, b, c = rand(40), rand(40), rand(40)
        # A tool result holding `cat .env` output: real newlines, stored JSON-escaped.
        env_dump = f"PORT=3000\nSTRIPE_SECRET_KEY={a}\nDEBUG=1\n"
        p = self.project / "s6.jsonl"
        p.write_text(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": env_dump}]}}) + "\n"
            + line(f'export DEPLOY_TOKEN="{b}"')
            + line(json.dumps({"mcpServers": {"x": {"env": {"SERVICE_API_KEY": c}}}})))
        r = self.run_scan("--session-id", "s6")
        self.assertEqual(r.returncode, 1, r.stdout)
        for name in ("STRIPE_SECRET_KEY", "DEPLOY_TOKEN", "SERVICE_API_KEY"):
            self.assertIn(f"assign {name}", r.stdout)
        for value in (a, b, c):
            self.assertNotIn(value[-12:], r.stdout)

    def test_secret_in_a_tool_call_input_object(self):
        value = rand(40)
        p = self.project / "s8.jsonl"
        p.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__x__configure", "input": {"API_KEY": value}}]}}) + "\n")
        r = self.run_scan("--session-id", "s8")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("assign API_KEY", r.stdout)
        self.assertNotIn(value[-12:], r.stdout)

    def test_empty_assignment_does_not_take_the_next_line(self):
        self.write("s9", f"API_KEY=\nDATABASE_URL_X=postgres{rand(24)}\n")
        r = self.run_scan("--session-id", "s9")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_typed_password_after_a_label(self):
        # Regression (2026-11-04): a password pasted into a chat message, read through
        # an MCP tool, scanned clean. The tool returns JSON as text, so the message
        # sits in JSON inside the transcript's JSON, and one decode leaves its
        # newlines as backslash-n. This mirrors that record's structure.
        pw, pw2, pw3 = typed_password(), typed_password(), typed_password()
        chat = json.dumps({"messages": "=== Message from A (U01) at 2026-11-04 ===\n"
                           f"Login for the test account:\nsomeone@corp.test\nPW: {pw}\n",
                           "pagination_info": "No more messages"})
        mcp = [{"type": "text", "text": chat}]
        p = self.project / "s11.jsonl"
        p.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"tool_use_id": "toolu_01", "type": "tool_result", "content": mcp}]},
            "toolUseResult": mcp}) + "\n"
            + json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "content": f"$ cat notes.txt\nstaging admin\npassword: {pw2}\n"}]}}) + "\n"
            + line(f"Server=db.test;Uid=app;Pwd={pw3};"))
        r = self.run_scan("--session-id", "s11")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("passwd PW  s11.jsonl line 1", r.stdout)
        self.assertIn("passwd password  s11.jsonl line 2", r.stdout)
        self.assertIn("passwd Pwd  s11.jsonl line 3", r.stdout)
        for value in (pw, pw2, pw3):
            self.assertNotIn(value, r.stdout)
            self.assertNotIn(value[-6:], r.stdout)

    def test_password_lookalikes_are_clean(self):
        self.write("s12",
                   "cd $(pwd)/build && pwd=$(pwd)",
                   "PWD=/Users/someone/work\nOLDPWD=/Users/someone",
                   "def login(user: str, password: str, pw: Optional[str] = None):",
                   'password=os.environ.get("DB_PASSWORD")',
                   "password=sha256(raw).hexdigest()",
                   "password: changeme123",
                   "password: process.env.DB_PASSWORD, pw: true",
                   'config = {"password": "${DB_PASSWORD}", "pwd": "<your password>"}',
                   "password: ********",
                   "PW: same as the one in 1Password",
                   "To reset the password: step2 in the runbook.",
                   f"https://zoom.test/j/8123?pwd={rand(30)}.1&from=addon")
        r = self.run_scan("--session-id", "s12")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("SECRET_SCAN_CLEAN", r.stdout)

    def test_password_label_in_an_image_is_not_text(self):
        # A PDF page saved beside the transcript as a JPEG: random bytes can spell "pw:".
        self.write("s13", "nothing here")
        side = self.project / "s13" / "tool-results" / "pdf-1"
        side.mkdir(parents=True)
        (side / "page-01.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\n\x8aPw:" + typed_password().encode() + b"\x00\xff\xd9")
        r = self.run_scan("--session-id", "s13")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_too_deeply_nested_line_is_scanned_as_text(self):
        bearer = rand(48)
        depth = 5000  # past the JSON decoder's recursion limit
        self.project.joinpath("s14.jsonl").write_text(
            "[" * depth + json.dumps(f"curl -H 'Authorization: Bearer {bearer}'") + "]" * depth + "\n")
        r = self.run_scan("--session-id", "s14")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("bearer", r.stdout)
        self.assertNotIn(bearer[-12:], r.stdout)

    def test_unlistable_side_folder_is_an_error_not_clean(self):
        self.write("s10", "nothing here")
        sub = self.project / "s10" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent.jsonl").write_text(line("nothing"))
        sub.chmod(0)
        try:
            r = self.run_scan("--session-id", "s10")
        finally:
            sub.chmod(0o700)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("could not read", r.stdout)

    def test_unreadable_transcript_is_an_error_not_clean(self):
        p = self.write("s7", "nothing here")
        p.chmod(0)
        try:
            r = self.run_scan("--session-id", "s7")
        finally:
            p.chmod(0o600)
        self.assertEqual(r.returncode, 2, r.stdout)

    def test_codex_guess_matches_a_cwd_with_spaces(self):
        spaced = self.home / "Application Support" / "work"
        spaced.mkdir(parents=True)
        day = self.home / ".codex" / "sessions" / "2026" / "10" / "07"
        day.mkdir(parents=True)
        (day / "rollout-2026-10-07T00-00-00-abc.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": str(spaced.resolve())}}) + "\n")
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "")}
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=spaced, env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("rollout-2026-10-07T00-00-00-abc.jsonl", r.stdout)

    def test_codex_thread_id_finds_its_rollout(self):
        # SKILL.md step 4 tells Codex to pass --session-id "$CODEX_THREAD_ID".
        thread = "01a00000-0000-7000-8000-00000000c0de"
        value = rand(40)
        day = self.home / ".codex" / "sessions" / "2026" / "10" / "30"
        day.mkdir(parents=True)
        rollout = day / f"rollout-2026-10-30T09-00-00-{thread}.jsonl"
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n"
            + json.dumps({"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "output": f"$ cat .env\nDEPLOY_TOKEN={value}\n"}}) + "\n")
        r = self.run_scan("--session-id", thread)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"{rollout} (session id;", r.stdout)
        self.assertIn("assign DEPLOY_TOKEN", r.stdout)
        self.assertNotIn(value[-12:], r.stdout)

    def test_unsubstituted_session_id_falls_back_to_cwd_guess(self):
        self.write("s4", "nothing here")
        r = self.run_scan("--session-id", "${CLAUDE_SESSION_ID}")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("guessed from cwd", r.stdout)
        self.assertIn("s4.jsonl", r.stdout)

    def test_unknown_session_is_an_error_not_clean(self):
        r = self.run_scan("--session-id", "no-such-session")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not a clean result", r.stdout)

    def test_missing_pattern_source_is_an_error_not_clean(self):
        copy = self.home / "repo" / "skills" / "session-closeout" / "scripts" / "secret-scan.py"
        copy.parent.mkdir(parents=True)
        shutil.copy(SCRIPT, copy)
        self.write("s5", "nothing here")
        r = self.run_scan("--session-id", "s5", script=copy)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("SECRET_SCAN_ERROR", r.stdout)


if __name__ == "__main__":
    unittest.main()
