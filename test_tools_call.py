"""End-to-end MCP test: tools/call memory_stats with proper handshake.

Run with: venv/Scripts/python.exe test_tools_call.py
"""
from _test_lib import McpTestClient, tool_payload_text, report

client = McpTestClient()
try:
    client.initialize()
    result = client.call_tool("memory_stats", {})
    payload = tool_payload_text(result)
    report("total_active" in payload, f"memory_stats returned total_active={payload.get('total_active')}")
finally:
    client.close()

print("PASS tools/call memory_stats")

