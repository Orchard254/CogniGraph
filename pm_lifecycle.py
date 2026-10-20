"""Lifecycle layer: provenance verification, maintenance (prune/consolidate/optimize/audit), exports, and the agent prepare/checkpoint hooks."""
import datetime
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from pm_common import (
    tokenize_text, looks_like_secret,
    _vec_to_blob, _get_embed_model,
    _EMBED_MODEL_NAME, _SEMANTIC_AVAILABLE, np,
    _blob_to_vec, logger, _epistemic_status, _is_test_file,
)
from corpus_hygiene import STUB_PREFIXES, _stub_where, _stub_params


def _run_git(root: Path, *args: str):
    """Run one read-only git probe. Returns (stdout, error_kind).

    error_kind is None on success, "no_git" when the git binary is missing,
    "git_error" for any other failure (not a repo, no upstream, timeout).
    stdin=DEVNULL plus a 15s timeout per the repo subprocess convention, so a
    probe can never stall on an inherited MCP stdio pipe.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, check=False,
        )
    except FileNotFoundError:
        return None, "no_git"
    except (subprocess.TimeoutExpired, OSError):
        return None, "git_error"
    if proc.returncode != 0:
        return None, "git_error"
    return proc.stdout, None


# Units whose numeric value is load-bearing: two facts stating DIFFERENT values
# for the same unit are contradicting each other, not elaborating. Kept narrow
# on purpose -- a classifier that cries wolf gets ignored, which is worse than
# no classifier at all.
_COUNT_UNITS = ("tools", "tool", "tests", "test files", "scripts", "lines",
                "files", "facts", "edges", "pairs", "functions", "classes",
                "endpoints", "chars", "ms", "seconds")
_COUNT_RE = re.compile(
    r"\b(\d{1,4})\s+(?:-\s*tool\s+)?(" + "|".join(_COUNT_UNITS) + r")\b",
    re.IGNORECASE)
# Spelled-out numbers, which is how the "six vs seven scripts" defect appeared.
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                 "twelve": 12}
_WORD_COUNT_RE = re.compile(
    r"\b(" + "|".join(_WORD_NUMBERS) + r")\s+(?:\w+[\s-]+){0,3}?(" +
    "|".join(_COUNT_UNITS) + r")\b", re.IGNORECASE)
# "17-tool" / "14 tools" as a single token
_HYPHEN_COUNT_RE = re.compile(r"\b(\d{1,4})-tool\b", re.IGNORECASE)


def _asserted_counts(text: str) -> Dict[str, Dict[int, str]]:
    """Map unit -> {value: how it was written}.

    Units are normalized to singular so "18 tools" and "18 tool" unify. The
    origin is kept because a spelled-out number ("all seven scripts") reads
    differently in a report than a digit.
    """
    out: Dict[str, Dict[int, str]] = {}

    def add(unit, value, origin):
        unit = unit.lower()
        unit = "test" if unit == "tests" else (unit[:-1] if unit.endswith("s") else unit)
        out.setdefault(unit, {})[value] = origin

    for m in _COUNT_RE.finditer(text or ""):
        add(m.group(2), int(m.group(1)), "digit")
    for m in _WORD_COUNT_RE.finditer(text or ""):
        add(m.group(2), _WORD_NUMBERS[m.group(1).lower()], "word_number")
    for m in _HYPHEN_COUNT_RE.finditer(text or ""):
        add("tool", int(m.group(1)), "digit")
    return out


# Text signalling that THIS fact's number was true at a point and has since
# moved, rather than a claim about the present. Two such facts disagreeing is a
# timeline, not a conflict: "baseline is now 141 (was 107)" then "now 156".
#
# Kept NARROW on purpose. An earlier version included "no longer" and bare
# "since", which over-fired badly: fact #172 read "rewrote all seven scripts ...
# [an earlier script] no longer [exists]" and the unrelated "no longer"
# suppressed a genuine six-vs-seven contradiction.
_PROGRESSION_RE = re.compile(
    r"(\bis now\b|\bwas now\b|\bwas \d{1,4}\b|after adding|\bgrew\b|moved from"
    r"|\bhas since\b|currently at)",
    re.IGNORECASE)


def classify_pair(fact_a: Dict[str, Any], fact_b: Dict[str, Any]) -> Dict[str, Any]:
    """Do two similar facts AGREE, DISAGREE, or is it unclear?

    The audit sweeper answers "these N facts are similar", which is not the
    question a maintainer has. Reading all 26 candidate pairs on 2026-09-27,
    most were benign co-topic pairs -- "added X" beside "committed and pushed X"
    are two true things -- while every real defect was a pair that CONTRADICTED:
    14 vs 18 tools, "six" vs "seven" scripts.

    Only load-bearing numeric contradictions count, and a pair that reads as a
    timeline is excluded. Absence of a comparable assertion is "unclear", never
    a guess: an over-eager classifier gets ignored, which is worse than none.

    KNOWN LIMITATION: contradictions that need phrase semantics rather than
    numbers are reported "unclear", not detected. The 2026-07 pair
    "npm test runs a glob" vs "node --test uses an explicit file path" is a
    real defect this returns "unclear" for. It still reaches a human; it just
    does not get prioritised.
    """
    a_text = (fact_a or {}).get("content") or ""
    b_text = (fact_b or {}).get("content") or ""
    a_counts = _asserted_counts(a_text)
    b_counts = _asserted_counts(b_text)

    a_progresses = bool(_PROGRESSION_RE.search(a_text))
    b_progresses = bool(_PROGRESSION_RE.search(b_text))

    conflicts = []
    for unit in sorted(set(a_counts) & set(b_counts)):
        a_vals, b_vals = set(a_counts[unit]), set(b_counts[unit])
        if not a_vals or not b_vals:
            continue
        if a_vals & b_vals:
            # They share a value. One may additionally mention another as
            # history -- "#635 was regenerated to 15 tools ... as of 09-15 the
            # tree registers 18" AGREES with "#634 verified at 18 tools".
            # Flagging that is noise, so only disjoint sets are conflicts.
            continue
        if a_progresses or b_progresses:
            # "baseline is now 141 (was 107)" then "now 156 (was 141)" is a
            # timeline, not a contradiction.
            continue
        origins = set(a_counts[unit].values()) | set(b_counts[unit].values())
        conflicts.append({
            "kind": "word_number" if "word_number" in origins else "count",
            "unit": unit,
            "a_value": sorted(a_vals)[0] if len(a_vals) == 1 else sorted(a_vals),
            "b_value": sorted(b_vals)[0] if len(b_vals) == 1 else sorted(b_vals),
        })

    if conflicts:
        verdict, reason = "disagree", "conflicting values for the same unit"
    elif a_text.strip() == b_text.strip():
        verdict, reason = "agree", "identical content"
    else:
        verdict, reason = "unclear", "no comparable load-bearing assertion"

    return {
        "a_id": (fact_a or {}).get("id"),
        "b_id": (fact_b or {}).get("id"),
        "verdict": verdict,
        "reason": reason,
        "conflicts": conflicts,
    }


class LifecycleMixin:
    _epistemic_status = staticmethod(_epistemic_status)
    _is_test_file = staticmethod(_is_test_file)
    # Audit sweep thresholds. The semantic default sits just under the
    # write-time dedup threshold in write() (0.88) on purpose: dedup only ever
    # sees a pair at the instant of writing, and this corpus is re-embedded
    # over time (backfill, watcher re_embed), so a pair can drift above 0.88
    # after both facts already exist. A periodic sweep is the complement to
    # that lagging guard, not a duplicate of it. The lexical default is the
    # original audit() constant, kept so the Jaccard path is unchanged.
    AUDIT_SEMANTIC_THRESHOLD = 0.80
    AUDIT_LEXICAL_THRESHOLD = 0.65

    def flag_fact(self, fact_id: int, note: str = "", flag_type: str = "misleading",
                  flagged_by: Optional[str] = None) -> Dict[str, Any]:
        """CORRECTION HOOK: record that a served fact misled the agent.

        Flags are stamped inline in file briefings so future sessions see the
        warning at priming time instead of re-consuming the bad fact at face
        value. Prefer supersede() when the corrected content is known.
        """
        if any(looks_like_secret(value) for value in (note, flag_type, flagged_by) if value):
            raise ValueError(
                "refused: this looks like it contains a credential or secret in the flag metadata."
            )
        row = self.conn.execute("SELECT id FROM memory WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            raise ValueError(f"no fact with id {fact_id}")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO fact_flags (fact_id, flag_type, note, flagged_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (fact_id, flag_type, note or None, flagged_by, now),
        )
        return {"status": "FLAGGED", "fact_id": fact_id, "flag_type": flag_type}

    def verify_provenance(self, fact_id: int, project_root: Optional[str] = None) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM memory WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            raise ValueError(f"no fact with id {fact_id}")
        if not row["file_path"]:
            return {"status": "NO_FILE_PATH", "fact_id": fact_id}
        
        root = Path(project_root or Path.cwd())
        full_path = root / row["file_path"]
        if not full_path.exists():
            self.deprecate(fact_id, reason=f"file_deleted: {row['file_path']}")
            return {"status": "DEPRECATED_FILE_DELETED", "fact_id": fact_id, "file_path": row["file_path"]}

        file_lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        l_start = max(0, (row["line_start"] or 1) - 1)
        l_end = min(len(file_lines), row["line_end"] or len(file_lines))
        snippet = "".join(file_lines[l_start:l_end])
        curr_hash = hashlib.sha256(snippet.encode("utf-8")).hexdigest()

        if row["code_hash"] and curr_hash == row["code_hash"]:
            return {"status": "VERIFIED", "fact_id": fact_id, "file_path": row["file_path"]}

        # Neighborhood search (±30 lines shift)
        if row["code_hash"]:
            target_len = l_end - l_start
            search_start = max(0, l_start - 30)
            search_end = min(len(file_lines), l_end + 30)
            for idx in range(search_start, search_end - target_len + 1):
                candidate = "".join(file_lines[idx:idx + target_len])
                if hashlib.sha256(candidate.encode("utf-8")).hexdigest() == row["code_hash"]:
                    # Healed line range!
                    new_start = idx + 1
                    new_end = idx + target_len
                    self.conn.execute(
                        "UPDATE memory SET line_start = ?, line_end = ? WHERE id = ?",
                        (new_start, new_end, fact_id),
                    )
                    return {
                        "status": "HEALED_LINE_SHIFT",
                        "fact_id": fact_id,
                        "old_lines": (row["line_start"], row["line_end"]),
                        "new_lines": (new_start, new_end),
                    }

        # Code changed and snippet moved/changed — drift detected.
        # Drift != obsolescence: flag for review, never auto-deprecate here.
        self.conn.execute(
            "UPDATE memory SET confidence = 'uncertain' WHERE id = ?", (fact_id,))
        return {
            "status": "DRIFT_DETECTED",
            "fact_id": fact_id,
            "file_path": row["file_path"],
            "stored_hash": row["code_hash"],
            "actual_hash": curr_hash,
        }

    def consolidate_state_facts(self, session_id: Optional[str] = None, category: str = "state",
                                allow_all: bool = False) -> Dict[str, Any]:
        if not session_id and not allow_all:
            raise ValueError(
                "consolidation requires a session_id scope. "
                "Pass session_id to consolidate one session, or allow_all=True "
                "to explicitly collapse ALL facts in this category (destructive)."
            )
        sql = "SELECT * FROM memory WHERE deprecated_at IS NULL AND category = ?"
        params = [category.lower().strip()]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        
        rows = self.conn.execute(sql, params).fetchall()
        if len(rows) <= 3:
            return {"status": "skipped", "reason": "too few facts to consolidate", "count": len(rows)}

        # Combine micro-state facts into milestone summary
        contents = [r["content"] for r in rows]
        summary_content = f"Consolidated milestone checkpoint ({len(rows)} facts): " + "; ".join(contents[:6])
        if len(contents) > 6:
            summary_content += f" (+{len(contents)-6} more itemized steps)"

        new_id = self.write("state", summary_content, rationale="Automated session consolidation",
                            confidence="confirmed", session_id=session_id, force=True)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        for r in rows:
            self.conn.execute(
                "UPDATE memory SET superseded_by = ?, deprecated_at = ?, deprecated_reason = 'consolidated' WHERE id = ?",
                (new_id, now, r["id"]),
            )
        
        return {"status": "consolidated", "new_fact_id": new_id, "deprecated_count": len(rows)}

    def check_safety(self, file_paths: List[str]) -> Dict[str, Any]:
        """
        Unified multi-file safety matrix before executing a multi-file refactor task.
        Analyzes cross-file dependencies, shared constraints, and breaking change risks.
        """
        clean_paths = [fp.strip().replace("\\", "/").lower() for fp in file_paths if fp.strip()]
        if not clean_paths:
            return {"status": "NO_FILES_PROVIDED"}

        file_briefings = {}
        cross_dependencies = []

        for path in clean_paths:
            briefing = self.get_file_context(path)
            file_briefings[path] = briefing

        # Find graph links between any two files in the batch
        all_edges = self.conn.execute(
            "SELECT e.relation, m1.file_path as source_file, m1.content as source_content, "
            "m2.file_path as target_file, m2.content as target_content "
            "FROM memory_edges e JOIN memory m1 ON e.source_id = m1.id JOIN memory m2 ON e.target_id = m2.id "
            "WHERE m1.deprecated_at IS NULL AND m2.deprecated_at IS NULL"
        ).fetchall()

        for edge in all_edges:
            sf = (edge["source_file"] or "").replace("\\", "/").lower()
            tf = (edge["target_file"] or "").replace("\\", "/").lower()
            if any(cp in sf for cp in clean_paths) and any(cp in tf for cp in clean_paths):
                cross_dependencies.append({
                    "relation": edge["relation"],
                    "source_file": edge["source_file"],
                    "target_file": edge["target_file"],
                    "source_content": edge["source_content"],
                    "target_content": edge["target_content"],
                })

        # Structural code-graph union (#499): memory_edges alone misses real
        # call/import deps. Additive — old cross_* fields are untouched.
        structural_dependencies = []
        known_limitations = []
        structural_scan_failed = False
        constraint_query_failed = False
        try:
            struct_rows = self.conn.execute(
                "SELECT e.edge_type, "
                "nc.file_path AS caller_file, nc.name AS caller_symbol, "
                "nc.class_name AS caller_class, nc.line_start AS caller_line, "
                "ne.file_path AS callee_file, ne.name AS callee_symbol, "
                "ne.class_name AS callee_class "
                "FROM structural_edges e "
                "JOIN structural_nodes nc ON e.caller_id = nc.id "
                "JOIN structural_nodes ne ON e.callee_id = ne.id "
                "WHERE e.edge_type IN ('calls', 'imports')"
            ).fetchall()
        except Exception:
            struct_rows = []
            structural_scan_failed = True
            known_limitations.append("structural_edges unavailable; structural scan SKIPPED - analysis incomplete")
        for r in struct_rows:
            cf = (r["caller_file"] or "").replace("\\", "/").lower()
            tf = (r["callee_file"] or "").replace("\\", "/").lower()
            if any(cp in cf for cp in clean_paths) and any(cp in tf for cp in clean_paths):
                structural_dependencies.append({
                    "edge_type": r["edge_type"],
                    "caller_file": r["caller_file"],
                    "caller_symbol": r["caller_symbol"],
                    "caller_class": r["caller_class"],
                    "caller_line": r["caller_line"],
                    "callee_file": r["callee_file"],
                    "callee_symbol": r["callee_symbol"],
                    "callee_class": r["callee_class"],
                })
        if not known_limitations:
            known_limitations.append(
                "structural_edges reflect last watcher sync; may be stale; "
                "sync callees before callers for cross-file resolution"
            )

        # Stored constraints surfaced as a validation checklist (section F slice)
        constraint_checks = []
        try:
            for cr in self.conn.execute(
                "SELECT id, file_path, content, importance FROM memory "
                "WHERE deprecated_at IS NULL AND category = 'constraint'"
            ).fetchall():
                cfp = (cr["file_path"] or "").replace("\\", "/").lower()
                applies = (not cfp) or any(cp in cfp or cfp in cp for cp in clean_paths)
                if applies:
                    constraint_checks.append({
                        "fact_id": cr["id"],
                        "file_path": cr["file_path"],
                        "constraint": cr["content"],
                        "importance": cr["importance"],
                        "verdict": "review",
                    })
        except Exception:
            constraint_checks = []
            constraint_query_failed = True
            known_limitations.append("constraint query failed; constraint analysis SKIPPED - analysis incomplete")
        analysis_complete = (not structural_scan_failed) and (not constraint_query_failed)
        safe = (analysis_complete and len(cross_dependencies) == 0
                and len(structural_dependencies) == 0 and len(constraint_checks) == 0)
        if not analysis_complete:
            reason = (
                f"Incomplete analysis: structural_scan_failed={structural_scan_failed}, "
                f"constraint_query_failed={constraint_query_failed}; review before proceeding."
            )
        elif constraint_checks and not cross_dependencies and not structural_dependencies:
            reason = (
                f"{len(constraint_checks)} stored constraint(s) require review before proceeding."
            )
        elif safe:
            reason = "No cross-file dependencies or constraints detected across the batch; safe to continue."
        else:
            reason = (
                f"Review before proceeding: cross_dependencies={len(cross_dependencies)}, "
                f"structural_dependencies={len(structural_dependencies)}, "
                f"constraint_checks={len(constraint_checks)}."
            )
        verdict = "PASS" if safe else "REVIEW_REQUIRED"
        # Partial analysis can never be PASS
        if not analysis_complete:
            verdict = "REVIEW_REQUIRED"
            safe = False
        return {
            "status": "CHECKED",
            "files_analyzed": len(clean_paths),
            "cross_dependencies_count": len(cross_dependencies),
            "cross_dependencies": cross_dependencies,
            "structural_dependencies_count": len(structural_dependencies),
            "structural_dependencies": structural_dependencies,
            "known_limitations": known_limitations,
            "analysis_complete": analysis_complete,
            "safe_to_continue": safe,
            "safe_to_continue_reason": reason,
            "briefings": file_briefings,
            "constraint_checks": constraint_checks,
            "verdict": verdict,
            "constraint_verdict": verdict,
        }

    def check_hygiene(self, project_root: Optional[str] = None,
                      max_dirty_files: int = 20,
                      ci_status: str = "") -> Dict[str, Any]:
        """Commit/push-discipline status for the working tree (local git only).

        Read-only planning aid for the finalizing phase: working-tree
        cleanliness, ahead/behind vs. the tracked upstream branch, and stale
        branches whose upstream is gone. Never raises for git conditions —
        every unavailable signal is reported as a value plus a reason in
        limitations[] (same honest-limitations contract as blast_radius).

        All probes are read-only (status, rev-parse, rev-list --count,
        branch -vv) with stdin=DEVNULL and a 15s timeout, per the repo's
        subprocess convention. v1 deliberately attempts no CI-status
        correlation (needs the GitHub API plus a token — out of scope).

        project_root defaults to the process CWD, which is correct under the
        MCP server. Tests MUST pass an explicit scratch root: under pytest the
        CWD is the real repo, so a bare call would probe live state instead
        of the fixture (cf. the episodic #84 wrong-directory precedent).
        """
        limitations = [
            "CI-status correlation is not attempted in v1: matching HEAD to "
            "a green CI run needs the GitHub API plus a token (out of scope "
            "for this local-git-only tool).",
        ]
        try:
            cap = int(max_dirty_files)
        except (TypeError, ValueError):
            cap = 20
        cap = max(1, min(cap, 200))

        root = Path(project_root) if project_root else Path.cwd()
        base: Dict[str, Any] = {
            "status": "NOT_A_REPO",
            "ci_status": ci_status or None,
            "project_root": str(root),
            "branch": None,
            "detached": False,
            "head": None,
            "upstream": None,
            "ahead": 0,
            "behind": 0,
            "dirty_files": [],
            "dirty_total": 0,
            "dirty_truncated": False,
            "untracked_count": 0,
            "stale_branches": [],
            "limitations": limitations,
        }
        if not root.exists():
            limitations.append(f"project root does not exist: {root}")
            return base

        _, err = _run_git(root, "rev-parse", "--git-dir")
        if err == "no_git":
            base["status"] = "GIT_UNAVAILABLE"
            limitations.append("git binary not found on PATH")
            return base
        if err:
            limitations.append("not a git repository (rev-parse --git-dir failed)")
            return base

        head_out, _ = _run_git(root, "rev-parse", "HEAD")
        if head_out:
            base["head"] = head_out.strip()
        else:
            limitations.append("no commits yet (empty repository)")

        branch_out, _ = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch_out:
            base["branch"] = branch_out.strip()
            base["detached"] = base["branch"] == "HEAD"
            if base["detached"]:
                limitations.append("detached HEAD: branch comparisons use HEAD directly")

        upstream_out, _ = _run_git(
            root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if upstream_out:
            base["upstream"] = upstream_out.strip()
        else:
            limitations.append(
                f"no upstream configured for '{base['branch']}': "
                "ahead/behind are reported as 0")

        if base["upstream"]:
            ab_out, _ = _run_git(root, "rev-list", "--count", "--left-right",
                                 "HEAD...@{u}")
            if ab_out:
                try:
                    ahead_s, behind_s = ab_out.strip().split()
                    base["ahead"] = max(0, int(ahead_s))
                    base["behind"] = max(0, int(behind_s))
                except ValueError:
                    limitations.append(
                        f"unparseable ahead/behind output: {ab_out.strip()!r}")
            else:
                limitations.append("ahead/behind probe failed; reported as 0")

        st_out, _ = _run_git(root, "status", "--porcelain=v1")
        dirty: List[str] = []
        untracked = 0
        if st_out is not None:
            for line in st_out.splitlines():
                if len(line) < 4:
                    continue
                if line.startswith("??"):
                    untracked += 1
                    continue
                if line.startswith("!!"):
                    continue  # ignored files are not dirt
                path = line[3:]
                if " -> " in path:  # rename/copy: report the new path
                    path = path.split(" -> ", 1)[1]
                dirty.append(path.strip().strip('"'))
        else:
            limitations.append("git status probe failed; cleanliness unknown")
        base["untracked_count"] = untracked
        base["dirty_total"] = len(dirty)
        base["dirty_files"] = dirty[:cap]
        base["dirty_truncated"] = len(dirty) > cap

        bv_out, _ = _run_git(root, "branch", "-vv")
        if bv_out is not None:
            for line in bv_out.splitlines():
                stripped = line[2:] if line[:1] in " *+" else line
                parts = stripped.split()
                if len(parts) < 3:
                    continue
                name = parts[0]
                track = stripped[stripped.find("[") + 1:stripped.find("]")] \
                    if "[" in stripped and "]" in stripped else ""
                if track.endswith(": gone"):
                    base["stale_branches"].append(
                        {"branch": name, "upstream": track[:-len(": gone")]})

        if base["dirty_total"] or base["untracked_count"]:
            base["status"] = "DIRTY"
        elif base["ahead"] and base["behind"]:
            base["status"] = "DIVERGED"
        elif base["ahead"]:
            base["status"] = "AHEAD"
        elif base["behind"]:
            base["status"] = "BEHIND"
        else:
            base["status"] = "CLEAN"
        return base

    def scan_provenance(self, project_root: Optional[str] = None, auto_deprecate: bool = False, auto_heal: bool = False) -> Dict[str, Any]:
        """
        Bulk health check: scans ALL active facts with file_path + code_hash and reports
        which ones have drifted (file changed/deleted since the fact was recorded).

        Args:
            project_root: Root directory to resolve relative file paths. Defaults to CWD.
            auto_deprecate: If True, automatically deprecate facts whose files are deleted.
            auto_heal: If True, automatically update code_hash for low-importance (1) file-coverage drifted facts to current file hash (self-healing for churn).

        Returns a structured report: {ok, drifted, missing, healed, total_checked}
        """
        root = Path(project_root or Path.cwd())
        rows = self.conn.execute(
            "SELECT id, file_path, code_hash, line_start, line_end, content, importance, tags "
            "FROM memory WHERE deprecated_at IS NULL AND file_path IS NOT NULL"
        ).fetchall()

        report = {"ok": [], "drifted": [], "missing": [], "healed": [], "total_checked": len(rows)}

        for row in rows:
            full_path = root / row["file_path"]
            if not full_path.exists():
                report["missing"].append({"id": row["id"], "file_path": row["file_path"]})
                if auto_deprecate:
                    self.deprecate(row["id"], reason=f"file_deleted: {row['file_path']}")
                continue

            if not row["code_hash"]:
                # No hash to compare — record as ok (unknown)
                report["ok"].append({"id": row["id"], "file_path": row["file_path"], "note": "no_hash"})
                continue

            # Skip directory file_path (e.g., sythion/backend/runtime/) — not a file, no drift
            if full_path.is_dir():
                report["ok"].append({"id": row["id"], "file_path": row["file_path"], "note": "is_dir"})
                continue
            try:
                # Use same snippet logic as verify_provenance for consistency

                # Read file bytes once to avoid double-read
                file_bytes = full_path.read_bytes()
                if row["line_start"] and row["line_end"]:
                    file_lines = file_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
                    l_start = max(0, (row["line_start"] or 1) - 1)
                    l_end = min(len(file_lines), row["line_end"] or len(file_lines))
                    snippet = "".join(file_lines[l_start:l_end])
                else:
                    # Whole-file hash: use raw bytes to match bootstrap's hashlib.sha256(data).hexdigest()
                    actual_hash = hashlib.sha256(file_bytes).hexdigest()
                    if actual_hash == row["code_hash"]:
                        report["ok"].append({"id": row["id"], "file_path": row["file_path"]})
                        continue
                    # Fallback to text snippet hash for cross-platform newline tolerance
                    snippet = file_bytes.decode("utf-8", errors="replace")[:2000]
                actual_hash = hashlib.sha256(snippet.encode("utf-8")).hexdigest()
                if actual_hash == row["code_hash"]:
                    report["ok"].append({"id": row["id"], "file_path": row["file_path"]})
                else:
                    # Auto-heal low-importance file-coverage drift (churn, not decision)
                    if auto_heal and (row["importance"] == 1 or "file-coverage" in (row["tags"] or "")):
                        try:
                            # Update hash to current file; keep line range if present
                            self.conn.execute("UPDATE memory SET code_hash=? WHERE id=?", (actual_hash, row["id"]))
                            report["healed"].append({"id": row["id"], "file_path": row["file_path"]})
                            report["ok"].append({"id": row["id"], "file_path": row["file_path"], "note": "auto_healed"})
                        except Exception:
                            report["drifted"].append({
                                "id": row["id"],
                                "file_path": row["file_path"],
                                "stored_hash": row["code_hash"],
                                "actual_hash": actual_hash,
                                "content_preview": row["content"][:120],
                            })
                    else:
                        report["drifted"].append({
                            "id": row["id"],
                            "file_path": row["file_path"],
                            "stored_hash": row["code_hash"],
                            "actual_hash": actual_hash,
                            "content_preview": row["content"][:120],
                        })
            except Exception as e:
                report["drifted"].append({"id": row["id"], "file_path": row["file_path"], "error": str(e)})

        return report

    def backfill_embeddings(self, batch_size: int = 64) -> Dict[str, Any]:
        """
        Backfill semantic embeddings for all existing active facts that don't have one yet.
        Safe to call on an existing database — skips facts that already have embeddings.
        Returns {backfilled, skipped, total_active}.
        """
        if not _SEMANTIC_AVAILABLE:
            return {"status": "SKIPPED", "reason": "sentence-transformers not installed"}
        existing_ids = {
            r["fact_id"] for r in
            self.conn.execute("SELECT fact_id FROM memory_embeddings").fetchall()
        }
        rows = self.conn.execute(
            "SELECT id, content, rationale FROM memory WHERE deprecated_at IS NULL"
        ).fetchall()
        to_embed = [r for r in rows if r["id"] not in existing_ids]
        if not to_embed:
            return {"status": "OK", "backfilled": 0, "skipped": len(rows), "total_active": len(rows)}

        model = _get_embed_model()
        if model is None:
            return {"status": "SKIPPED", "reason": "embedding model unavailable"}
        texts = [f"{r['content']} {r['rationale'] or ''}" for r in to_embed]
        count = 0
        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i: i + batch_size]
            batch_rows = to_embed[i: i + batch_size]
            vecs = list(model.embed(batch_texts))
            for row, vec in zip(batch_rows, vecs):
                vec = np.asarray(vec, dtype="float32")

                norm = np.linalg.norm(vec)
                if norm > 0.0:
                    vec = vec / norm
                blob = _vec_to_blob(vec.astype("float32"))
                self.conn.execute(
                    "INSERT OR REPLACE INTO memory_embeddings (fact_id, model, embedding) VALUES (?, ?, ?)",
                    (row["id"], _EMBED_MODEL_NAME, blob),
                )
                count += 1

        return {"status": "OK", "backfilled": count, "skipped": len(existing_ids), "total_active": len(rows)}

    # Pollution markers. Anchored on purpose: a loose prefix regex flagged a
    # real recorded decision (#183 'Placeholder GitHub URLs removed from
    # package.json rather than replaced'), so a marker must consume the whole
    # content string, not merely appear at the start. Each pattern is a
    # synthetic-fixture shape written by a throwaway script, not a phrase a
    # curated fact would plausibly use.
    POLLUTION_PATTERNS = (
        (r"^(?:batch|stress|test)\s+fact\s*\d*\s*$", "synthetic indexed fixture"),
        (r"^(?:batch|stress)\s+test\s*\d*\s*$", "synthetic indexed fixture"),
        (r"^fact\s+\d+\s*$", "synthetic indexed fixture"),
        # Prefix forms with trailing prose ("Stress test fact 1: ...", "Batch
        # fact 0"). The length cap plus no-edges requirement carry the
        # false-positive load here, so a distinctive anchored prefix is enough.
        (r"^stress\s+test\b.*$", "stress-test artifact"),
        (r"^batch\s+fact\b.*$", "synthetic indexed fixture"),
        (r"^test\s+batch\s*\d*\s*$", "test scaffold"),
        (r"^test\s+fact\s*$", "test scaffold"),
        (r"^test\s+fact\s+for\b.*$", "test scaffold"),
        (r"^latency\s+test\b.*$", "test scaffold"),
        (r"^another\s+test\s+via\b.*$", "test scaffold"),
    )
    # Categories whose facts retrieval boosts; pollution there costs result
    # slots. prune_stale only looks at ('state','other'), which is why it
    # reported 0 candidates while 8 junk facts sat in 'component'.
    POLLUTION_CATEGORIES = ("component", "decision", "constraint")
    POLLUTION_MAX_CHARS = 80

    def detect_pollution(self) -> Dict[str, Any]:
        """Report throwaway test/stress artifacts. Read-only; never deprecates.

        Complements prune_stale, which selects by inactivity and so cannot see
        artifacts that were written recently and are being actively retrieved.

        Deliberately conservative: an anchored full-content match, a length
        cap, and no graph edges are all required, so a real fact that merely
        starts with a similar word is not reported. Deciding whether a flagged
        fact actually matters needs human judgment, so this only produces a
        worklist; reconcile with memory_deprecate.
        """
        stub_sql = " AND ".join("content NOT LIKE ?" for _ in STUB_PREFIXES)
        cats = ",".join("?" * len(self.POLLUTION_CATEGORIES))
        rows = self.conn.execute(
            f"SELECT m.id, m.category, m.content, m.rationale, m.file_path, "
            f"m.created_at, m.importance FROM memory m "
            f"WHERE m.deprecated_at IS NULL AND {stub_sql} "
            f"AND m.category IN ({cats})",
            tuple(f"{p}%" for p in STUB_PREFIXES) + self.POLLUTION_CATEGORIES,
        ).fetchall()

        scanned = len(rows)
        findings: List[Dict[str, Any]] = []
        for r in rows:
            text = (r["content"] or "").strip()
            if not text or len(text) > self.POLLUTION_MAX_CHARS:
                continue
            reason = next(
                (why for pat, why in self.POLLUTION_PATTERNS
                 if re.match(pat, text, re.IGNORECASE)),
                None,
            )
            if reason is None:
                continue
            edged = self.conn.execute(
                "SELECT 1 FROM memory_edges WHERE source_id=? OR target_id=? LIMIT 1",
                (r["id"], r["id"])).fetchone()
            if edged is not None:
                continue  # graph-connected: something depends on it
            findings.append({
                "id": r["id"],
                "content": text,
                "category": r["category"],
                "rationale": r["rationale"],
                "file_path": r["file_path"],
                "created_at": r["created_at"],
                "importance": r["importance"],
                "reason": reason,
            })

        findings.sort(key=lambda d: d["id"])
        return {
            "read_only": True,
            "scanned": scanned,
            "count": len(findings),
            "pollution": findings,
            "note": ("Report only. Reconcile with memory_deprecate; a human "
                     "decides whether a flagged fact still matters."),
        }

    def prune_stale_stubs(self, root, dry_run: bool = True) -> Dict[str, Any]:
        """
        Deprecate active whole-file template stubs whose file no longer exists.

        The missing half of bootstrap. scan_file_tree only ever WRITES stubs for
        files that are present, so nothing could ever remove one: on the live
        corpus 29 active 'Source file:' stubs pointed at deleted scratch files,
        and stats().active_stubs reported 115 when roughly 86 were real. They
        were inert (excluded from the audit, the structural lane, and the
        curated count) but unbounded, and the next scratch-file burst recreates
        the condition.

        Deliberately narrow. Stubs are INVENTORY -- auto-generated facts that
        assert a file exists, so a file that is gone makes them false. A curated
        fact carrying a file_path is KNOWLEDGE, and a dangling anchor on one is
        a different problem with a different answer: null the column and keep
        the fact (see fact #804). Only stubs are touched here.

        Also skipped, because they are not scan_file_tree's relative-path
        convention and therefore not ours to judge: absolute paths, paths with a
        drive letter, and anything containing '..'.

        `memory` has no repo_id column, so a stub is identified only by
        file_path. Under the per-project layout (.cognigraph/memory.db per root)
        that is unambiguous, but if one database ever held two roots, pruning
        from the wrong root would delete the other root's inventory. Hence
        opt-in, and dry_run=True by default: this names each candidate before
        removing anything.
        """
        try:
            root_path = Path(root).resolve()
        except Exception as e:
            raise ValueError(f"bad prune root {root!r}: {e}")
        if not root_path.is_dir():
            # Refusing matters: pruning against a non-existent root would
            # resolve every relative path to "missing" and wipe the inventory.
            raise ValueError(f"prune root is not a directory: {root_path}")

        # OR, never AND. corpus_hygiene._stub_where() is the single source of
        # truth for this form: hand-rolling it as AND requires a row to match
        # every prefix simultaneously, which silently matches nothing.
        rows = self.conn.execute(
            f"SELECT id, file_path, content FROM memory "
            f"WHERE deprecated_at IS NULL AND file_path IS NOT NULL "
            f"AND file_path != '' AND ({_stub_where()})",
            _stub_params()).fetchall()

        candidates: List[Dict[str, Any]] = []
        for r in rows:
            fp = (r["file_path"] or "").strip()
            if not fp:
                continue
            norm = fp.replace("\\", "/")
            if norm.startswith("/") or ":" in norm.split("/")[0]:
                continue                       # absolute or drive-lettered
            if ".." in Path(norm).parts:
                continue                       # escapes the root
            if (root_path / norm).exists():
                continue
            candidates.append({"id": r["id"], "file_path": norm,
                               "content": (r["content"] or "")[:120]})

        pruned = 0
        if not dry_run:
            for c in candidates:
                try:
                    self.deprecate(c["id"], reason=(
                        "bootstrap prune: the file this 'Source file:' stub "
                        "asserted no longer exists under the scanned root"))
                    pruned += 1
                except Exception as e:
                    logger.debug("stub prune failed for id %s: %r", c["id"], e)

        return {
            "root": str(root_path),
            "dry_run": bool(dry_run),
            "candidates": candidates,
            "scanned": len(rows),
            "candidate_count": len(candidates),
            "pruned": pruned,
            "note": ("Whole-file 'Source file:' stubs only. Curated facts with a "
                     "dangling file_path are untouched -- null the anchor instead "
                     "(memory_supersede with file_path=''), see fact #804."),
        }

    def prune_stale(self, days_inactive: int = 90, dry_run: bool = True,
                    min_keep: int = 10) -> Dict[str, Any]:
        """
        Deprecate low-value facts not accessed within `days_inactive` days.

        Safety rails: never touches facts with importance >= 4, facts that have
        graph edges, non-state/other categories, or anything that would drop the
        active total below `min_keep`. Default is dry_run (preview only).
        """
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days_inactive)).isoformat()
        rows = self.conn.execute(
            "SELECT m.id FROM memory m "
            "WHERE m.deprecated_at IS NULL AND m.category IN ('state', 'other') "
            "AND m.importance <= 3 "
            "AND ((m.last_accessed_at IS NULL AND m.created_at < ?) "
            "     OR (m.last_accessed_at IS NOT NULL AND m.last_accessed_at < ?)) "
            "AND NOT EXISTS (SELECT 1 FROM memory_edges e "
            "                WHERE e.source_id = m.id OR e.target_id = m.id) "
            "ORDER BY m.created_at ASC",
            (cutoff, cutoff)).fetchall()
        candidates = [r["id"] for r in rows]
        total_active = self.stats()["total_active"]
        allowed = max(0, total_active - min_keep)
        to_prune = candidates[:allowed]
        if not dry_run and to_prune:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for fid in to_prune:
                self.conn.execute(
                    "UPDATE memory SET deprecated_at = ?, deprecated_reason = 'pruned_stale' WHERE id = ?",
                    (now, fid))
        return {
            "status": "PRUNED" if (not dry_run and to_prune) else "DRY_RUN" if dry_run else "NO_CANDIDATES",
            "dry_run": dry_run,
            "days_inactive": days_inactive,
            "candidates": len(candidates),
            "pruned": len(to_prune),
            "ids": to_prune,
        }

    def optimize_db(self) -> Dict[str, Any]:
        """Run SQLite maintenance: PRAGMA optimize, VACUUM, and FTS integrity check."""
        self.conn.execute("PRAGMA optimize")
        self.conn.execute("VACUUM")
        stats = self.stats()
        return {"status": "OPTIMIZED", "stats": stats}

    def backup_db(self, tag: str = "manual", keep: int = 8) -> Dict[str, Any]:
        """Timestamped full-DB snapshot (WAL-safe, integrity-checked, pruned)."""
        from pm_backup import timestamped_backup
        res = timestamped_backup(self.db_path, tag=tag, keep=keep)
        res["status"] = "BACKED_UP"
        return res

    def export_interactive_html(self, out_path: Optional[str] = None) -> str:
        """
        Generate a self-contained standalone single-file HTML page containing an
        interactive 2D graph visualizer of project memory facts and relationship edges.
        """
        # limit=None: an export that silently drops facts is data loss.
        facts = self.get_state(limit=None)
        edges = self.conn.execute(
            "SELECT source_id, target_id, relation FROM memory_edges"
        ).fetchall()

        nodes_data = []
        category_colors = {
            "component": "#3b82f6",  # blue
            "decision": "#22c55e",   # green
            "constraint": "#ef4444", # red
            "state": "#f59e0b",      # amber
            "other": "#8b5cf6",      # purple
        }

        for f in facts:
            nodes_data.append({
                "id": f["id"],
                "label": f"[{f['id']}] {f['category'].upper()}",
                "content": f["content"][:80] + ("..." if len(f["content"]) > 80 else ""),
                "full_content": f["content"],
                "category": f["category"],
                "color": category_colors.get(f["category"], "#6b7280"),
                "file_path": f["file_path"] or "",
            })

        links_data = []
        for e in edges:
            links_data.append({
                "source": e["source_id"],
                "target": e["target_id"],
                "relation": e["relation"],
            })

        json_nodes = json.dumps(nodes_data)
        json_links = json.dumps(links_data)

        html_content = f"""<!DOCTYPE html>
<html lang="en">

<head>
    <meta charset="UTF-8">
    <title>CogniGraph Interactive Knowledge Graph</title>
    <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
    <style>
        body {{ margin: 0; padding: 0; font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f8fafc; }}
        #header {{ padding: 16px 24px; background: #1e293b; border-bottom: 1px solid #334155; display: flex; justify-content: space-between; align-items: center; }}
        #title {{ font-size: 20px; font-weight: 700; color: #38bdf8; }}
        #stats {{ font-size: 14px; color: #94a3b8; }}
        #container {{ display: flex; height: calc(100vh - 65px); }}
        #mynetwork {{ flex: 1; height: 100%; }}
        #sidebar {{ width: 360px; background: #1e293b; border-left: 1px solid #334155; padding: 20px; overflow-y: auto; display: none; }}
        .badge {{ display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; text-transform: uppercase; margin-bottom: 8px; }}
        .fact-card {{ background: #0f172a; border-radius: 8px; padding: 16px; border: 1px solid #334155; }}
        .fact-title {{ font-size: 16px; font-weight: 600; margin-bottom: 8px; color: #f8fafc; }}
        .fact-desc {{ font-size: 14px; line-height: 1.5; color: #cbd5e1; }}
        .fact-meta {{ margin-top: 12px; font-size: 12px; color: #64748b; }}
    </style>
</head>
<body>
    <div id="header">
        <div id="title">CogniGraph Knowledge Graph</div>
        <div id="stats">Active Nodes: {len(nodes_data)} | Relationship Edges: {len(links_data)}</div>
    </div>
    <div id="container">
        <div id="mynetwork"></div>
        <div id="sidebar">
            <div class="fact-card">
                <div id="node-badge" class="badge"></div>

                <div id="node-title" class="fact-title">Select a Node</div>
                <div id="node-content" class="fact-desc">Click any node in the graph to view full details.</div>
                <div id="node-file" class="fact-meta"></div>
            </div>
        </div>
    </div>
    <script>
        const nodesRaw = {json_nodes};
        const linksRaw = {json_links};

        const nodes = new vis.DataSet(nodesRaw.map(n => ({{
            id: n.id,
            label: `[#${{n.id}}] ${{n.category}}\\n${{n.content}}`,
            color: {{ background: n.color, border: '#ffffff', highlight: {{ background: '#38bdf8', border: '#ffffff' }} }},
            shape: 'box',
            font: {{ color: '#ffffff', size: 12 }},
            margin: 10,
            raw: n
        }})));

        const edges = new vis.DataSet(linksRaw.map(l => ({{
            from: l.source,
            to: l.target,
            label: l.relation,
            arrows: 'to',
            font: {{ color: '#94a3b8', size: 10, align: 'top' }},
            color: {{ color: '#475569', highlight: '#38bdf8' }}
        }})));

        const container = document.getElementById('mynetwork');
        const data = {{ nodes: nodes, edges: edges }};
        const options = {{
            physics: {{
                solver: 'forceAtlas2Based',
                forceAtlas2Based: {{ gravitationalConstant: -50, centralGravity: 0.01, springLength: 100, springConstant: 0.08 }}
            }},
            interaction: {{ hover: true, tooltipDelay: 200 }}
        }};

        const network = new vis.Network(container, data, options);
        const sidebar = document.getElementById('sidebar');

        network.on("selectNode", function (params) {{
            if (params.nodes.length > 0) {{
                const nodeId = params.nodes[0];
                const nodeObj = nodesRaw.find(n => n.id === nodeId);
                if (nodeObj) {{
                    sidebar.style.display = 'block';
                    document.getElementById('node-badge').innerText = nodeObj.category;
                    document.getElementById('node-badge').style.backgroundColor = nodeObj.color;
                    document.getElementById('node-title').innerText = `Fact #${{nodeObj.id}}`;
                    document.getElementById('node-content').innerText = nodeObj.full_content;
                    document.getElementById('node-file').innerText = nodeObj.file_path ? `File: ${{nodeObj.file_path}}` : '';
                }}
            }}
        }});

        network.on("deselectNode", function () {{
            sidebar.style.display = 'none';

        }});
    </script>
</body>
</html>"""


        target = Path(out_path or (Path(self.db_path).parent / "memory_graph.html"))
        target.write_text(html_content, encoding="utf-8")
        return str(target)

    def audit(self, semantic_threshold: Optional[float] = None,
              lexical_threshold: Optional[float] = None,
              block: int = 512,
              disagreements_only: bool = False) -> Dict[str, Any]:
        """Read-only sweep for stale / contradictory ACTIVE fact pairs.

        Reports pairs that a human should reconcile with memory_supersede.
        It never mutates: a decision about which fact survives needs judgment
        (e.g. "six" vs "seven" rewritten scripts), so the sweeper only prints
        the worklist.

        Similarity is semantic cosine over the stored embeddings when both
        facts have one, and tokenized Jaccard otherwise. Semantic matters
        here: stale state facts are usually near-identical in MEANING but
        differently worded, which Jaccard scores far too low to catch.

        Template stubs (corpus_hygiene.STUB_PREFIXES) are excluded. They are
        whole-file boilerplate, so two of them read as near-identical while
        describing different files -- reporting those was the only thing the
        previous lexical audit found on the live corpus.

        The old implementation compared facts only within the same category
        and read them through get_state(limit=MAX_LIMIT), which silently
        truncated the sweep to the first 200 active facts.

        The similarity matrix is computed in row blocks so memory stays
        bounded as the corpus grows (a dense n x n float32 matrix is ~2 GB at
        n=20_000; blocks keep it at block x n).
        """
        semantic_threshold = (self.AUDIT_SEMANTIC_THRESHOLD if semantic_threshold is None
                              else float(semantic_threshold))
        lexical_threshold = (self.AUDIT_LEXICAL_THRESHOLD if lexical_threshold is None
                             else float(lexical_threshold))

        stub_sql = " AND ".join("content NOT LIKE ?" for _ in STUB_PREFIXES)
        rows = self.conn.execute(
            f"SELECT id, category, content, rationale, file_path, created_at "
            f"FROM memory WHERE deprecated_at IS NULL AND {stub_sql}",
            tuple(f"{p}%" for p in STUB_PREFIXES),
        ).fetchall()
        facts = {r["id"]: dict(r) for r in rows}

        pairs: Dict[Tuple[int, int], Dict[str, Any]] = {}
        method = "lexical"

        # --- semantic pass: stored embeddings, no model inference ---
        vectors: Dict[int, Any] = {}
        if _SEMANTIC_AVAILABLE and facts:
            for r in self.conn.execute(
                "SELECT fact_id, embedding FROM memory_embeddings"
            ):
                if r["fact_id"] in facts:
                    v = _blob_to_vec(r["embedding"])
                    if v is not None and v.size:
                        # _cosine_sim assumes L2-normalized input. Stored
                        # vectors normally already are; normalizing again is a
                        # no-op for those and rescues any that are not.
                        vectors[r["fact_id"]] = v / (np.linalg.norm(v) + 1e-9)

        if len(vectors) >= 2:
            method = "semantic"
            emb_ids = list(vectors)
            M = np.asarray([vectors[i] for i in emb_ids], dtype=np.float32)
            for start in range(0, len(emb_ids), max(1, block)):
                chunk_ids = emb_ids[start:start + max(1, block)]
                sims = M[start:start + max(1, block)] @ M.T
                for r_i, fact_a in enumerate(chunk_ids):
                    sims[r_i, start + r_i] = -1.0  # never self-pair
                    for r_j in np.nonzero(sims[r_i] >= semantic_threshold)[0]:
                        fact_b = emb_ids[int(r_j)]
                        if fact_a == fact_b:
                            continue
                        lo, hi = (fact_a, fact_b) if fact_a < fact_b else (fact_b, fact_a)
                        score = float(sims[r_i, int(r_j)])
                        prev = pairs.get((lo, hi))
                        if prev is None or score > prev["similarity"]:
                            pairs[(lo, hi)] = {
                                "similarity": round(score, 3), "method": "semantic",
                                "fact1_id": lo, "fact2_id": hi,
                            }
                del sims

        # --- lexical pass: only for facts with no usable embedding ---
        unembedded = [fid for fid in facts if fid not in vectors]
        if len(unembedded) >= 2:
            token_cache = {fid: tokenize_text(facts[fid]["content"]) for fid in unembedded}
            for i, fact_a in enumerate(unembedded):
                wa = token_cache[fact_a]
                if not wa:
                    continue
                for fact_b in unembedded[i + 1:]:
                    wb = token_cache[fact_b]
                    if not wb:
                        continue
                    union = len(wa | wb)
                    if not union:
                        continue
                    jaccard = len(wa & wb) / float(union)
                    if jaccard >= lexical_threshold:
                        lo, hi = (fact_a, fact_b) if fact_a < fact_b else (fact_b, fact_a)
                        pairs.setdefault((lo, hi), {
                            "similarity": round(jaccard, 3), "method": "lexical",
                            "fact1_id": lo, "fact2_id": hi,
                        })

        # --- assemble, keeping the original return shape backward compatible ---
        # Classify each pair as agree / disagree / unclear so a maintainer sees
        # the contradictions rather than reading every near-duplicate. See
        # classify_pair() for why "unclear" is a first-class answer.
        duplicates = []
        verdicts = {"agree": 0, "disagree": 0, "unclear": 0}
        for (lo, hi), meta in sorted(pairs.items(), key=lambda kv: -kv[1]["similarity"]):
            f1, f2 = facts[lo], facts[hi]
            verdict = classify_pair(
                {"id": lo, "content": f1["content"]},
                {"id": hi, "content": f2["content"]})
            verdicts[verdict["verdict"]] += 1
            if disagreements_only and verdict["verdict"] != "disagree":
                continue
            duplicates.append({
                "fact1_id": lo,
                "fact1_content": f1["content"],
                "fact1_category": f1["category"],
                "fact1_file_path": f1["file_path"],
                "fact1_created_at": f1["created_at"],
                "fact2_id": hi,
                "fact2_content": f2["content"],
                "fact2_category": f2["category"],
                "fact2_file_path": f2["file_path"],
                "fact2_created_at": f2["created_at"],
                "similarity": meta["similarity"],
                "method": meta["method"],
                "category": f1["category"],
                "cross_category": f1["category"] != f2["category"],
                "verdict": verdict["verdict"],
                "verdict_reason": verdict["reason"],
                "conflicts": verdict["conflicts"],
            })

        return {
            "total_active_checked": len(facts),
            "potential_duplicates_count": len(duplicates),
            "potential_duplicates": duplicates,
            "method": method,
            "embedded_checked": len(vectors),
            "stubs_excluded": True,
            "read_only": True,
            "thresholds": {"semantic": semantic_threshold, "lexical": lexical_threshold},
            "pairs_examined": sum(verdicts.values()),
            "pair_verdicts": verdicts,
            "disagreement_count": verdicts["disagree"],
            "disagreements_only": disagreements_only,
        }

    def export_markdown(self, category: Optional[str] = None) -> str:
        # limit=None: an export that silently drops facts is data loss.
        facts = self.get_state(category=category, limit=None)
        if not facts:
            return "# CogniGraph Snapshot\n\n*No active facts stored.*"

        lines = ["# CogniGraph Snapshot", f"*Generated at UTC: {datetime.datetime.now(datetime.timezone.utc).isoformat()}*", ""]
        by_cat: Dict[str, List[Dict[str, Any]]] = {}
        for f in facts:
            by_cat.setdefault(f["category"], []).append(f)

        category_order = ["component", "decision", "constraint", "state", "other"]
        for cat in category_order:
            if cat in by_cat:
                lines.append(f"## {cat.capitalize()}s")
                for f in by_cat[cat]:
                    prov = []
                    if f["file_path"]:
                        prov.append(f"file: {f['file_path']}")
                    if f["confidence"] != "confirmed":
                        prov.append(f"confidence: {f['confidence']}")
                    if f["tags"]:
                        prov.append(f"tags: {f['tags']}")
                    prov_str = f" *({', '.join(prov)})*" if prov else ""
                    lines.append(f"- **[{f['id']}]** {f['content']}{prov_str}")
                    if f["rationale"]:
                        lines.append(f"  - *Rationale*: {f['rationale']}")
                lines.append("")
        return "\n".join(lines)

    def prepare_task(self, intent: str, file_paths: Optional[List[str]] = None, limit: int = 5) -> str:
        """
        PRE-CODING LIFECYCLE BRIEFING:
        Generates a high-density, token-efficient 'Flight Briefing' before coding:
        1. Architectural Context & Prior Decisions.
        2. Active Constraints & Traps.
        3. Target Files Safety Matrix & Dependencies.
        4. Latest State / Test Baseline.
        5. Proactive Context Suggestions (when co-retrieval history exists).
        """
        sections = [f"# PRE-CODING FLIGHT BRIEFING: {intent}", ""]

        # 1. Intent Context (Hybrid RRF + Knowledge Graph), ranked by detected task intent
        detected_intent = self._detect_intent(intent)
        ctx = self.get_context(intent, limit=limit, intent=detected_intent)
        sections.append(ctx)
        sections.append("")

        # Project orientation: indexed inventory and code-graph size
        try:
            sections.append(self.project_shape())
            sections.append("")
        except Exception:
            pass

        # 2. Target File Impact & Dependencies
        if file_paths:
            sections.append("### Target Files Safety Matrix")
            safety = self.check_safety(file_paths)
            if safety.get("cross_dependencies"):
                sections.append(f"**[WARNING] Cross-File Dependencies ({len(safety['cross_dependencies'])} detected):**")
                for dep in safety["cross_dependencies"]:
                    sections.append(f"- `{dep['source_file']}` --[{dep['relation']}]--> `{dep['target_file']}`")
                sections.append("")
            if safety.get("structural_dependencies"):
                sections.append(f"**[WARNING] Structural Deps ({len(safety['structural_dependencies'])} call/import edges):**")
                for dep in safety["structural_dependencies"][:20]:
                    sections.append(f"- `{dep['caller_file']}:{dep['caller_symbol']}` --[{dep['edge_type']}]--> `{dep['callee_file']}:{dep['callee_symbol']}`")
                sections.append("")
            if len(file_paths) > 1:
                sections.append(self.get_dependency_map(file_paths))
                sections.append("")
            for fp in file_paths:
                sections.append(self.get_file_context(fp))
                sections.append("")

        # 3. Latest Verified Baseline & State
        # Only the newest checkpoint, and capped: state facts are session
        # history, not working context — uncapped they dominate the briefing.
        recent_states = self.get_state(category="state", limit=1)
        if recent_states:
            sections.append("### Latest Verified Baseline & State")
            for s in recent_states:
                content = s["content"] or ""
                if " | Verified Baseline: " in content:
                    body, baseline = content.split(" | Verified Baseline: ", 1)
                    content = (f"{self._cap_text(body, 300)}"
                               f" | Verified Baseline: {self._cap_text(baseline, 300)}")
                else:
                    content = self._cap_text(content, 300)
                sections.append(f"- **[STATE #{s['id']}]** {content}")
            sections.append("")

        # 4. Proactive suggestions: facts historically co-retrieved with the
        # current context, surfaced here to save the agent a round-trip.
        try:
            seed_results = self.query_hybrid(intent, limit=limit, log_usage=False)
            seed_ids = [r["id"] for r in seed_results if r.get("source") == "memory"]
            if seed_ids:
                suggestions = self.suggest_proactive_context(seed_ids, limit=3)
                if not suggestions.startswith("*"):
                    sections.append(suggestions)
                    sections.append("")
        except Exception:
            pass

        return "\n".join(sections)

    def checkpoint_task(self,
                        summary: str,
                        components: Optional[List[Dict[str, Any]]] = None,
                        decisions: Optional[List[Dict[str, Any]]] = None,
                        test_baseline: Optional[str] = None,
                        links: Optional[List[Tuple[int, int, str]]] = None,
                        session_id: Optional[str] = None,
                        failures: Optional[List[Dict[str, Any]]] = None,
                        unfinished: Optional[List[str]] = None,
                        blockers: Optional[List[str]] = None,
                        acceptance_criteria: Optional[List[str]] = None,
                        verified: Optional[bool] = None,
                        evidence_type: Optional[str] = None,
                        source_ref: Optional[str] = None) -> Dict[str, Any]:
        """
        POST-CODING LIFECYCLE CHECKPOINT:
        Atomically records all achievements, new components, architectural decisions,
        code provenance anchors, and the verified test baseline into the live
        `memory` table (see write() intake contract).
        """
        written_ids = []
        
        # 0. Negative knowledge: approaches that failed and why, stored as
        # constraints so no future agent re-derives the same dead end.
        if failures:
            for fl in failures:
                fid = self.write(
                    category="constraint",
                    content=fl.get("content", ""),
                    rationale=fl.get("rationale") or fl.get("why_failed"),
                    confidence="confirmed",
                    tags="negative-knowledge,known-failure",
                    importance=4,
                    session_id=session_id,
                    evidence_type=(fl.get("evidence_type") if isinstance(fl, dict) else None) or evidence_type or "task_checkpoint",
                    source_ref=(fl.get("source_ref") if isinstance(fl, dict) else None) or source_ref,
                )
                written_ids.append(fid)

        # 1. Write Decisions
        if decisions:
            for d in decisions:
                did = self.write(
                    category="decision",
                    content=d["content"],
                    rationale=d.get("rationale"),
                    confidence=d.get("confidence", "confirmed"),
                    file_path=d.get("file_path"),
                    tags=d.get("tags"),
                    importance=d.get("importance", 4),
                    session_id=session_id,
                    evidence_type=d.get("evidence_type", evidence_type) or "task_checkpoint",
                    source_ref=d.get("source_ref", source_ref) or None,
                )
                written_ids.append(did)

        # 2. Write Components with Code Provenance
        if components:
            for c in components:
                cid = self.write(
                    category="component",
                    content=c["content"],
                    rationale=c.get("rationale"),
                    confidence=c.get("confidence", "confirmed"),
                    file_path=c.get("file_path"),
                    line_start=c.get("line_start"),
                    line_end=c.get("line_end"),
                    code_hash=c.get("code_hash"),
                    tags=c.get("tags"),
                    importance=c.get("importance", 3),
                    session_id=session_id,
                    evidence_type=c.get("evidence_type", evidence_type) or "task_checkpoint",
                    source_ref=c.get("source_ref", source_ref) or None,
                )
                written_ids.append(cid)

        # 3. Create Graph Edges (skip edges referencing missing/duplicate facts)
        if links:
            for src, tgt, rel in links:
                try:
                    self.link_facts(src, tgt, rel)
                except ValueError:
                    pass

        # 4a. Unfinished items: separate state facts tagged unfinished,backlog
        unfinished_ids = []
        for item in (unfinished or []):
            uid = self.write(
                category="state",
                content=item,
                rationale="Post-task verification checkpoint (unfinished)",
                confidence="confirmed",
                tags="unfinished,backlog",
                session_id=session_id,
                importance=4,
                evidence_type=evidence_type or "task_checkpoint",
                source_ref=source_ref,
            )
            unfinished_ids.append(uid)
            written_ids.append(uid)

        # 4b. Blockers: separate state facts tagged blocker,open
        blocker_ids = []
        for item in (blockers or []):
            bid = self.write(
                category="state",
                content=item,
                rationale="Post-task verification checkpoint (blocker)",
                confidence="confirmed",
                tags="blocker,open",
                session_id=session_id,
                importance=4,
                evidence_type=evidence_type or "task_checkpoint",
                source_ref=source_ref,
            )
            blocker_ids.append(bid)
            written_ids.append(bid)

        # 4c. Acceptance criteria: separate state facts tagged acceptance-criteria
        acceptance_ids = []
        for item in (acceptance_criteria or []):
            aid = self.write(
                category="state",
                content=item,
                rationale="Post-task verification checkpoint (acceptance criterion)",
                confidence="confirmed",
                tags="acceptance-criteria",
                session_id=session_id,
                importance=4,
                evidence_type=evidence_type or "task_checkpoint",
                source_ref=source_ref,
            )
            acceptance_ids.append(aid)
            written_ids.append(aid)

        # 4d. Record State Milestone & Test Baseline
        state_content = summary
        if verified is None:
            state_content += " | Verified: unknown"
        elif verified:
            state_content += " | Verified: yes"
        else:
            state_content += " | Verified: no"
        if acceptance_criteria is not None:
            state_content += f" | Acceptance criteria: {len(acceptance_criteria)}"
        if test_baseline:
            state_content += f" | Verified Baseline: {test_baseline}"

        state_id = self.write(
            category="state",
            content=state_content,
            rationale="Post-task verification checkpoint",
            confidence="confirmed",
            session_id=session_id,
            importance=4,
            evidence_type=evidence_type or "task_checkpoint",
            source_ref=source_ref,
        )
        written_ids.append(state_id)

        recorded = {
            "unfinished": len(unfinished_ids),
            "blockers": len(blocker_ids),
            "acceptance_criteria": len(acceptance_ids),
            "state_milestone": 1,
        }

        return {
            "status": "CHECKPOINT_RECORDED",
            "state_id": -state_id if state_id < 0 else state_id,
            "state_was_duplicate": state_id < 0,
            "total_facts_written": sum(1 for i in written_ids if i > 0),
            "duplicates_skipped": sum(1 for i in written_ids if i < 0),
            "duplicates_of_existing_ids": [-i for i in written_ids if i < 0],
            "fact_ids": [-i if i < 0 else i for i in written_ids],
            "written_ids": [-i if i < 0 else i for i in written_ids],
            "recorded": recorded,
        }

