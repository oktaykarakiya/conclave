"""W24 end-to-end acceptance: the REAL opencode CLI behind Conclave's provider, against a fake
OpenAI-compatible model server, in temporary owner homes.

The fake model answers the first request with one `bash` tool call (a probe script) and every
later request with "done". The script checks what really happened:
  - the model was called with the dispatch provider's key only;
  - the system prompt holds neither the owner's global AGENTS.md nor ~/.claude/CLAUDE.md;
  - the owner's opencode folders are byte-identical afterwards (no session, log, lock);
  - the session database lives under $CONCLAVE_HOME;
  - the worker's shell command saw the owner's XDG values and tool entries.

Usage: PYTHONPATH=src python docs/self-optimization/e2e/w24_opencode_e2e.py <workdir>
       [--sandbox auto|off]
Needs the opencode CLI ($CONCLAVE_OPENCODE_BIN, else `opencode` on PATH) and, for the
sandbox, bubblewrap. No real credential or model is used: the keys are fake and the model
is a local fake server. <workdir> is deleted and recreated. Prints a JSON report; exit 0
when every check passes. On W24's parent commit it fails: the owner's AGENTS.md reaches
the prompt, the owner's opencode folders are written (sessions, log, snapshots, and
opencode rewrites the owner's opencode.json) and the worker can read every credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

OPENCODE = os.environ.get("CONCLAVE_OPENCODE_BIN") or shutil.which("opencode") or "opencode"
REAL_CACHE = Path.home() / ".cache" / "opencode"
FAKE_KEY = "sk-e2e-fakellm-key"
OTHER_KEY = "sk-e2e-otherllm-key"
RULES = "E2E OWNER GLOBAL RULES SENTINEL"
CLAUDE_MD = "E2E OWNER CLAUDE MD SENTINEL"
KINDS = {"config": "XDG_CONFIG_HOME", "data": "XDG_DATA_HOME", "state": "XDG_STATE_HOME",
         "cache": "XDG_CACHE_HOME"}

REQUESTS: list[dict[str, Any]] = []
LOCK = threading.Lock()


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # quiet
        return

    def do_GET(self) -> None:
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        with LOCK:
            REQUESTS.append({"path": self.path, "auth": self.headers.get("Authorization"),
                             "body": body})
        tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
        answered = any(m.get("role") == "tool" for m in body.get("messages", []))
        if "bash" in tools and not answered:
            delta = {"role": "assistant", "content": None, "tool_calls": [{
                "index": 0, "id": "call_probe", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({
                    "command": f"bash {PROBE}", "description": "Run the probe"})}}]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "done"}
            finish = "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not body.get("stream"):
            message = {k: v for k, v in delta.items() if k != "tool_calls"}
            if "tool_calls" in delta:
                message["tool_calls"] = [{k: v for k, v in c.items() if k != "index"}
                                         for c in delta["tool_calls"]]
            payload = {"id": "c1", "object": "chat.completion", "created": 0, "model": "m1",
                       "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                       "usage": usage}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        chunks = [
            {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m1",
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m1",
             "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage},
        ]
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    (repo / "README.md").write_text("e2e\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=e2e", "-c", "user.email=e2e@localhost", "commit",
                    "-q", "-m", "init"], cwd=repo, check=True)


def snapshot(roots: list[Path], skip: Path) -> dict[str, str]:
    digest: dict[str, str] = {}
    for root in roots:
        for folder, dirs, names in os.walk(root):
            here = Path(folder).resolve()
            if here == skip or skip in here.parents:
                dirs[:] = []
                continue
            for name in names:
                p = Path(folder) / name
                digest[str(p)] = ("link:" + os.readlink(p)) if p.is_symlink() else (
                    hashlib.sha256(p.read_bytes()).hexdigest())
    return digest


async def main() -> int:
    work = Path(sys.argv[1]).resolve()
    mode = sys.argv[sys.argv.index("--sandbox") + 1] if "--sandbox" in sys.argv else "auto"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    home = work / "owner-home"
    home.mkdir()
    homes = {k: work / "owner-xdg" / k for k in KINDS}
    for kind, path in homes.items():
        (path / "w24tool").mkdir(parents=True)
        (path / "w24tool" / "marker").write_text(f"{kind}-marker")
    conclave_home = homes["data"] / "conclave"
    out = work / "probe-out"
    out.mkdir()
    global PROBE
    PROBE = work / "probe.sh"
    PROBE.write_text(
        "set +e\n"
        f"OUT={out}\n"
        "env | sort > $OUT/env.txt\n"
        "for k in CONFIG DATA STATE CACHE; do v=XDG_${k}_HOME; "
        "cat \"${!v}/w24tool/marker\" > $OUT/marker_$k.txt 2>&1; done\n"
        "cat \"$XDG_DATA_HOME/opencode/auth.json\" > $OUT/auth.txt 2>&1\n"
        "cat \"$XDG_CONFIG_HOME/opencode/opencode.json\" > $OUT/ocjson.txt 2>&1\n"
        "cat \"$HOME/.claude/CLAUDE.md\" > $OUT/claude.txt 2>&1\n"
        "ls -la \"$XDG_DATA_HOME/opencode\" > $OUT/datadir.txt 2>&1\n"
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    cfg = homes["config"] / "opencode"
    cfg.mkdir()
    (cfg / "opencode.json").write_text(json.dumps({
        "model": "fakellm/m1",
        "permission": {"e2e_owner_rule": "allow"},
        "mcp": {"owner-mcp": {"type": "local", "command": ["false"]}},
        "provider": {
            "fakellm": {"npm": "@ai-sdk/openai-compatible", "name": "Fake",
                        "options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                        "models": {"m1": {"name": "m1", "tool_call": True}}},
            "otherllm": {"npm": "@ai-sdk/openai-compatible", "name": "Other",
                         "options": {"baseURL": "http://127.0.0.1:9/v1"},
                         "models": {"x": {"name": "x"}}},
        },
    }))
    (cfg / "AGENTS.md").write_text(RULES)
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text(CLAUDE_MD)
    data = homes["data"] / "opencode"
    data.mkdir()
    auth = data / "auth.json"
    auth.write_text(json.dumps({"fakellm": {"type": "api", "key": FAKE_KEY},
                                "otherllm": {"type": "api", "key": OTHER_KEY}}))
    auth.chmod(0o600)
    cache = homes["cache"] / "opencode"
    cache.mkdir()
    for name in ("bin", "models.json"):
        src = REAL_CACHE / name
        if src.is_dir():
            shutil.copytree(src, cache / name)
        elif src.exists():
            shutil.copy2(src, cache / name)

    repo = work / "repo"
    await asyncio.to_thread(_init_repo, repo)

    for var in list(os.environ):
        if var.startswith("OPENCODE_"):
            del os.environ[var]
    os.environ.update({"HOME": str(home), "CONCLAVE_HOME": str(conclave_home),
                       "SHELL": "/bin/bash", "OPENCODE_DISABLE_AUTOUPDATE": "1",
                       "OPENCODE_DISABLE_MODELS_FETCH": "1",
                       "OPENCODE_DISABLE_LSP_DOWNLOAD": "1"})
    for kind, var in KINDS.items():
        os.environ[var] = str(homes[kind])

    from conclave.config import ArgMode
    from conclave.providers import opencode_cli
    from conclave.providers.opencode_cli import OpenCodeCliProvider
    from conclave.providers.profiles import ResolvedProfile

    opencode_cli._OPENCODE_BIN = OPENCODE  # type: ignore[attr-defined]
    extra: dict[str, Any] = {}
    try:
        from conclave.config import WorkerSandbox  # type: ignore[attr-defined]
        extra["sandbox"] = WorkerSandbox(mode)
    except ImportError:
        pass
    before = snapshot([home, *homes.values()], conclave_home.resolve())
    profile = ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit, isolated=True,
                              model="fakellm/m1", **extra)
    result = await OpenCodeCliProvider().run_agent(
        profile=profile, prompt="Run the probe, then answer done.", timeout_seconds=240,
        cwd=repo)
    after = snapshot([home, *homes.values()], conclave_home.resolve())
    server.shutdown()

    def read(name: str) -> str:
        p = out / name
        return p.read_text() if p.exists() else "<missing>"

    env = dict(line.split("=", 1) for line in read("env.txt").splitlines() if "=" in line)
    systems = [json.dumps(m) for r in REQUESTS for m in r["body"].get("messages", [])
               if m.get("role") == "system"]
    tools = sorted({t.get("function", {}).get("name") for r in REQUESTS
                    for t in r["body"].get("tools") or []})
    changed = sorted(set(before) ^ set(after) | {k for k in before if before[k] != after.get(k)})
    dbs = [str(p.relative_to(conclave_home)) for p in conclave_home.rglob("*.db")] if (
        conclave_home.exists()) else []
    checks = {
        "dispatch ok": result.ok,
        "model called": len(REQUESTS) >= 2,
        "only the dispatch key used": {r["auth"] for r in REQUESTS} == {f"Bearer {FAKE_KEY}"},
        "no owner AGENTS.md in prompt": not any(RULES in s for s in systems),
        "no owner CLAUDE.md in prompt": not any(CLAUDE_MD in s for s in systems),
        "no owner MCP tool": not any("owner" in (t or "") for t in tools),
        "owner folders untouched": not changed,
        "session db under CONCLAVE_HOME": bool(dbs),
        "command XDG unchanged": all(env.get(v) == str(homes[k]) for k, v in KINDS.items()),
        "command sees tool markers": all(
            read(f"marker_{k.upper()}.txt") == f"{k}-marker" for k in KINDS),
        "command sees only its credential": FAKE_KEY in read("auth.txt")
        and OTHER_KEY not in read("auth.txt"),
        "command sees no owner CLAUDE.md": CLAUDE_MD not in read("claude.txt"),
    }
    report = {
        "mode": mode, "result_ok": result.ok, "error": result.error,
        "text": (result.text or "")[:200], "requests": len(REQUESTS),
        "tools": tools, "changed_owner_files": changed[:20], "conclave_dbs": dbs,
        "worker_opencode_json": read("ocjson.txt")[:400], "checks": checks,
    }
    print(json.dumps(report, indent=2))
    return 0 if all(checks.values()) else 1


PROBE = Path("/nonexistent")

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
