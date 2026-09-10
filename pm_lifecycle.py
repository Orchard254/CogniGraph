"""Lifecycle layer: provenance verification, maintenance (prune/consolidate/optimize/audit), exports, and the agent prepare/checkpoint hooks."""
import datetime
import hashlib
import json
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from pm_common import (
    tokenize_text, MAX_LIMIT,
    _vec_to_blob, _get_embed_model,
    _EMBED_MODEL_NAME, _SEMANTIC_AVAILABLE, np,
)


class LifecycleMixin:
    def flag_fact(self, fact_id: int, note: str = "", flag_type: str = "misleading",
                  flagged_by: Optional[str] = None) -> Dict[str, Any]:
        """CORRECTION HOOK: record that a served fact misled the agent.

        Flags are stamped inline in file briefings so future sessions see the
        warning at priming time instead of re-consuming the bad fact at face
        value. Prefer supersede() when the corrected content is known.
        """
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

        return {
            "status": "CHECKED",
            "files_analyzed": len(clean_paths),
            "cross_dependencies_count": len(cross_dependencies),
            "cross_dependencies": cross_dependencies,
            "briefings": file_briefings,
        }

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

    def export_interactive_html(self, out_path: Optional[str] = None) -> str:
        """
        Generate a self-contained standalone single-file HTML page containing an
        interactive 2D graph visualizer of project memory facts and relationship edges.
        """
        facts = self.get_state(limit=MAX_LIMIT)
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

    def audit(self) -> Dict[str, Any]:
        active_facts = self.get_state(limit=MAX_LIMIT)
        duplicates = []
        for i in range(len(active_facts)):
            for j in range(i + 1, len(active_facts)):
                f1 = active_facts[i]
                f2 = active_facts[j]
                if f1["category"] == f2["category"]:
                    # Skip file-coverage vs file-coverage with different file_path (templated docs) — not true dupes
                    fp1 = (f1.get("file_path") or "").strip().replace("\\", "/").lower()
                    fp2 = (f2.get("file_path") or "").strip().replace("\\", "/").lower()
                    tags1 = (f1.get("tags") or "").lower()
                    tags2 = (f2.get("tags") or "").lower()
                    if fp1 and fp2 and fp1 != fp2:
                        # Different files — only flag if both are non-file-coverage or one is not templated
                        if "file-coverage" in tags1 or "file-coverage" in tags2:
                            continue
                    w1 = tokenize_text(f1["content"])
                    w2 = tokenize_text(f2["content"])
                    if w1 and w2:
                        jaccard = len(w1 & w2) / float(len(w1 | w2))
                        if jaccard >= 0.65:
                            duplicates.append({
                                "fact1_id": f1["id"],
                                "fact1_content": f1["content"],
                                "fact2_id": f2["id"],
                                "fact2_content": f2["content"],
                                "similarity": round(jaccard, 2),
                                "category": f1["category"]
                            })
        return {
            "total_active_checked": len(active_facts),
            "potential_duplicates_count": len(duplicates),
            "potential_duplicates": duplicates,
        }

    def export_markdown(self, category: Optional[str] = None) -> str:
        facts = self.get_state(category=category, limit=MAX_LIMIT)
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
                        failures: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
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
                )
                written_ids.append(cid)

        # 3. Create Graph Edges (skip edges referencing missing/duplicate facts)
        if links:
            for src, tgt, rel in links:
                try:
                    self.link_facts(src, tgt, rel)
                except ValueError:
                    pass

        # 4. Record State Milestone & Test Baseline
        state_content = summary
        if test_baseline:
            state_content += f" | Verified Baseline: {test_baseline}"
        
        state_id = self.write(
            category="state",
            content=state_content,
            rationale="Post-task verification checkpoint",
            confidence="confirmed",
            session_id=session_id,
            importance=4,
        )
        written_ids.append(state_id)

        return {
            "status": "CHECKPOINT_RECORDED",
            "state_id": -state_id if state_id < 0 else state_id,
            "state_was_duplicate": state_id < 0,
            "total_facts_written": sum(1 for i in written_ids if i > 0),
            "duplicates_skipped": sum(1 for i in written_ids if i < 0),
            "duplicates_of_existing_ids": [-i for i in written_ids if i < 0],
            "fact_ids": [-i if i < 0 else i for i in written_ids],
        }

