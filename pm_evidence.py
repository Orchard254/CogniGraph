"""Task requirements, evidence coverage and next-action guidance.

This module is intentionally stdlib-only. It is a *controller*, not a new
knowledge store: no model call, new schema, index, or inferred truth. The
controller turns a task into explicit evidence needs before fact selection,
rewards genuinely different types of relevant evidence, and reports gaps
without promoting retrieved claims to verified source behavior.
"""
from __future__ import annotations

import re
from typing import Any

# These are *information needs*, not claims that the project has the evidence.
# A category match can offer a reference, never establish that source behavior
# or a test outcome is true.
_REQUIREMENTS = {
    "behavior": ("Locate the behavior", frozenset({"component", "state"})),
    "constraints": ("Protect existing decisions and constraints",
                    frozenset({"constraint", "decision", "convention"})),
    "dependencies": ("Inspect affected callers and dependencies", frozenset()),
    "failure": ("Observe or reproduce the reported failure", frozenset()),
    "verification": ("Verify the change with observed results", frozenset()),
}
_PHASE_NEEDS = {
    "orient": ("behavior", "constraints"),
    "inspect": ("behavior", "dependencies"),
    "edit": ("behavior", "constraints", "dependencies", "verification"),
    "debug": ("behavior", "failure", "constraints", "dependencies", "verification"),
    "verify": ("behavior", "verification"),
    "handoff": ("behavior", "verification"),
}
_FAILURE_WORDS = re.compile(
    r"\b(fix|bug|broken|fail(?:ed|ure|ing)?|crash|exception|regression|error|debug)\b", re.I
)
_CHANGE_WORDS = re.compile(
    r"\b(add|implement|build|create|change|modify|refactor|replace|remove|improve|"
    r"optimi[sz]e|migrate|upgrade|rewrite)\b", re.I
)


def required_evidence(task: str, phase: str) -> tuple[str, ...]:
    """Expand orient into a task-aware investigation, not a keyword-only search.

    IDE-observed inspect/edit/verify phases take precedence over task text.
    Reproduction and verification are independent requirements; stored facts
    cannot satisfy them just by claiming a test ran.
    """
    if phase not in _PHASE_NEEDS:
        raise ValueError("unknown evidence phase")
    if phase == "orient" and _FAILURE_WORDS.search(task):
        return _PHASE_NEEDS["debug"]
    if phase == "orient" and _CHANGE_WORDS.search(task):
        return _PHASE_NEEDS["edit"]
    return _PHASE_NEEDS[phase]


def evidence_bonus(
    fact: dict[str, Any],
    needs: tuple[str, ...],
    covered: set[str],
) -> int:
    """Marginal value of *new* evidence, after baseline search relevance.

    Applied during selection, not at query time: the first relevant component
    is worth more than the fifth near-duplicate. Never reward an unanchored
    'other' fact as if it were implementation evidence.
    """
    category = str(fact.get("category") or "")
    anchored = bool(fact.get("file_path"))
    if fact.get("source", "memory") != "memory":
        return 0
    if "behavior" in needs and "behavior" not in covered:
        if category in _REQUIREMENTS["behavior"][1] and anchored:
            return 20
    if "constraints" in needs and "constraints" not in covered:
        if category in _REQUIREMENTS["constraints"][1]:
            return 20
    return 0


def evidence_type(fact: dict[str, Any]) -> str | None:
    """Coverage classification for one actually delivered fact."""
    category = str(fact.get("category") or "")
    if category in _REQUIREMENTS["behavior"][1] and fact.get("file_path"):
        return "behavior"
    if category in _REQUIREMENTS["constraints"][1]:
        return "constraints"
    return None


def evaluate_evidence(
    task: str,
    phase: str,
    selected: list[dict[str, Any]],
    inspection_targets: list[dict[str, str]],
) -> dict[str, Any]:
    """Evaluate only *delivered* facts and *rendered* inspection leads.

    A memory receipt or an indexed symbol is never equivalent to an observed
    current source read. Verification requires actual tool/test evidence and
    cannot be inferred from this read-only briefing.
    """
    needs = required_evidence(task, phase)
    anchored = [
        fact for fact in selected
        if fact.get("category") in _REQUIREMENTS["behavior"][1]
        and fact.get("file_path")
        and fact.get("freshness") != "SOURCE_MISSING"
    ]
    protected = [
        fact for fact in selected
        if fact.get("category") in _REQUIREMENTS["constraints"][1]
    ]
    leads = [target["file_path"] for target in inspection_targets
             if target.get("file_path")]
    linked = [target["file_path"] for target in inspection_targets
              if target.get("kind") == "linked_fact" and target.get("file_path")]
    requirements = []
    for key in needs:
        label, _ = _REQUIREMENTS[key]
        refs: list[str] = []
        paths: list[str] = []
        status = "missing"
        if key == "behavior":
            refs = [f["ref"] for f in anchored][:3]
            paths = list(dict.fromkeys(
                [f["file_path"] for f in anchored] + leads
            ))[:3]
            status = "reference_available" if refs else ("inspection_lead" if paths else "missing")
        elif key == "constraints":
            refs = [f["ref"] for f in protected][:3]
            status = "reference_available" if refs else "missing"
        elif key == "dependencies":
            paths = list(dict.fromkeys(linked))[:3]
            status = "inspection_lead" if paths else "missing"
        # No selected memory can prove a failure was reproduced or tests ran.
        requirements.append({
            "id": key, "label": label, "status": status,
            "refs": refs, "paths": paths,
        })

    # Immediate action is deliberately narrower than the entire task plan.
    # Lead paths must have been validated and rendered by the caller.
    behavior = next((r for r in requirements if r["id"] == "behavior"), None)
    if behavior and behavior["paths"]:
        next_action = "Inspect current source at " + behavior["paths"][0] + " before relying on stored behavior."
    elif behavior:
        next_action = "Locate the current implementation in source before editing or assuming behavior."
    else:
        missing = next((r for r in requirements if r["status"] == "missing"), None)
        next_action = {
            "failure": "Reproduce the failure and capture the observed result.",
            "dependencies": "Inspect callers, imports, and related code before editing.",
            "verification": "Run relevant checks and record their observed outcomes.",
            "constraints": "Check current code, tests, and decisions for restrictions.",
        }.get(missing["id"] if missing else "", "Inspect current source and verify assumptions.")
    return {
        "requirements": requirements,
        "next_action": next_action,
        "verified": False,
        "unresolved_count": sum(r["status"] == "missing" for r in requirements),
    }


def format_evidence_plan(plan: dict[str, Any], *, max_lines: int = 3) -> list[str]:
    """Small non-authoritative text projection; structured state remains full."""
    lines = ["\n### Evidence requirements (not proof)\n"]
    for item in plan["requirements"][:max_lines]:
        status = item["status"].replace("_", " ")
        lines.append(f"- {item['label']}: {status}\n")
    lines.append("Next: " + plan["next_action"] + "\n")
    return lines
