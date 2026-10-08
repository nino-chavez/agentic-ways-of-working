"""A stream-json SDK host shaped like the desktop app's Code tab, for proving the
Blast Radius hold where no surface draws the pane.

The desktop app starts the engine with --input-format/--output-format stream-json
and --permission-prompt-tool stdio, so $.ui.ask reaches it as a can_use_tool
request for AskUserQuestion. This host answers that request the way a person
would, or plays plain `claude -p`, and prints what came back.

usage: sdk_host.py <mode> <plugin_dir> <cwd> <prompt_file>
  mode  proceed    answer the question with Proceed
        deny-late  dismiss it after 1.5 s, as a person would
        deny       dismiss it at once, as a host that refuses questions would
        none       leave it unanswered (set BLAST_RADIUS_HOLD_SECONDS=5)
        allow-all  approve it unchanged, with no answer, as an SDK script that
                   allows every permission request does
        bare       plain `claude -p`: no question dialog at all

The user's settings are skipped and CLAUDE_CODE_PLUGIN_DIRS is unset, so only
<plugin_dir> loads. Runs Haiku; a run costs about a cent.
"""
import json
import os
import subprocess
import sys
import threading
import time

mode, plugin_dir, cwd, prompt_file = sys.argv[1:5]
prompt = open(prompt_file).read().strip()
env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_PLUGIN_DIRS"}
args = ["claude", "--model", "haiku", "--plugin-dir", plugin_dir,
        "--setting-sources", "project,local", "--permission-mode", "auto",
        "--output-format", "stream-json", "--verbose", "--max-turns", "3"]
if mode == "bare":
    args += ["-p", prompt]
else:
    args += ["--input-format", "stream-json", "--permission-prompt-tool", "stdio"]

t0 = time.time()


def say(*parts):
    print(f"[{time.time() - t0:5.1f}s]", *parts, flush=True)


# stderr goes to a file: an unread pipe can fill and stall the engine.
stderr_path = os.path.join(cwd, "claude-stderr.log")
stderr_file = open(stderr_path, "w")
proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=stderr_file, text=True, bufsize=1)


def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


if mode == "bare":
    proc.stdin.close()
else:
    send({"type": "user", "session_id": "", "parent_tool_use_id": None,
          "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
killer = threading.Timer(170, proc.kill)
killer.start()

for line in proc.stdout:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    kind = msg.get("type")
    if kind == "system" and msg.get("subtype") == "ui_log":
        say("ui_log", msg.get("plugin"), msg.get("text"))
    elif kind == "control_request":
        req, rid = msg.get("request", {}), msg.get("request_id")
        if req.get("subtype") != "can_use_tool":
            send({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": {}}})
            continue
        tool, inp = req.get("tool_name"), req.get("input") or {}
        if tool != "AskUserQuestion":
            send({"type": "control_response", "response": {"subtype": "success", "request_id": rid,
                                                           "response": {"behavior": "allow", "updatedInput": inp}}})
            continue
        question = inp["questions"][0]["question"]
        say("asked:", question)
        if mode == "none":
            say("left unanswered")
            continue
        if mode == "proceed":
            resp = {"behavior": "allow", "updatedInput": {**inp, "answers": {question: "Proceed"}}}
        elif mode == "allow-all":
            resp = {"behavior": "allow", "updatedInput": inp}
        else:
            if mode == "deny-late":
                time.sleep(1.5)
            resp = {"behavior": "deny", "message": "The user dismissed the question."}
        send({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": resp}})
    elif kind == "user":
        for block in msg.get("message", {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                content = content if isinstance(content, str) else json.dumps(content)
                say("tool result:", "error" if block.get("is_error") else "ok", content)
    elif kind == "result":
        say("result:", msg.get("subtype"))
        if mode != "bare":
            proc.stdin.close()
        break

killer.cancel()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
stderr_file.close()
errors = open(stderr_path).read().strip()
if errors:
    say("engine stderr (last 400 chars):", errors[-400:])
