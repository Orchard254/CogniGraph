"""Guardrail for deliberate changes to CogniGraph's public MCP tool surface.

Updating EXPECTED_PUBLIC_TOOLS is intentionally a reviewed action. It is NOT
an instruction to increase the list when a test fails: first demonstrate why
an existing tool, optional parameter, plugin hook, or internal function is
insufficient. See docs/TOOL_GOVERNANCE.md.
"""
import ast
import unittest
from pathlib import Path


EXPECTED_PUBLIC_TOOLS = frozenset({
    "memory_acquire_lock",
    "memory_audit_disagreements",
    "memory_blast_radius",
    "memory_check_hygiene",
    "memory_check_safety",
    "memory_check_security",
    "memory_checkpoint_task",
    "memory_deprecate",
    "memory_flag_fact",
    "memory_get_context",
    "memory_get_file_context",
    "memory_get_intelligent_context",
    "memory_lock_status",
    "memory_prepare_task",
    "memory_project_shape",
    "memory_query",
    "memory_rate_context",
    "memory_read_code",
    "memory_record_task_outcome",
    "memory_release_lock",
    "memory_relevance_feedback_stats",
    "memory_resolve_conflict",
    "memory_search_symbols",
    "memory_stats",
    "memory_supersede",
    "memory_task_outcome_review_queue",
    "memory_validate_constraints",
    "memory_who_calls",
    "memory_who_inherits",
    "memory_write",
    "memory_write_batch",
})


def declared_mcp_tools():
    """Parse public tool declarations without importing or bootstrapping MCP."""
    source = Path(__file__).with_name("server.py").read_text(encoding="utf-8")
    module = ast.parse(source, filename="server.py")
    names = []
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            func = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "tool"
                and isinstance(func.value, ast.Name)
                and func.value.id == "mcp"
            ):
                names.append(node.name)
                break
    return names


class PublicMcpToolSurfaceTests(unittest.TestCase):
    def test_public_mcp_tools_have_explicit_review_baseline(self):
        names = declared_mcp_tools()
        self.assertEqual(len(names), len(set(names)), "Duplicate MCP tool names")
        observed = set(names)
        added = sorted(observed - EXPECTED_PUBLIC_TOOLS)
        removed = sorted(EXPECTED_PUBLIC_TOOLS - observed)
        self.assertEqual(
            (added, removed), ([], []),
            "Public MCP surface changed without updating its reviewed baseline. "
            "See docs/TOOL_GOVERNANCE.md; update this list only after documenting "
            "the unique use case, alternatives, compatibility, and evaluation.",
        )

    def test_reviewed_baseline_is_current(self):
        self.assertEqual(len(EXPECTED_PUBLIC_TOOLS), 31)


if __name__ == "__main__":
    unittest.main()
