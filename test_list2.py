"""End-to-end MCP test: tools/list includes all LLM-priming tools.

Run with: venv/Scripts/python.exe test_list2.py
"""
from _test_lib import McpTestClient, report

PRIMING_TOOLS = [
    "memory_get_context",
    "memory_get_context_optimized",
    "memory_get_context_for_agent",
    "memory_get_file_context",
    "memory_get_dependency_map",
    "memory_prepare_task",
]

client = McpTestClient()
try:
    client.initialize()
    names = {t["name"] for t in client.list_tools()}
    missing = [t for t in PRIMING_TOOLS if t not in names]
    report(not missing, f"all priming tools registered (missing: {missing or 'none'})")
finally:
    client.close()

print("PASS priming tool inventory")

