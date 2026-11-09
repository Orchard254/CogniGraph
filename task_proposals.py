"""Conservative, read-only discovery of candidate coding-task goal contracts.

Candidates are NEVER registered automatically. A filename match is an
inspection lead, not proof that a test covers requested behavior. This
module discovers bounded on-disk evidence, challenges the proposed contract,
and leaves explicit adoption to task_goal's predeclared evidence gate.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from pm_common import looks_like_secret
from pm_evidence import _FAILURE_WORDS, _CHANGE_WORDS
from task_checks import classify_check
from task_evidence import safe_project_file

_MAX_FILES = 1200
_MAX_DEPTH = 5
_MAX_HINTS = 6
_MAX_TESTS = 2
_SOURCE_EXTS = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs"})
_SKIP_DIRS = frozenset({
    "node_modules", "vendor", "venv", "env", "__pycache__", "dist",
    "build", "target", "coverage", ".git", ".cognigraph",
})
_STOPWORDS = frozenset({
    "fix", "bug", "failed", "failure", "error", "the", "this", "that", "with",
    "from", "when", "into", "only", "does", "work", "make", "have",
    "code", "test", "tests", "issue", "please", "should", "feature",
    "create", "implement", "change", "modify", "improve", "update",
    "project", "app", "application", "file", "function", "class",
    "without", "after", "before", "using", "more", "some", "need",
})
_SOURCE_PATH_RE = re.compile(
    r"(?<![\w./-])(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\."
    r"(?:py|js|jsx|ts|tsx|go|rs)\b"
)
_SCRIPT_RISK = re.compile(r"[;&|><$\x60\n\r]|\b(?:echo|exit|true)\b", re.I)


def _profile(task: str) -> str:
    if _FAILURE_WORDS.search(task):
        return "repair"
    if _CHANGE_WORDS.search(task):
        return "implementation"
    return "inspection"


def _is_test(path: str) -> bool:
    name = Path(path).name.lower()
    stem = Path(path).stem.lower()
    parts = path.lower().split("/")
    return (stem.startswith("test_") or stem.endswith("_test")
            or ".test." in name or ".spec." in name
            or any(part in ("tests", "test", "__tests__") for part in parts[:-1]))


def _index_sources(root: Path) -> tuple[list[str], bool]:
    """Bounded, deterministic directory walk, never following symlinks."""
    results: list[str] = []
    visited = 0
    truncated = False
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue and visited < _MAX_FILES:
        current, depth = queue.pop(0)
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except (OSError, PermissionError):
            continue
        directories = []
        for entry in entries:
            visited += 1
            if visited > _MAX_FILES:
                truncated = True
                break
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < _MAX_DEPTH and entry.name.lower() not in _SKIP_DIRS:
                        directories.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    path = Path(entry.path)
                    if path.suffix.lower() not in _SOURCE_EXTS:
                        continue
                    rel = safe_project_file(root, str(path))
                    if rel:
                        results.append(rel)
            except (OSError, RuntimeError):
                continue
        queue.extend(directories)
    if queue:
        truncated = True
    return results, truncated


def _tokens(task: str) -> set[str]:
    return {
        token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", task)
        if len(token) >= 3 and token.lower() not in _STOPWORDS
    }


def _source_score(path: str, tokens: set[str]) -> int:
    stem = Path(path).stem.lower()
    parts = set(re.split(r"[^a-z0-9]+", stem)) - {""}
    score = 0
    for token in tokens:
        if token in parts:
            score += 5
        elif len(token) >= 4 and any(
            part.startswith(token) or token.startswith(part)
            for part in parts if len(part) >= 4
        ):
            score += 3
    return score


def _from_hints(root: Path, hints: list[str] | None, task: str) -> list[str]:
    values = list(hints or [])[:_MAX_HINTS]
    values.extend(_SOURCE_PATH_RE.findall(task)[:_MAX_HINTS])
    selected: list[str] = []
    for raw in values:
        valid = safe_project_file(root, raw)
        if valid and Path(valid).suffix.lower() in _SOURCE_EXTS and not _is_test(valid):
            if valid not in selected:
                selected.append(valid)
    return selected[:2]


def _candidate_tests(source: str, indexed: list[str]) -> list[tuple[str, str]]:
    """Find only existing, stem-related tests; don't invent test paths."""
    base = Path(source).stem.lower()
    result: list[tuple[str, str]] = []
    for target in indexed:
        if not _is_test(target):
            continue
        name = Path(target).name.lower()
        teststem = Path(target).stem.lower()
        related = (
            teststem in ("test_" + base, base + "_test", base + ".test", base + ".spec")
            or name.startswith(base + ".test.") or name.startswith(base + ".spec.")
        )
        if related:
            result.append((target, "filename_match"))
    return sorted(set(result), key=lambda item: (
        0 if item[0].startswith("tests/") or item[0].startswith("test/") else 1,
        len(item[0]), item[0],
    ))[:_MAX_TESTS]


def _npm_test_available(root: Path) -> bool:
    manifest = root / "package.json"
    try:
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 65536:
            return False
        raw = manifest.read_text(encoding="utf-8")
        obj = json.loads(raw)
        scripts = obj.get("scripts", {}) if isinstance(obj, dict) else {}
        test = scripts.get("test") if isinstance(scripts, dict) else None
        if not isinstance(test, str) or not (1 <= len(test) <= 250):
            return False
        return (
            _SCRIPT_RISK.search(test) is None
            and re.search(r"\b(?:vitest|jest|mocha|ava|tap)\b|node\s+--test", test)
            is not None
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        return False


def _check_commands(root: Path, source: str, indexed: list[str]) -> list[dict[str, str]]:
    ext = Path(source).suffix.lower()
    candidates = _candidate_tests(source, indexed)
    checks: list[dict[str, str]] = []
    for test_path, basis in candidates:
        # No shell substitutions or exotic filenames in proposed commands.
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,180}", test_path):
            continue
        if ext == ".py" and test_path.endswith(".py"):
            command = "python -m pytest -q " + shlex.quote(test_path)
            scope = "targeted"
        elif ext in (".js", ".jsx", ".ts", ".tsx"):
            if _npm_test_available(root):
                command = "npm test"
                scope = "project_wide"
            elif test_path.endswith((".js", ".mjs", ".cjs")):
                command = "node --test " + shlex.quote(test_path)
                scope = "targeted"
            else:
                continue
        elif ext == ".go" and test_path.endswith("_test.go"):
            command = "go test ./..."
            scope = "project_wide"
        elif ext == ".rs" and (root / "Cargo.toml").is_file():
            command = "cargo test"
            scope = "project_wide"
        else:
            continue
        if classify_check(command) and not any(
            item["command"] == command for item in checks
        ):
            checks.append({
                "command": command, "test_file": test_path,
                "test_basis": basis, "scope": scope,
            })
    return checks[:_MAX_TESTS]


def audit_proposal(
    root: Path, profile: str, criteria: list[dict[str, str]],
    sources: list[str], checks: list[dict[str, str]],
    *, ambiguous: bool = False, truncated: bool = False,
) -> dict[str, Any]:
    """Independent coverage challenge; NEVER a declaration of correctness."""
    blockers: list[str] = []
    warnings: list[str] = []
    if ambiguous:
        blockers.append("Multiple equally plausible source owners; inspect and select a focus file.")
    if not sources:
        blockers.append("No grounded source file; locate the implementation first.")
    for source in sources:
        if safe_project_file(root, source) != source:
            blockers.append("Candidate source is no longer a safe, existing project file.")
            break
    if profile != "inspection" and not checks:
        blockers.append("No existing source-related runnable test was found; map a real check.")
    if checks and any(item["scope"] != "targeted" for item in checks):
        warnings.append("Project-wide check may not exercise the requested behavior.")
    if checks:
        warnings.append("Test filename matching does not prove behavioral assertions.")
    if profile == "repair" and checks:
        warnings.append("Failure reproduction is unproven: require red, relevant edit, then green.")
    if truncated:
        warnings.append("Source search was bounded; discovery may have missed files.")
    if sources:
        warnings.append("Confirm source ownership and affected dependencies before adoption.")
    if not blockers and not criteria:
        blockers.append("No valid criteria were generated.")
    return {
        "blockers": blockers[:3], "warnings": warnings[:4],
        "registerable": not blockers and bool(criteria),
        "owner_approved": False, "verified": False,
    }


def suggest_goal_contract(
    root: Path, task: str, *,
    focus_files: list[str] | None = None,
    inspection_leads: list[str] | None = None,
) -> dict[str, Any]:
    """Read-only suggestion based on actual project paths and test files."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("invalid project root")
    if not isinstance(task, str) or len(task) > 4000 or looks_like_secret(task):
        return {"status": "unavailable", "criteria": [], "registerable": False}
    profile = _profile(task)
    indexed, truncated = _index_sources(root)
    focus = _from_hints(root, focus_files, task)
    leads = _from_hints(root, inspection_leads, "")
    reason = "observed_focus" if focus else "recorded_inspection_lead" if leads else "filename_match"
    ambiguous = False
    plausible: list[str] = []
    if focus:
        sources = focus
    elif leads:
        sources = leads[:2]
    else:
        words = _tokens(task)
        ranked = sorted(
            ((-_source_score(path, words), path) for path in indexed if not _is_test(path)),
        )
        ranked = [(abs(score), path) for score, path in ranked if score < 0]
        plausible = [path for _, path in ranked[:4]]
        if not ranked:
            sources = []
        elif len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            ambiguous = True
            sources = []
        else:
            sources = [ranked[0][1]]
    criteria: list[dict[str, str]] = []
    for i, source in enumerate(sources):
        criteria.append({
            "id": "source_" + str(i + 1), "type": "source_read",
            "path": source,
        })
    checks = _check_commands(root, sources[0], indexed) if sources else []
    if profile != "inspection":
        for i, candidate in enumerate(checks):
            criteria.append({
                "id": ("regression_" if profile == "repair" and i == 0 else "check_") + str(i + 1),
                "type": "regression" if profile == "repair" and i == 0 else "check",
                "command": candidate["command"],
            })
    audit = audit_proposal(
        root, profile, criteria, sources, checks,
        ambiguous=ambiguous, truncated=truncated,
    )
    status = "review_required" if audit["registerable"] else "incomplete"
    return {
        "status": status, "profile": profile, "criteria": criteria,
        "source_files": sources, "source_basis": reason if sources else "unknown",
        "plausible_sources": plausible,
        "test_candidates": checks, "challenge": audit,
        "registered": False, "accepted_by_owner": False, "verified": False,
    }


def format_goal_proposal(plan: dict[str, Any], max_chars: int = 670) -> str:
    """Concise actionable candidate; never emit partial JSON or path strings."""
    if not plan.get("source_files") and not plan.get("plausible_sources"):
        return ""
    ticks = chr(96)
    lines = ["### Suggested goal contract (not registered or approved)"]
    lines.append("- Profile: " + plan["profile"] + "; source basis: " + plan["source_basis"])
    if plan["source_files"]:
        lines.append("- Source: " + ", ".join(ticks + p + ticks for p in plan["source_files"][:2]))
    elif plan["plausible_sources"]:
        lines.append("- Candidate owners: " + ", ".join(ticks + p + ticks for p in plan["plausible_sources"][:2]))
    checks = plan.get("test_candidates", [])
    if checks:
        lines.append("- Candidate check: " + ticks + checks[0]["command"] + ticks)
    challenges = plan.get("challenge", {})
    for message in (challenges.get("blockers") or [])[:1]:
        lines.append("- Blocker: " + message)
    for message in (challenges.get("warnings") or [])[:1]:
        lines.append("- Challenge: " + message)
    if challenges.get("registerable"):
        compact = json.dumps(plan["criteria"], separators=(",", ":"), ensure_ascii=True)
        lines.append("- Proposed JSON: " + ticks + compact + ticks)
        lines.append(
            "- After review, register with " + ticks
            + "python -m task_goal --project-root . --ref <goal-ref> --profile "
            + plan["profile"] + ticks + " (JSON on stdin)"
        )
    result = ""
    for line in lines:
        if len(result) + len(line) + 1 > max_chars:
            break
        result += line + "\n"
    return result
