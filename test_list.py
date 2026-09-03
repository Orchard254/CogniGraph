"""End-to-end MCP test: tools/list returns the full tool inventory.

Run with: venv/Scripts/python.exe test_list.py
"""
from _test_lib import McpTestClient, report

client = McpTestClient()
try:
    client.initialize()
    tools = client.list_tools()
    report(len(tools) >= 30, f"tools/list returned {len(tools)} tools")
    for t in sorted(tools, key=lambda x: x["name"]):
        print(f"  - {t['name']}")
finally:
    client.close()

print("PASS tools/list")

