#!/usr/bin/env python3
"""Regression test for read-guard.py. Run: python3 -m unittest test_read_guard

Isolation is HOME-only: read-guard.py computes CACHE_DIR/STATE_DIR/OVERRIDE_FILE
from Path.home() at import, with no env override, so every subprocess gets a
HOME pointed at a per-test tmpdir. realpath() it first — tempfile hands back
/var/folders/... while the hook abspath()s without resolving, and the mismatch
would silently break the "reads of our own cache copies are allowed" branch.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

HOOK = Path(__file__).with_name("read-guard.py")
HAVE_SIPS = shutil.which("sips") is not None


def make_png(path: Path, w: int, h: int) -> None:
    """Minimal RGB PNG with incompressible pixels.

    urandom rather than a solid fill on purpose: the hook skips the dimension
    probe entirely below SIZE_PROBE_BYTES (200KB), and a flat 1500x1500 image
    compresses to a few KB — it would be allowed without ever measuring.
    """
    raw = b"".join(b"\x00" + os.urandom(w * 3) for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"IEND", b"")
    )


def read(path, session="sess1", **tool_input):
    return {"session_id": session, "tool_name": "Read",
            "tool_input": {"file_path": str(path), **tool_input}}


class ReadGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # realpath: the hook abspath()s the target but never resolves symlinks.
        self.home = os.path.realpath(self.tmp.name)
        self.cache = Path(self.home, ".claude", "cache", "read-guard")
        self.sessions = self.cache / "sessions"
        self.work = Path(self.home, "work")
        self.work.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    # --- plumbing ---------------------------------------------------------

    def run_hook(self, payload, raw=None):
        """Returns (exit_code, parsed_stdout_or_None).

        input= closes the child's stdin and timeout= bounds it; without both, a
        hook that json.load()s sys.stdin hangs forever on whatever stdin the
        runner inherited (measured in this repo 2026-08-27).
        """
        p = subprocess.run(
            [sys.executable, str(HOOK)],
            input=raw if raw is not None else json.dumps(payload),
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "HOME": self.home},
        )
        return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)

    def assertAllowed(self, payload, msg=None):
        self.assertEqual(self.run_hook(payload), (0, None), msg)

    def assertDenied(self, payload, substring, msg=None):
        code, out = self.run_hook(payload)
        self.assertEqual(code, 0, msg)         # deny is stdout JSON, never a nonzero exit
        self.assertIsNotNone(out, msg)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse", msg)
        self.assertEqual(hso["permissionDecision"], "deny", msg)
        self.assertIn(substring, hso["permissionDecisionReason"], msg)
        return hso["permissionDecisionReason"]

    def seed_state(self, path, ts_offset, session="sess1", pending_retry=False):
        """Write a prior-read record straight into session state.

        mtime comes from a real stat of the fixture: the re-read branch compares
        it for exact equality, so a fabricated value would skip the branch and
        the test would pass for the wrong reason.
        """
        self.sessions.mkdir(parents=True, exist_ok=True)
        Path(self.sessions, f"{session}.json").write_text(json.dumps({
            str(path): {"mtime": os.stat(path).st_mtime,
                        "ts": time.time() + ts_offset,
                        "pending_retry": pending_retry},
        }))

    def text_file(self, name="notes.txt", body="hello\n" * 50):
        p = self.work / name
        p.write_text(body)
        return p

    # --- image branch -----------------------------------------------------

    @unittest.skipUnless(HAVE_SIPS, "image branch shells out to sips (macOS)")
    def test_oversized_image_denied_once_then_retry_allowed(self):
        img = self.work / "shot.png"
        make_png(img, 1500, 1500)
        self.assertGreater(img.stat().st_size, 200_000)  # clears the size probe

        reason = self.assertDenied(read(img), "oversized image")
        self.assertIn("1500x1500", reason)

        # The copy is real, on disk, 1000px on the long edge, and named in the
        # deny text — a deny that points at a path the agent cannot read is
        # worse than no deny at all.
        copies = [f for f in self.cache.iterdir() if f.is_file()]
        self.assertEqual(len(copies), 1)
        self.assertIn(str(copies[0]), reason)
        probe = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight",
                                str(copies[0])], capture_output=True, text=True, timeout=10)
        dims = [int(l.split(":")[1]) for l in probe.stdout.splitlines()
                if l.strip().startswith(("pixelWidth:", "pixelHeight:"))]
        self.assertEqual(max(dims), 1000)

        # Immediate retry of the same path: allowed (full-res escape hatch).
        self.assertAllowed(read(img))

    @unittest.skipUnless(HAVE_SIPS, "image branch shells out to sips (macOS)")
    def test_reading_the_downscaled_copy_is_always_allowed(self):
        img = self.work / "shot.png"
        make_png(img, 1500, 1500)
        self.assertDenied(read(img), "oversized image")
        copy = next(f for f in self.cache.iterdir() if f.is_file())
        # Twice: the cache copy must not fall into the re-read branch either.
        self.assertAllowed(read(copy))
        self.assertAllowed(read(copy))

    @unittest.skipUnless(HAVE_SIPS, "image branch shells out to sips (macOS)")
    def test_image_under_trigger_edge_allowed_after_probing(self):
        # >200KB so the size shortcut does not fire — this is the case that
        # actually exercises the TRIGGER_EDGE comparison.
        img = self.work / "small.png"
        make_png(img, 1200, 1200)
        self.assertGreater(img.stat().st_size, 200_000)
        self.assertAllowed(read(img))
        self.assertFalse(self.cache.exists() and any(f.is_file() for f in self.cache.iterdir()))

    def test_tiny_image_skips_the_probe(self):
        img = self.work / "icon.png"
        make_png(img, 64, 64)
        self.assertLess(img.stat().st_size, 200_000)
        self.assertAllowed(read(img))
        self.assertAllowed(read(img))  # and is never recorded — see below

    def test_allowed_images_are_exempt_from_the_reread_guard(self):
        # Both image-allow paths return before save_state and before the text
        # branch, so an under-threshold image is re-readable without limit. The
        # re-read guard covers text only.
        img = self.work / "icon.png"
        make_png(img, 64, 64)
        for _ in range(3):
            self.assertAllowed(read(img))
        self.assertFalse(Path(self.sessions, "sess1.json").exists())

    @unittest.skipUnless(HAVE_SIPS, "image branch shells out to sips (macOS)")
    def test_oversized_image_rearms_after_the_retry(self):
        # The deny is per-attempt, not per-session: retry clears pending_retry,
        # so the read after that is denied again.
        img = self.work / "shot.png"
        make_png(img, 1500, 1500)
        self.assertDenied(read(img), "oversized image")
        self.assertAllowed(read(img))
        self.assertDenied(read(img), "oversized image")

    # --- text branch ------------------------------------------------------

    def test_first_read_allowed_and_recorded(self):
        f = self.text_file()
        self.assertAllowed(read(f))
        state = json.loads(Path(self.sessions, "sess1.json").read_text())
        self.assertEqual(state[str(f)]["mtime"], os.stat(f).st_mtime)
        self.assertFalse(state[str(f)]["pending_retry"])

    def test_unchanged_reread_denied_once_then_retry_allowed(self):
        f = self.text_file()
        self.assertAllowed(read(f))
        self.assertDenied(read(f), "redundant re-read")
        self.assertAllowed(read(f))
        # And re-arms, same as the image branch.
        self.assertDenied(read(f), "redundant re-read")

    def test_changed_file_allowed(self):
        f = self.text_file()
        self.assertAllowed(read(f))
        os.utime(f, (time.time() - 100, time.time() - 100))  # deterministic delta
        self.assertAllowed(read(f))

    def test_reread_outside_the_window_allowed(self):
        f = self.text_file()
        # Control first: the same seeding mechanism inside the window must deny,
        # or "allowed" below proves nothing about the window.
        self.seed_state(f, -60)
        self.assertDenied(read(f), "redundant re-read")
        self.seed_state(f, -700)  # REREAD_WINDOW is 600s
        self.assertAllowed(read(f))

    def test_state_is_per_session(self):
        f = self.text_file()
        self.assertAllowed(read(f, session="sess1"))
        self.assertAllowed(read(f, session="sess2"))
        self.assertDenied(read(f, session="sess2"), "redundant re-read")

    def test_bounded_reads_are_exempt(self):
        f = self.text_file()
        for extra in ({"offset": 1, "limit": 20}, {"limit": 20}, {"offset": 1}):
            self.assertAllowed(read(f, **extra), extra)
            self.assertAllowed(read(f, **extra), extra)

    def test_pdf_is_exempt(self):
        f = self.text_file("doc.pdf", body="%PDF-1.4\n" + "x" * 100)
        self.assertAllowed(read(f))
        self.assertAllowed(read(f))

    # --- fail-open --------------------------------------------------------

    def test_guard_off_allows_everything(self):
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / ".guard-off").touch()
        f = self.text_file()
        self.assertAllowed(read(f))
        self.assertAllowed(read(f))
        if HAVE_SIPS:
            img = self.work / "shot.png"
            make_png(img, 1500, 1500)
            self.assertAllowed(read(img))

    def test_malformed_payloads_fail_open(self):
        for raw in ("", "not json", "[]", "null", '{"tool_name": "Read"}',
                    '{"tool_name": "Read", "tool_input": "a string"}',
                    '{"tool_name": "Read", "tool_input": {"file_path": ""}}',
                    '{"tool_name": "Read", "tool_input": {"file_path": 42}}'):
            self.assertEqual(self.run_hook(None, raw=raw), (0, None), raw)

    def test_wrong_shaped_state_file_fails_open(self):
        f = self.text_file()
        self.sessions.mkdir(parents=True, exist_ok=True)
        state = Path(self.sessions, "sess1.json")
        for body in ("", "{", "[]", "null", '"x"', "123",
                     json.dumps({str(f): "not a dict"}),
                     json.dumps({str(f): None})):
            state.write_text(body)
            self.assertAllowed(read(f), body)

    def test_missing_file_allowed(self):
        self.assertAllowed(read(self.work / "nope.txt"))
        self.assertAllowed(read(self.work / "nope.png"))

    def test_non_read_tools_ignored(self):
        f = self.text_file()
        self.assertAllowed(read(f))
        for tool in ("Edit", "Write", "Bash", "Grep", "Agent", "mcp__x__y"):
            p = read(f)
            p["tool_name"] = tool
            self.assertAllowed(p, tool)
            self.assertAllowed(p, tool)
        # ...and the Read that follows them still sees the original record.
        self.assertDenied(read(f), "redundant re-read")


if __name__ == "__main__":
    unittest.main()
