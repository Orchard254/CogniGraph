"""End-to-end MCP test: initialize + tools/list + tools/call in one session.

Run with: venv/Scripts/python.exe test_methods.py
"""
from _test_lib import McpTestClient, tool_payload_text, report

client = McpTestClient()
try:
    init = client.initialize()
    report("serverInfo" in init, "initialize handshake")

    tools = client.list_tools()
    report(len(tools) >= 30, f"tools/list returned {len(tools)} tools")

    stats = tool_payload_text(client.call_tool("memory_stats", {}))
    report("total_active" in stats, "memory_stats tool call")
    print(f"  total_active={stats['total_active']}, edges={stats['total_edges']}")
finally:
    client.close()

print("PASS full method sequence")

