"""Shared MCP stdio test harness used by the standalone test_*.py scripts.

Spawns server.py, performs the correct MCP handshake
(initialize -> notifications/initialized -> request), reads exactly one
JSON-RPC response per request, and exits non-zero on any protocol error so
the scripts can be run unattended.
"""
import json
import os
import queue
import subprocess
import sys
import threading

SERVER = os.environ.get("PROJECT_MEMORY_SERVER") or r"C:/PROJECT/project-memory/server.py"
PYTHON = os.environ.get("PROJECT_MEMORY_PYTHON") or r"C:/PROJECT/project-memory/venv/Scripts/python.exe"


class McpTestClient:
    def __init__(self, env=None, timeout=20):
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [PYTHON, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        # Drain stderr in the background so a verbose server can never
        # block on a full pipe buffer while we wait on stdout.
        self._stderr_lines = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self._next_id = 1

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr_lines.append(line)
        except Exception:
            pass

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _recv(self):
        # readline() blocks forever on a hung server; enforce self.timeout
        # with a reader thread (select() doesn't work on Windows pipes).
        q = queue.Queue()

        def _read():
            try:
                q.put(self.proc.stdout.readline())
            except Exception as e:  # noqa: BLE001
                q.put(e)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(self.timeout)
        if t.is_alive():
            raise RuntimeError(f"server did not respond within {self.timeout}s")
        line = q.get()
        if isinstance(line, Exception):
            raise RuntimeError(f"failed reading server stdout: {line!r}")
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
            err = resp["error"]
            raise RuntimeError(f"{method} failed: {err}")
        return resp.get("result", {})

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
        finally:
            # Release pipes and reap the stderr-drain thread so large suites
            # don't accumulate handles/threads across clients.
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
            try:
                self._stderr_thread.join(timeout=5)
            except Exception:
                pass


def tool_payload_text(call_result):
    """Extract the first text payload from a tools/call result and parse as JSON."""
    content = call_result.get("content", []) if isinstance(call_result, dict) else []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            return json.loads(item["text"])
    raise RuntimeError(f"no text content in tools/call result: {call_result}")


def report(ok, label):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        sys.exit(1)

