"""End-to-end MCP test: tools/call memory_query over a proper handshake.

Run with: venv/Scripts/python.exe test_call_tool.py
"""
from _test_lib import McpTestClient, tool_payload_text, report

client = McpTestClient()
try:
    init = client.initialize()
    report("serverInfo" in init, "initialize handshake")

    result = client.call_tool("memory_query", {"text": "planner", "limit": 3})
    payload = tool_payload_text(result)
    report("count" in payload and "results" in payload, "memory_query returns count/results")
    print(f"  memory_query('planner') -> {payload['count']} results")
finally:
    client.close()

print("PASS end-to-end memory_query")

