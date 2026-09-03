"""Shared MCP stdio test harness used by the standalone test_*.py scripts.

Spawns server.py, performs the correct MCP handshake
(initialize -> notifications/initialized -> request), reads exactly one
JSON-RPC response per request, and exits non-zero on any protocol error so
the scripts can be run unattended.
"""
import json
import subprocess
import sys

SERVER = r"C:/PROJECT/project-memory/server.py"
PYTHON = r"C:/PROJECT/project-memory/venv/Scripts/python.exe"


class McpTestClient:
    def __init__(self, env=None, timeout=20):
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [PYTHON, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._next_id = 1

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _recv(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout without a response")
        return json.loads(line)

    def request(self, method, params=None):
        rid = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        resp = self._recv()
        if resp.get("id") != rid:
            raise RuntimeError(f"expected response id {rid}, got {resp.get('id')}")
        if "error" in resp:
            raise RuntimeError(f"{method} failed: {resp['error']}")
        return resp["result"]

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def initialize(self):
        result = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        })
        self.notify("notifications/initialized")
        return result

    def list_tools(self):
        return self.request("tools/list", {})["tools"]

    def call_tool(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def tool_payload_text(call_result):
    """Extract the first text payload from a tools/call result and parse as JSON."""
    content = call_result.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    raise RuntimeError(f"no text content in tools/call result: {call_result}")


def report(ok, label):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        sys.exit(1)

