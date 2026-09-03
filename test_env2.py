"""End-to-end MCP test: MEMORY_DB_PATH at a fresh location auto-creates the database file.

Run with: venv/Scripts/python.exe test_env2.py
"""
import os
import shutil
import tempfile

from _test_lib import McpTestClient, tool_payload_text, report

tmp_root = tempfile.mkdtemp(prefix="pm_env2_test_")
db_path = os.path.join(tmp_root, "memory.db").replace("\\", "/")
report(not os.path.exists(db_path), "db does not exist before server start")

env = os.environ.copy()
env["MEMORY_DB_PATH"] = db_path

client = McpTestClient(env=env)
try:
    client.initialize()
    payload = tool_payload_text(client.call_tool("memory_stats", {}))
    report(payload.get("total_active") is not None, "memory_stats responds on fresh db")
    report(os.path.exists(db_path), "database file auto-created on disk")
finally:
    client.close()
    shutil.rmtree(tmp_root, ignore_errors=True)

print("PASS fresh-db env test")

