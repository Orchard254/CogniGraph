"""Subprocess-level MCP stdio smoke test for server.py (JSON-RPC framing path).

Covers what in-process tests (test_server_tools.py, which imports server.py and
calls the @mcp.tool() functions directly) cannot: real child-process spawn,
stdio JSON-RPC framing (initialize -> notifications/initialized -> tools/list),
and clean child teardown.

Handshake pattern reused from test_final.py / test_tools_call.py
(initialize with protocolVersion 2024-11-05, then the mandatory
notifications/initialized notification before any further request).

Read-only: this test issues NO tools/call at all, so it performs zero DB
writes (not even read-only tool calls that could trigger first-time-setup
bootstrap writes on a fresh DB). The child's background services
(_start_background_services) operate on the tmp DB via MEMORY_DB_PATH.

Run: .\\venv\\Scripts\\python.exe -m pytest test_server_e2e.py -q
"""

import json
import os
import queue
import subprocess
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "server.py")
VENV_PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

# Hardening budgets: every blocking op has an absolute timeout, and the whole
# test (spawn -> handshake -> teardown) must fit inside OVERALL_BUDGET_S.
OVERALL_BUDGET_S = 60.0
READ_TIMEOUT_S = 20.0
STOP_TIMEOUT_S = 5.0


def _remaining(deadline):
    return max(0.1, deadline - time.monotonic())


def _send(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _recv(proc, timeout):
    """Read exactly one JSON-RPC line with an absolute timeout.

    select() does not work on Windows pipes, so readline() runs in a daemon
    reader thread joined with a timeout — the suite can never hang here.
    """
    q = queue.Queue()

    def _read():
        try:
            q.put(proc.stdout.readline())
        except Exception as e:  # noqa: BLE001 - transport error to caller
            q.put(e)

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError(f"server did not respond within {timeout:.1f}s")
    line = q.get()
    if isinstance(line, Exception):
        raise AssertionError(f"failed reading server stdout: {line!r}")
    if not line or not line.strip():
        raise AssertionError("server closed stdout without a response")
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        raise AssertionError(f"server sent non-JSON line {line[:200]!r}: {e}")


def test_server_stdio_framing(tmp_path, monkeypatch):
    """Spawn server.py over stdio, handshake, assert known tools registered."""
    if not os.path.isfile(VENV_PY):
        pytest.skip(f"venv python missing: {VENV_PY}")
    if not os.path.isfile(SERVER_PY):
        pytest.skip(f"server.py missing: {SERVER_PY}")

    # Unique tmp DB per run; the child inherits it via env (MemoryStore honors
    # MEMORY_DB_PATH), so the live repo DB is never touched.
    tmp_db = str(tmp_path / "e2e-smoke.db")
    monkeypatch.setenv("MEMORY_DB_PATH", tmp_db)
    env = os.environ.copy()
    env["MEMORY_DB_PATH"] = tmp_db

    # NOTE on stdin: repo DEVNULL convention (bootstrap.py, wire.py,
    # hooks/git_hook.py) applies to git/probe children that must never inherit
    # a stdio pipe. The MCP child here speaks JSON-RPC on stdin, so it
    # requires stdin=PIPE; DEVNULL would break the framing path under test.
    proc = subprocess.Popen(
        [VENV_PY, SERVER_PY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
    )
    # Drain stderr in the background so a verbose server can never block on a
    # full pipe buffer while we wait on stdout (same mitigation as _test_lib).
    stderr_lines = []

    def _drain_stderr():
        try:
            for line in proc.stderr:
                stderr_lines.append(line)
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    deadline = time.monotonic() + OVERALL_BUDGET_S
    next_id = 1
    try:
        # 1. initialize
        _send(proc, {
            "jsonrpc": "2.0",
            "id": next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test_server_e2e", "version": "1.0"},
            },
        })
        init_id = next_id
        next_id += 1
        init_resp = _recv(proc, min(READ_TIMEOUT_S, _remaining(deadline)))
        assert init_resp.get("id") == init_id, f"initialize id mismatch: {init_resp!r}"
        assert "result" in init_resp, (
            f"initialize failed: {init_resp!r} "
            f"stderr={''.join(stderr_lines)[-2000:]}"
        )

        # 2. initialized notification (REQUIRED before further requests).
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3. tools/list — read-only, triggers no DB writes.
        _send(proc, {"jsonrpc": "2.0", "id": next_id, "method": "tools/list", "params": {}})
        list_id = next_id
        next_id += 1
        tools_resp = _recv(proc, min(READ_TIMEOUT_S, _remaining(deadline)))
        assert tools_resp.get("id") == list_id, f"tools/list id mismatch: {tools_resp!r}"
        assert "result" in tools_resp, (
            f"tools/list failed: {tools_resp!r} "
            f"stderr={''.join(stderr_lines)[-2000:]}"
        )
        tools = tools_resp["result"].get("tools", [])
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        assert "memory_stats" in names, f"memory_stats not registered: {sorted(names)}"
        assert "memory_query" in names, f"memory_query not registered: {sorted(names)}"
        assert time.monotonic() < deadline, "test exceeded overall 60s budget"
    finally:
        # Clean teardown that never hangs and never leaks the child: close
        # stdin first (lets a cooperative server exit), then terminate with a
        # bounded wait, escalating to kill. All waits have absolute timeouts.
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=STOP_TIMEOUT_S)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=STOP_TIMEOUT_S)
            except Exception:
                pass
        finally:
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
            try:
                stderr_thread.join(timeout=STOP_TIMEOUT_S)
            except Exception:
                pass
