#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json --output-format stream-json`.

Speaks just enough of the wire that `scribe.driver` cannot tell the
difference, and writes the transcript rows a real child would, so the daemon's
watcher sees the turn land. Everything it is told is recorded in the file
named by ``FAKE_CLAUDE_LOG`` (one JSON object per line) so a test can assert
on argv, cwd and env.

Prompt markers steer it:

    PERMIT   ask the host `can_use_tool` for a Bash call before replying
    SLOW     take two seconds over the turn (queueing, interrupt)
    DIE      exit with an error mid-turn
"""

import json
import os
import sys
import threading
import time
import uuid


def log(**entry):
    path = os.environ.get("FAKE_CLAUDE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def emit(frame):
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def arg(name, default=""):
    argv = sys.argv
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


SESSION = arg("--resume") or arg("--session-id") or str(uuid.uuid4())
MODE = arg("--permission-mode", "default")
if MODE == "manual":
    MODE = "default"
MODEL = arg("--model", "claude-fake-1")
CWD = os.getcwd()

log(event="start", argv=sys.argv[1:], cwd=CWD, env={k: v for k, v in os.environ.items() if k.startswith(("CLAUDE", "SCRIBE"))})

state = {"mode": MODE, "model": MODEL, "interrupted": False, "turn": 0}
pending_answers = {}
answers_lock = threading.Lock()


def transcript_path():
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    folder = os.path.join(home, "projects", CWD.replace("/", "-"))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, SESSION + ".jsonl")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def write_rows(rows):
    with open(transcript_path(), "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def base(**extra):
    row = {"sessionId": SESSION, "cwd": CWD, "version": "2.1.272", "gitBranch": "main", "isSidechain": False, "userType": "external"}
    row.update(extra)
    return row


def init_frame():
    return {
        "type": "system",
        "subtype": "init",
        "cwd": CWD,
        "session_id": SESSION,
        "tools": ["Bash", "Read", "Edit"],
        "model": state["model"],
        "permissionMode": state["mode"],
        "slash_commands": ["fake-skill", "compact", "plugin:cmd"],
        "terminal_slash_commands": ["doctor"],
        "skills": ["fake-skill"],
        "agents": ["Explore"],
        "messaging_socket_path": "",
    }


def ask_permission(command):
    rid = str(uuid.uuid4())
    done = threading.Event()
    with answers_lock:
        pending_answers[rid] = (done, [])
    emit(
        {
            "type": "control_request",
            "request_id": rid,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "display_name": "Bash",
                "input": {"command": command, "description": "fake"},
                "description": "fake",
                "tool_use_id": "toolu_fake_" + rid[:8],
                "permission_suggestions": [],
            },
        }
    )
    done.wait(300)
    with answers_lock:
        _, slot = pending_answers.pop(rid, (None, []))
    return slot[0] if slot else {"behavior": "deny", "message": "no answer"}


def turn(content):
    state["turn"] += 1
    state["interrupted"] = False
    text = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
    images = sum(1 for b in content if b.get("type") == "image")
    log(event="user", text=text, images=images, mode=state["mode"], model=state["model"])
    emit(init_frame())
    user = base(type="user", uuid=str(uuid.uuid4()), timestamp=now(), permissionMode=state["mode"],
                message={"role": "user", "content": content})
    write_rows([user])
    if "PERMIT" in text:
        decision = ask_permission("touch fake.txt")
        log(event="permission", decision=decision)
        outcome = "ran" if decision.get("behavior") == "allow" else "refused"
        reply = f"the command was {outcome}"
    elif "DIE" in text:
        sys.stderr.write("fake claude: dying on request\n")
        sys.stderr.flush()
        os._exit(3)
    else:
        reply = "echo: " + text
    if "SLOW" in text:
        for _ in range(20):
            if state["interrupted"]:
                break
            time.sleep(0.1)
    if state["interrupted"]:
        write_rows([base(type="user", uuid=str(uuid.uuid4()), timestamp=now(),
                         message={"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]})])
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": SESSION})
        return
    assistant = base(type="assistant", uuid=str(uuid.uuid4()), timestamp=now(),
                     message={"role": "assistant", "model": state["model"], "content": [{"type": "text", "text": reply}],
                              "usage": {"input_tokens": 5, "output_tokens": 5, "cache_read_input_tokens": 0}})
    write_rows([assistant])
    emit({"type": "assistant", "message": assistant["message"], "session_id": SESSION})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": SESSION,
          "duration_ms": 10, "total_cost_usd": 0.0})


def control(rid, request):
    sub = request.get("subtype")
    log(event="control", subtype=sub, request=request)
    if sub == "initialize":
        response = {"commands": [
            {"name": "fake-skill", "description": "A fake skill (user)", "argumentHint": "<topic>"},
            {"name": "compact", "description": "Compact the conversation", "argumentHint": ""},
            {"name": "plugin:cmd", "description": "A plugin command", "argumentHint": ""},
        ], "output_style": "default"}
    elif sub == "set_permission_mode":
        state["mode"] = request.get("mode") or state["mode"]
        response = {"mode": state["mode"]}
        emit({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": response}})
        emit({"type": "system", "subtype": "status", "status": None, "permissionMode": state["mode"], "session_id": SESSION})
        return
    elif sub == "set_model":
        state["model"] = request.get("model") or "claude-fake-1"
        response = {}
    elif sub == "interrupt":
        state["interrupted"] = True
        response = {"still_queued": []}
    elif sub == "get_context_usage":
        response = {"categories": [{"name": "System prompt", "tokens": 1000, "kind": "used"}], "max": 200000}
    elif sub == "file_suggestions":
        q = request.get("query") or ""
        response = {"suggestions": [{"path": p} for p in ("src/app.js", "src/main.py", "README.md") if q.lower() in p.lower()]}
    else:
        emit({"type": "control_response", "response": {"subtype": "error", "request_id": rid, "error": f"unsupported {sub}"}})
        return
    emit({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": response}})


def main():
    worker = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        frame = json.loads(line)
        kind = frame.get("type")
        if kind == "control_request":
            threading.Thread(target=control, args=(frame.get("request_id"), frame.get("request") or {}), daemon=True).start()
        elif kind == "control_response":
            reply = frame.get("response") or {}
            with answers_lock:
                waiter = pending_answers.get(str(reply.get("request_id")))
            if waiter:
                waiter[1].append(reply.get("response") or {})
                waiter[0].set()
        elif kind == "user":
            content = (frame.get("message") or {}).get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if worker is not None:
                worker.join()
            worker = threading.Thread(target=turn, args=(content,), daemon=True)
            worker.start()
    if worker is not None:
        worker.join(5)
    log(event="eof")


if __name__ == "__main__":
    main()
