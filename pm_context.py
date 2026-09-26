"""Context layer: LLM priming output - context briefings, file briefings, dependency maps, and proactive suggestions."""
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Any

class ContextMixin:
    _CODING_INTENTS = {"fix", "implement", "refactor", "add", "remove", "optimize", "test", "debug"}
    _FACT_CONTENT_CAP = 500
    _FACT_RATIONALE_CAP = 300
    _DOC_DUMP_MIN_LEN = 200

    def _cap_text(self, text: Optional[str], cap: int) -> str:
        if text and len(text) > cap:
            return text[:cap].rstrip() + " …(truncated)"
        return text or ""

    def _fact_flag_stamp(self, fact_id: int) -> str:
        """Latest correction flag for a fact, rendered inline at priming time."""
        try:
            row = self.conn.execute(
                "SELECT flag_type, note FROM fact_flags WHERE fact_id = ? ORDER BY id DESC LIMIT 1",
                (fact_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return ""
        if not row:
            return ""
        return f"FLAGGED {row['flag_type'].upper()}" + (f": {row['note']}" if row["note"] else "")

    def _call_graph_lines(self, clean_path: str, max_edges: int = 15) -> List[str]:
        """Call edges touching this file: outgoing calls from its definitions
        and incoming calls from other files, from the structural code graph."""
        try:
            outgoing = self.conn.execute(
                "SELECT n.name AS c_name, n.class_name AS c_class, "
                "cn.name AS t_name, cn.class_name AS t_class, cn.file_path AS t_file "
                "FROM structural_edges e "
                "JOIN structural_nodes n ON e.caller_id = n.id "
                "JOIN structural_nodes cn ON e.callee_id = cn.id "
                "WHERE e.edge_type = 'calls' AND n.file_path = ? "
                "ORDER BY (cn.file_path != ?) DESC, n.line_start LIMIT ?",
                (clean_path, clean_path, max_edges),
            ).fetchall()
            incoming = self.conn.execute(
                "SELECT cn.name AS c_name, cn.class_name AS c_class, cn.file_path AS c_file, "
                "n.name AS t_name, n.class_name AS t_class "
                "FROM structural_edges e "
                "JOIN structural_nodes n ON e.callee_id = n.id "
                "JOIN structural_nodes cn ON e.caller_id = cn.id "
                "WHERE e.edge_type = 'calls' AND n.file_path = ? AND cn.file_path != ? "
                "ORDER BY cn.file_path LIMIT ?",
                (clean_path, clean_path, max_edges),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

        def label(name, cls):
            return f"{cls}.{name}" if cls else name

        out: List[str] = []
        for r in outgoing:
            loc = "" if r["t_file"] == clean_path else f" in `{r['t_file']}`"
            out.append(f"- `{label(r['c_name'], r['c_class'])}()` calls `{label(r['t_name'], r['t_class'])}()`{loc}")
        for r in incoming:
            out.append(f"- `{label(r['t_name'], r['t_class'])}()` is called by `{label(r['c_name'], r['c_class'])}()` in `{r['c_file']}`")
        return out

    def _suppress_doc_noise(self, results: List[Dict[str, Any]], intent: str) -> List[Dict[str, Any]]:
        """For coding intents, demote long bootstrap doc-section dumps to the end of the list.

        Bootstrap ingests whole README/AGENTS.md sections as 'other' facts; those
        routinely exceed the per-fact budget and crowd out code facts for concrete
        coding tasks, so they are ranked last rather than dropped.
        """
        if intent not in self._CODING_INTENTS:
            return results

        def is_doc_dump(f):
            tags = (f.get("tags") or "").lower()
            return (
                f.get("category") == "other"
                and "bootstrap" in tags
                and "docs" in tags
                and len(f.get("content") or "") > self._DOC_DUMP_MIN_LEN
            )

        return [f for f in results if not is_doc_dump(f)] + [f for f in results if is_doc_dump(f)]

    def _expand_context_for_intent(self, base_results: List[Dict[str, Any]], intent: str) -> List[Dict[str, Any]]:
        """
        Expand context based on detected intent. Returns additional relevant facts.
        """
        expanded_results = base_results.copy()
        seen_ids = {f['id'] for f in base_results}
        
        # Intent-based fact expansion rules
        intent_expansion_rules = {
            'fix': [
                ('constraint', 'security', 'authentication', 'validation'),
                ('component', 'error handling', 'exception'),
                ('decision', 'architecture decision', 'design pattern')
            ],
            'implement': [
                ('component', 'architecture', 'design pattern'),
                ('decision', 'architecture decision', 'best practice'),
                ('constraint', 'performance', 'scalability')
            ],
            'refactor': [
                ('component', 'architecture', 'design pattern'),
                ('decision', 'refactoring decision', 'migration plan'),
                ('constraint', 'maintainability', 'code quality')
            ],
            'add': [
                ('component', 'feature', 'capability'),
                ('decision', 'feature decision', 'requirement'),
                ('constraint', 'api', 'interface')
            ],
            'test': [
                ('component', 'test framework', 'testing strategy'),
                ('decision', 'testing decision', 'coverage requirement'),
                ('constraint', 'quality assurance', 'test data')
            ],
            'optimize': [
                ('component', 'performance bottleneck', 'optimization target'),
                ('decision', 'performance decision', 'benchmark'),
                ('constraint', 'resource usage', 'memory', 'cpu')
            ]
        }
        
        # Get expansion rules for detected intent
        rules = intent_expansion_rules.get(intent, [])
        
        # Expand with related facts
        for category, *tags in rules:
            for tag in tags:
                # Query for related facts
                related_facts = self.query(tag, category=category, limit=5)
                for fact in related_facts:
                    if fact['id'] not in seen_ids:
                        expanded_results.append(fact)
                        seen_ids.add(fact['id'])
        
        return expanded_results

    def _prioritize_facts_for_llm(self, facts: List[Dict[str, Any]], intent: str) -> List[Dict[str, Any]]:
        """
        Prioritize facts for LLM consumption based on intent and importance.
        Importance-5 facts always come first; the remaining facts are ordered by
        the intent-specific category priority. Never drops facts.
        """
        if not facts:
            return []

        def fact_key(f):
            return (f.get("source", "memory"), f.get("id"))

        def by_importance(fl):
            return sorted(fl, key=lambda x: x.get("importance", 3), reverse=True)

        critical_facts = by_importance([f for f in facts if f.get("importance", 3) >= 5])
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for f in facts:
            if f.get("importance", 3) >= 5:
                continue
            buckets.setdefault(f.get("category", "other"), []).append(f)

        intent_priority_order = {
            'fix': ['constraint', 'decision', 'component', 'state', 'other'],
            'implement': ['decision', 'component', 'constraint', 'state', 'other'],
            'refactor': ['decision', 'constraint', 'component', 'state', 'other'],

            'add': ['component', 'decision', 'constraint', 'state', 'other'],
            'test': ['constraint', 'component', 'decision', 'state', 'other'],
            'optimize': ['constraint', 'component', 'decision', 'state', 'other'],
        }
        priority_order = intent_priority_order.get(intent, ['decision', 'constraint', 'component', 'state', 'other'])

        prioritized = list(critical_facts)
        seen = {fact_key(f) for f in prioritized}
        for category in priority_order:
            for f in by_importance(buckets.get(category, [])):
                k = fact_key(f)
                if k not in seen:
                    seen.add(k)
                    prioritized.append(f)
        # Any categories not covered by the priority order still keep their facts
        for category, fl in buckets.items():
            if category in priority_order:
                continue
            for f in by_importance(fl):
                k = fact_key(f)
                if k not in seen:
                    seen.add(k)
                    prioritized.append(f)

        return prioritized[:len(facts) * 2]

    def get_context(self, query: str, limit: int = 5, repo_id: Optional[int] = None, max_chars: int = 8000, intent: str = "", agent_type: str = "") -> str:
        """
        Enhanced unified context retriever with intent-based expansion and LLM optimization.
        
        Args:
            query: The search query or task description
            limit: Maximum number of primary facts to return
            repo_id: Repository ID for episodic filtering
            max_chars: Maximum character budget for output
            intent: Optional explicit intent (overrides automatic detection)
            
        Returns:
            Rich Markdown Context block optimized for LLM prompt injection with:
            - Intent-based context expansion
            - Critical fact prioritization
            - Noise reduction
            - Enhanced formatting for LLM consumption
        """
        # Auto-detect intent if not provided
        if not intent:
            detected_intent = self._detect_intent(query)
        else:
            detected_intent = intent
        
        # Get base results from hybrid search (intent-aware, agent-lane-weighted ranking)
        results = self.query_hybrid(query, limit=limit * 2, expand_graph=True, repo_id=repo_id, intent=detected_intent, agent_type=agent_type)
        
        # Intent-based expansion
        if len(results) < limit * 3:  # Only expand if we have room
            expanded_results = self._expand_context_for_intent(results, detected_intent)
        else:
            expanded_results = results
        
        # Prioritize facts for LLM consumption, then demote documentation noise for coding intents
        prioritized_results = self._prioritize_facts_for_llm(expanded_results[:limit * 3], detected_intent)
        prioritized_results = self._suppress_doc_noise(prioritized_results, detected_intent)
        
        # Limit to requested count
        final_results = prioritized_results[:limit]
        
        if not final_results:
            return f"### CogniGraph Context for: '{query}'\n*No matching facts found in memory.*"

        lines = [f"### CogniGraph Context for: '{query}'", f"*Intent detected: **{detected_intent.upper()}** task*", ""]
        seen_ids: set[tuple] = set()

        for idx, f in enumerate(final_results, 1):
            source = f.get("source", "memory")
            seen_ids.add((source, f["id"]))
            prov: list[str] = []
            
            # Enhanced provenance with intent-based highlighting
            if source == "episodic":
                branch_id = f.get("branch_id")
                if branch_id is not None:
                    prov.append(f"branch: canonical #{branch_id}")
                if f.get("created_revision") is not None:
                    prov.append(f"rev: {f['created_revision']}")
            else:
                if f["file_path"]:
                    lines_range = f":L{f['line_start']}-L{f['line_end']}" if f.get("line_start") else ""
                    prov.append(f"file: {f['file_path']}{lines_range}")
            
            if f["confidence"] != "confirmed":
                prov.append(f"confidence: {f['confidence']}")
            if f.get("tags"):
                prov.append(f"tags: {f['tags']}")
            
            # Enhanced importance highlighting

            importance = f.get("importance", 3)
            if importance >= 5:
                importance_tag = " **[CRITICAL]**"
                prov.append("high-impact constraint")
            elif importance >= 4:
                importance_tag = " **[IMPORTANT]**"
            elif importance >= 3:
                importance_tag = ""
            else:
                importance_tag = " **[LOW PRIORITY]**"
            
            # Color-coded category labels for better LLM parsing
            category_colors = {
                'component': '📦',
                'decision': '💡',
                'constraint': '⚠️',
                'state': '📊',
                'other': '📝'
            }
            category_emoji = category_colors.get(f['category'], '📌')
            
            # Visible source label for episodic
            if source == "episodic":
                label = f"EPISODIC/{f['category'].upper()} #{f['id']}"
            else:
                label = f"{category_emoji} {f['category'].upper()} #{f['id']}"
            
            prov_str = f" *({', '.join(prov)})*" if prov else ""
            content = self._cap_text(f["content"], self._FACT_CONTENT_CAP)
            lines.append(f"{idx}. **[{label}]**{importance_tag} {content}{prov_str}")
            
            # Enhanced rationale formatting
            if f.get("rationale"):
                lines.append(f"   - *📋 Rationale*: {self._cap_text(f['rationale'], self._FACT_RATIONALE_CAP)}")
            
            # Intent-based graph neighbor highlighting
            if f.get("graph_neighbors"):
                for edge in f["graph_neighbors"]:
                    target_id = edge["target_id"]
                    if ("memory", target_id) not in seen_ids:
                        seen_ids.add(("memory", target_id))
                        relation_emoji = "🔗" if edge['relation'] == 'depends_on' else "🔄"
                        lines.append(f"   - {relation_emoji} **[{edge['relation']}]** → [{edge['target_category'].upper()} #{target_id}] {self._cap_text(edge['target_content'], self._FACT_CONTENT_CAP)}")

            # Incoming graph neighbors with enhanced formatting
            if f.get("incoming_neighbors"):
                for inc in f["incoming_neighbors"]:
                    source_id = inc["source_id"]
                    if ("memory", source_id) not in seen_ids:
                        seen_ids.add(("memory", source_id))
                        lines.append(f"   - 🔙 **[required by ({inc['relation']})]** ← [{inc['source_category'].upper()} #{source_id}] {self._cap_text(inc['source_content'], self._FACT_CONTENT_CAP)}")

            lines.append("")

        result = "\n".join(lines)
        if len(result) > max_chars:
            result = result[:max_chars] + f"\n\n*...truncated to {max_chars} chars (budget), {len(lines)} lines total...*"
        

        # Add intent-based usage tip
        if detected_intent in ['fix', 'refactor', 'optimize']:
            result += f"\n---\n*💡 Tip: Focus on constraints and decisions for {detected_intent} tasks to avoid breaking changes.*"
        
        return result

    def _callee_constraint_lines(self, clean_path: str, seen_ids: set) -> List[str]:
        """Constraints anchored to files this file calls into (one hop out
        via structural_edges). Caller-side briefings otherwise omit
        callee-anchored rules that govern the call (e.g. pm_search.py's
        log_usage discipline for query_hybrid callers). Capped at 5 facts
        and empty (not a placeholder) when there is nothing to show, so
        the token budget is untouched in the common case."""
        try:
            callee_files = [
                r["t_file"] for r in self.conn.execute(
                    "SELECT DISTINCT cn.file_path AS t_file "
                    "FROM structural_edges e "
                    "JOIN structural_nodes n ON e.caller_id = n.id "
                    "JOIN structural_nodes cn ON e.callee_id = cn.id "
                    "WHERE e.edge_type = 'calls' AND n.file_path = ? AND cn.file_path != ? "
                    "LIMIT 10",
                    (clean_path, clean_path),
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            return []
        callee_files = [t for t in callee_files if t]
        if not callee_files:
            return []
        try:
            cand_rows = self.conn.execute(
                "SELECT id, content, file_path, importance FROM memory "
                "WHERE category = 'constraint' AND deprecated_at IS NULL AND importance >= 4 "
                "ORDER BY importance DESC LIMIT 40",
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        norm_targets = [t.strip().replace("\\", "/").lower() for t in callee_files]
        picked = []
        for cr in cand_rows:
            if cr["id"] in seen_ids:
                continue
            cfp = (cr["file_path"] or "").strip().replace("\\", "/").lower()
            if cfp and any(self._paths_match(cfp, t) for t in norm_targets):
                picked.append(cr)
                if len(picked) >= 5:
                    break
        if not picked:
            return []
        lines = ["#### Constraints From Called Files (call safety):"]
        for c in picked:
            seen_ids.add(c["id"])
            lines.append(f"- **[CONSTRAINT #{c['id']}]** (via `{c['file_path']}`) "
                         f"{self._cap_text(c['content'], self._FACT_CONTENT_CAP)}")
        lines.append("")
        return lines

    def get_file_context(self, file_path: str) -> str:
        """
        File-Centric Impact Analysis Briefing:
        Given a file path, retrieves:
        1. All stored facts, decisions, and constraints for this file.
        2. All linked files and graph dependencies (outgoing & incoming edges).
        3. Critical editing constraints/traps.
        Formats a structured Markdown briefing before modifying the file.

        Note: This is file-path-centric and intentionally out of scope for episodic
        memories, which carry no file_path (episodic is branch/revision-centric).
        It reads only memory + memory_edges (+ structural_nodes via graph), not
        episodic_memories. This is by design, not a gap — episodic retrieval is
        via query_hybrid/get_context, not via file path.
        """
        clean_path = file_path.strip().replace("\\", "/").lower()
        # limit=None: the briefing must see EVERY active fact. Clamping to
        # MAX_LIMIT hid facts about the target file entirely once the corpus
        # passed 200 facts. The fact #498 standing rule (do not uncap the
        # STRUCTURAL lane without a retrieval re-tuning pass) does not apply
        # here: this read feeds no ranking, only the pre-edit briefing.
        all_active = self.get_state(limit=None)

        direct_facts = []
        for f in all_active:
            if f["file_path"]:
                fp = f["file_path"].strip().replace("\\", "/").lower()
                if self._paths_match(clean_path, fp):
                    direct_facts.append(f)

        filename = Path(clean_path).name
        if filename:
            fts_matches = self.query(filename, limit=20)
            for f in fts_matches:
                if f["id"] not in [df["id"] for df in direct_facts]:
                    direct_facts.append(f)

        # State facts are session history, not file knowledge: the latest one
        # is already served (capped) by prepare_task's baseline section, so
        # duplicating full checkpoint prose here just burns the token budget.
        direct_facts = [f for f in direct_facts if f["category"] != "state"]

        # Long bootstrap doc-section dumps that happen to mention this file's
        # name (via the FTS filename fallback) are documentation, not file
        # knowledge — the filename match would otherwise leak them in.
        def _is_doc_dump(f):
            tags = (f.get("tags") or "").lower()
            return (
                f.get("category") == "other"
                and "bootstrap" in tags
                and "docs" in tags
                and len(f.get("content") or "") > self._DOC_DUMP_MIN_LEN
            )

        direct_facts = [f for f in direct_facts if not _is_doc_dump(f)]

        if not direct_facts:
            struct_only = self._structural_briefing_lines(clean_path)
            if struct_only:
                seen: set = set()
                block = [
                    f"### File Memory Briefing for: `{file_path}`",
                    "*No stored facts recorded for this file yet, but structural index has entries:*",
                    "",
                    "#### Code Structure (functions / classes / imports):",
                    *struct_only,
                    "",
                    *self._callee_constraint_lines(clean_path, seen),
                ]
                graph_only = self._call_graph_lines(clean_path)
                if graph_only:
                    block += ["", "#### Code Graph (calls):", *graph_only]
                return "\n".join(block)
            return (
                f"### File Memory Briefing for: `{file_path}`\n"
                "*⚠️ This file is UNMAPPED in memory — no facts and no structural index entries. "
                "Do not reason about it from memory; read the file directly before editing.*"
            )

        lines = [f"### File Memory Briefing for: `{file_path}`", ""]
        seen_ids = set()
        impacted_files = set()
        constraints = []

        lines.append("#### Direct Components & Decisions:")
        for f in direct_facts:
            seen_ids.add(f["id"])
            if f["category"] == "constraint":
                constraints.append(f)

            stamps = []
            flag_note = self._fact_flag_stamp(f["id"])
            if flag_note:
                stamps.append(f"⚠️ {flag_note}")
            code_hash = f.get("code_hash") or ""
            if code_hash and (len(code_hash) < 16 or any(c not in "0123456789abcdef" for c in code_hash.lower())):
                stamps.append("provenance unverified")
            stamp_text = f" *({'; '.join(stamps)})*" if stamps else ""
            lines.append(f"- **[{f['category'].upper()} #{f['id']}]**{stamp_text} "
                         f"{self._cap_text(f['content'], self._FACT_CONTENT_CAP)}")
            if f.get("rationale"):
                lines.append(f"  - *Rationale*: {self._cap_text(f['rationale'], self._FACT_RATIONALE_CAP)}")

            out_edges = self.get_graph(f["id"], max_depth=1)
            for edge in out_edges:
                tid = edge["target_id"]
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    lines.append(f"  - **[{edge['relation']}]** -> [{edge['target_category'].upper()} #{tid}] "
                                 f"{self._cap_text(edge['target_content'], self._FACT_CONTENT_CAP)}")
                    target_row = self.conn.execute("SELECT file_path FROM memory WHERE id = ?", (tid,)).fetchone()
                    if target_row and target_row["file_path"]:
                        impacted_files.add((edge["relation"], target_row["file_path"]))

            inc_rows = self.conn.execute(
                "SELECT e.relation, m.id, m.category, m.content, m.file_path "
                "FROM memory_edges e JOIN memory m ON e.source_id = m.id "
                "WHERE e.target_id = ? AND m.deprecated_at IS NULL", (f["id"],)
            ).fetchall()
            for inc in inc_rows:
                if inc["id"] not in seen_ids:
                    seen_ids.add(inc["id"])
                    lines.append(f"  - **[required_by ({inc['relation']})]** <- [{inc['category'].upper()} #{inc['id']}] "
                                 f"{self._cap_text(inc['content'], self._FACT_CONTENT_CAP)}")
                    if inc["file_path"]:
                        impacted_files.add((f"reverse_{inc['relation']}", inc["file_path"]))

            lines.append("")

        if constraints:
            lines.append("#### Editing Constraints & Traps:")
            for c in constraints:
                lines.append(f"- **[CONSTRAINT #{c['id']}]** {self._cap_text(c['content'], self._FACT_CONTENT_CAP)}")
            lines.append("")

        # Cross-file call safety lives in _callee_constraint_lines (also
        # served on the struct-only path above): constraints anchored to
        # files this file calls into, capped and omitted when empty.
        lines.extend(self._callee_constraint_lines(clean_path, seen_ids))

        if impacted_files:
            lines.append("#### Impacted / Linked Files:")
            for rel, imp_file in impacted_files:
                lines.append(f"- `{imp_file}` ({rel})")
            lines.append("")

        struct_lines = self._structural_briefing_lines(clean_path)
        if struct_lines:
            lines.append("#### Code Structure (functions / classes / imports):")
            lines.extend(struct_lines)
            lines.append("")

        graph_lines = self._call_graph_lines(clean_path)
        if graph_lines:
            lines.append("#### Code Graph (calls):")
            lines.extend(graph_lines)
            lines.append("*For one-hop questions ('who calls X?') use memory_who_calls instead of re-reading files.*")
            lines.append("")

        return "\n".join(lines)

    def _structural_briefing_lines(self, clean_path: str, max_nodes: int = 30) -> List[str]:
        """Render structural_nodes for one file as compact briefing lines.

        Grouped: classes first (with their methods indented under them), then
        top-level functions, then imports collapsed to a single summary line.
        """
        try:
            rows = self.conn.execute(
                "SELECT node_type, name, class_name, line_start, line_end "
                "FROM structural_nodes WHERE file_path = ? "
                "ORDER BY line_start LIMIT ?",
                (clean_path, 200),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        if not rows:
            return []

        classes: Dict[str, List[str]] = {}
        functions: List[str] = []
        import_count = 0

        shown = 0
        for r in rows:
            if shown >= max_nodes and r["node_type"] != "import":
                continue
            rng = f"L{r['line_start']}-{r['line_end']}"
            if r["node_type"] == "class":
                classes.setdefault(r["name"], [])
                classes[r["name"]].insert(0, f"- `class {r['name']}` ({rng})")
                shown += 1
            elif r["node_type"] == "method":
                cls = r["class_name"] or "(unknown)"
                classes.setdefault(cls, []).append(f"  - `{r['name']}()` ({rng})")
                shown += 1
            elif r["node_type"] == "function":
                functions.append(f"- `{r['name']}()` ({rng})")
                shown += 1
            else:
                import_count += 1

        out: List[str] = []
        for cls_name, entries in classes.items():
            out.extend(entries)
        out.extend(functions)
        if import_count:
            out.append(f"- ({import_count} import statement{'s' if import_count != 1 else ''})")
        return out

    def get_dependency_map(self, file_paths: List[str]) -> str:
        """
        Cross-File Dependency Map for LLM priming.

        Given a set of files, renders an LLM-readable adjacency view built from
        memory_edges whose endpoint facts carry file_path: which of the target
        files depend on each other, which external files depend on them, and any
        shared constraints/decisions that span more than one of the target files.
        """
        clean_paths = []
        for fp in file_paths:
            clean_paths.append(fp.strip().replace("\\", "/").lower())

        facts_by_file: Dict[str, List[Dict[str, Any]]] = {p: [] for p in clean_paths}
        # limit=None: the briefing must see EVERY active fact. Clamping to
        # MAX_LIMIT hid facts about the target file entirely once the corpus
        # passed 200 facts. The fact #498 standing rule (do not uncap the
        # STRUCTURAL lane without a retrieval re-tuning pass) does not apply
        # here: this read feeds no ranking, only the pre-edit briefing.
        all_active = self.get_state(limit=None)
        for f in all_active:
            if not f["file_path"]:
                continue
            fp = f["file_path"].strip().replace("\\", "/").lower()
            for p in clean_paths:
                if self._paths_match(p, fp):
                    facts_by_file[p].append(f)

        fact_to_file: Dict[int, str] = {}
        for p, fl in facts_by_file.items():
            for f in fl:
                fact_to_file[f["id"]] = p

        edges = self.conn.execute(
            "SELECT source_id, target_id, relation FROM memory_edges"
        ).fetchall()

        internal_edges: List[Dict[str, Any]] = []
        external_dependents: Dict[str, set] = {p: set() for p in clean_paths}
        external_dependencies: Dict[str, set] = {p: set() for p in clean_paths}
        for e in edges:
            sid, tid = e["source_id"], e["target_id"]
            s_row = self.conn.execute("SELECT file_path, deprecated_at FROM memory WHERE id = ?", (sid,)).fetchone()
            t_row = self.conn.execute("SELECT file_path, deprecated_at FROM memory WHERE id = ?", (tid,)).fetchone()
            if not s_row or not t_row or s_row["deprecated_at"] or t_row["deprecated_at"]:
                continue
            s_fp = (s_row["file_path"] or "").strip().replace("\\", "/").lower()
            t_fp = (t_row["file_path"] or "").strip().replace("\\", "/").lower()
            s_match = next((p for p in clean_paths if s_fp and self._paths_match(p, s_fp)), None)
            t_match = next((p for p in clean_paths if t_fp and self._paths_match(p, t_fp)), None)
            if s_match and t_match and s_match != t_match:
                internal_edges.append({"from": s_match, "to": t_match, "relation": e["relation"]})
            elif t_match and s_fp and not s_match:
                external_dependents[t_match].add(s_fp)
            elif s_match and t_fp and not t_match:
                external_dependencies[s_match].add(t_fp)

        shared_facts: List[Dict[str, Any]] = []
        seen_shared: set = set()
        for p_a in clean_paths:
            for p_b in clean_paths:
                if p_a >= p_b:
                    continue
                ids_a = {f["id"] for f in facts_by_file[p_a] if f["category"] in ("constraint", "decision")}
                ids_b = {f["id"] for f in facts_by_file[p_b] if f["category"] in ("constraint", "decision")}
                for fid in ids_a & ids_b:
                    if fid not in seen_shared:
                        seen_shared.add(fid)
                        fact = next(f for fl in facts_by_file.values() for f in fl if f["id"] == fid)
                        shared_facts.append({"fact": fact, "files": [p_a, p_b]})

        lines = [f"### Cross-File Dependency Map ({len(clean_paths)} files)", ""]
        lines.append("Target files: " + ", ".join(f"`{p}`" for p in clean_paths))
        lines.append("")
        if internal_edges:
            lines.append("#### Dependencies Between Target Files:")
            for ie in internal_edges:
                lines.append(f"- `{ie['from']}` **[{ie['relation']}]** → `{ie['to']}`")
            lines.append("")
        else:

            lines.append("#### Dependencies Between Target Files:")
            lines.append("*No recorded edges connect these files to each other.*")
            lines.append("")

        has_external = False
        for p in clean_paths:
            deps_on = external_dependents[p]
            deps_of = external_dependencies[p]
            if deps_on or deps_of:
                if not has_external:
                    lines.append("#### External File Relationships:")
                    has_external = True
                if deps_on:
                    lines.append(f"- `{p}` is depended on by: {', '.join(f'`{d}`' for d in sorted(deps_on))}")
                if deps_of:
                    lines.append(f"- `{p}` depends on: {', '.join(f'`{d}`' for d in sorted(deps_of))}")
        if has_external:
            lines.append("")

        if shared_facts:
            lines.append("#### Shared Constraints / Decisions Across Target Files:")
            for sf in shared_facts:
                lines.append(f"- **[{sf['fact']['category'].upper()} #{sf['fact']['id']}]** {sf['fact']['content']}")
                lines.append(f"  - applies to: {', '.join(f'`{p}`' for p in sf['files'])}")

            lines.append("")

        risk_files = [p for p in clean_paths if len(external_dependents[p]) >= 2]
        if risk_files:
            lines.append("#### ⚠️ High-Risk Files (2+ external dependents — changes here ripple outward):")
            for p in risk_files:
                lines.append(f"- `{p}`")
            lines.append("")

        return "\n".join(lines)

    def suggest_proactive_context(self, current_fact_ids: List[int], limit: int = 5) -> str:
        """
        Suggest facts likely needed next, based on co-retrieval patterns in the
        usage log: if facts A and B were often retrieved together and A is in the
        current context, suggest B.
        """
        if not current_fact_ids:
            return "*Provide the IDs of facts currently in context to get proactive suggestions.*"
        current = set(int(fid) for fid in current_fact_ids)
        co_counts: Dict[int, int] = {}
        rows = self.conn.execute(
            "SELECT retrieved_fact_ids FROM memory_usage_log ORDER BY logged_at DESC LIMIT 300"
        ).fetchall()
        for r in rows:
            ids_str = r["retrieved_fact_ids"] or ""
            ids: List[int] = []
            for tok in ids_str.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    ids.append(int(tok))
                except ValueError:
                    continue
            if not (set(ids) & current):
                continue
            for fid in ids:
                if fid in current:
                    continue
                co_counts[fid] = co_counts.get(fid, 0) + 1
        if not co_counts:
            return "*No co-retrieval patterns found yet — usage history is too sparse for proactive suggestions.*"
        candidates = sorted(co_counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        lines: List[str] = []
        for fid, cnt in candidates:
            row = self.conn.execute(
                "SELECT content, category, importance FROM memory WHERE id = ? AND deprecated_at IS NULL",
                (fid,),
            ).fetchone()
            if not row:
                continue
            lines.append(f"- **[{row['category']} #{fid}]** {row['content']} *(co-retrieved {cnt}x)*")
        if not lines:
            return "*No co-retrieval patterns found yet — usage history is too sparse for proactive suggestions.*"
        return "## Proactive Context Suggestions\n\n" + "\n".join(lines)

