"""End-to-end MCP test: MEMORY_DB_PATH env override is honored (db_path in stats).

Run with: venv/Scripts/python.exe test_env.py
"""
import os
import shutil
import tempfile

from _test_lib import McpTestClient, tool_payload_text, report

tmp_root = tempfile.mkdtemp(prefix="pm_env_test_")
db_path = os.path.join(tmp_root, "memory.db").replace("\\", "/")
env = os.environ.copy()
env["MEMORY_DB_PATH"] = db_path

client = McpTestClient(env=env)
try:
    client.initialize()
    payload = tool_payload_text(client.call_tool("memory_stats", {}))
    actual = payload.get("db_path", "").replace("\\", "/")
    report(actual == db_path, f"MEMORY_DB_PATH honored (db_path={actual})")
finally:
    client.close()
    shutil.rmtree(tmp_root, ignore_errors=True)

print("PASS env override test")

