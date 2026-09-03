"""End-to-end MCP protocol test: initialize -> initialized -> tools/list -> tools/call.

Run with: venv/Scripts/python.exe test_final.py
"""
import sys, json, subprocess

proc = subprocess.Popen(
    [r"C:/PROJECT/project-memory/venv/Scripts/python.exe", r"C:/PROJECT/project-memory/server.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

def send(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()

def recv():
    line = proc.stdout.readline()
    return json.loads(line) if line.strip() else None

# 1. initialize
send({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"}
    }
})
init_resp = recv()
assert init_resp and "result" in init_resp, f"initialize failed: {init_resp}"
print("✅ initialize OK —", init_resp["result"]["serverInfo"])

# 2. initialized notification (REQUIRED before any tool call)
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

# 3. tools/list
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
tools_resp = recv()
assert tools_resp and "result" in tools_resp, f"tools/list failed: {tools_resp}"
tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
print(f"✅ tools/list OK — {len(tool_names)} tools registered")

new_tools = [
    "memory_get_context",
    "memory_get_context_optimized",
    "memory_get_context_for_agent",
    "memory_get_dependency_map",
]
print("\n=== New LLM-priming tools ===")
all_present = True
for tool in new_tools:
    present = tool in tool_names
    all_present = all_present and present
    print(f"  {tool}: {'✅ REGISTERED' if present else '❌ MISSING'}")

# 4. tools/call — memory_stats
send({
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": "memory_stats", "arguments": {}}
})
call_resp = recv()
if call_resp and "result" in call_resp:
    print("\n✅ tools/call memory_stats OK")
    content = call_resp["result"].get("content", [])
    if content and content[0].get("type") == "text":
        stats = json.loads(content[0]["text"])
        print(f"   total_active={stats.get('total_active')}, edges={stats.get('total_edges')}")
else:
    print(f"\n❌ tools/call failed: {call_resp}")
    all_present = False

proc.terminate()
print("\n" + ("✅ End-to-end MCP test PASSED" if all_present else "❌ End-to-end MCP test FAILED"))
sys.exit(0 if all_present else 1)

