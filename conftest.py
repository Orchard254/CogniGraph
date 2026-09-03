# The standalone test_*.py scripts in this repo are end-to-end MCP subprocess
# tests meant to be run directly (python test_x.py), not collected by pytest.
# Exclude them so `pytest` only collects the real unit-test modules.
collect_ignore = [
    "test_final.py",
    "test_call_tool.py",
    "test_env.py",
    "test_env2.py",
    "test_list.py",
    "test_list2.py",
    "test_methods.py",
    "test_tools_call.py",
]

