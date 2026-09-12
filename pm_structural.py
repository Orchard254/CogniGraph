"""Structural layer: tree-sitter node sync and fact-to-node linking."""
import json
import sqlite3
import logging
import re
import datetime
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)



class StructuralMixin:
    # Provenance artifacts and non-codebase directories that must never enter
    # the structural index, no matter which caller syncs them: indexing them
    # pollutes project_shape and creates phantom callers in the code graph.
    _EXCLUDE_SYNC_FILENAMES = frozenset({
        "memory_store.PRISTINE.bak.py", "memory_store_reconstructed.py",
        "memory_store_corrupted_backup.py", "_split_store.py", "_rebuild_store.py",
        "_refactor_split.py", "_fix_priming.py", "phase0_spike.py",
    })
    _EXCLUDE_SYNC_PREFIXES = ("scratch/", "fixtures/")

    def sync_structural_nodes(self, file_path: str, nodes: list[dict], project_root: str | None = None) -> dict:

        """
        Upsert structural nodes for a single file, then prune stale rows for that file.
        
        Args:
            file_path: The file path (relative to project_root, or absolute)
            nodes: List of node dicts from extract_structures(), each with:
                type: 'function' | 'method' | 'class' | 'import'
                name: str
                class_name: str (optional, for methods)
                line_start: int (1-indexed)
                line_end: int (1-indexed)
            project_root: Optional root directory to resolve relative file_path against.
        
        Returns:
            dict with keys: upserted, pruned, nodes (list of node_ids)
        """
        # Normalize file path
        clean_path = file_path.strip().replace("\\", "/")
        
        # Single scan timestamp for this sync operation
        scan_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Enforced here rather than per caller so every present and future
        # sync path (bootstrap, watcher, ad-hoc scripts) is protected.
        basename = clean_path.split("/")[-1]
        if basename in self._EXCLUDE_SYNC_FILENAMES or clean_path.startswith(self._EXCLUDE_SYNC_PREFIXES):
            return {"upserted": 0, "pruned": 0, "nodes": [], "scan_timestamp": scan_ts,
                    "edges_calls": 0, "edges_imports": 0}
        
        upserted = 0
        node_ids = []
        
        # Read file once for code_hash computation
        # Resolve file_path relative to project_root if provided
        if project_root:
            full_path = Path(project_root) / clean_path
        else:
            full_path = Path(clean_path)
        if not full_path.exists() or not full_path.is_file():
            # File doesn't exist or is not a file — skip this sync (defensive)
            return {"upserted": 0, "pruned": 0, "nodes": [], "scan_timestamp": scan_ts,
                    "edges_calls": 0, "edges_imports": 0}
        try:
            file_lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except (OSError, FileNotFoundError):
            # Replaced or deleted between the exists check and the read.
            return {"upserted": 0, "pruned": 0, "nodes": [], "scan_timestamp": scan_ts,
                    "edges_calls": 0, "edges_imports": 0}
        
        # Derive language from file suffix
        suffix = full_path.suffix.lower()
        lang_map = {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
        }
        language = lang_map.get(suffix, "unknown")

        # base_classes column exists on migrated databases; hand-rolled test
        # fixtures may predate it, in which case bases are skipped silently.
        try:
            _sn_cols = [c["name"] for c in
                        self.conn.execute("PRAGMA table_info(structural_nodes)").fetchall()]
            has_bases_col = "base_classes" in _sn_cols
        except Exception:
            has_bases_col = False

        # Upsert + prune + edge rebuild must be atomic: two concurrent syncs
        # for the same file (watcher thread + bootstrap scan) use different
        # scan_ts values, and a prune from one must not delete the other's
        # just-upserted rows. BEGIN IMMEDIATE serializes same-file syncs.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for node in nodes:
                node_type = node.get("type")
                name = node.get("name", "")
                class_name = node.get("class_name", "")  # default '' per schema
                line_start = node.get("line_start")
                line_end = node.get("line_end")

                if not node_type or not name or line_start is None or line_end is None:
                    continue

                # Compute code_hash the same way verify_provenance does
                l_start = max(0, line_start - 1)
                l_end = min(len(file_lines), line_end)
                snippet = "".join(file_lines[l_start:l_end])
                code_hash = hashlib.sha256(snippet.encode("utf-8")).hexdigest()

                # Ordered base-class tails as JSON (class rows only); None for
                # anything else and when the column predates the migration.
                base_classes = None
                if has_bases_col and node_type == "class":
                    try:
                        base_classes = json.dumps(list(node.get("bases") or []))
                    except (TypeError, ValueError):
                        base_classes = "[]"

                # UPSERT by natural key
                if has_bases_col:
                    self.conn.execute(
                        """
                        INSERT INTO structural_nodes (node_type, name, class_name, file_path, line_start, line_end,
                                                      language, code_hash, base_classes, created_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(file_path, node_type, name, class_name) DO UPDATE SET
                            line_start = excluded.line_start,
                            line_end = excluded.line_end,
                            code_hash = excluded.code_hash,
                            base_classes = excluded.base_classes,
                            last_seen_at = excluded.last_seen_at,
                            language = excluded.language
                        """,
                        (node_type, name, class_name, clean_path, line_start, line_end,
                         language, code_hash, base_classes, scan_ts, scan_ts),
                    )
                else:
                    self.conn.execute(
                        """
                        INSERT INTO structural_nodes (node_type, name, class_name, file_path, line_start, line_end,
                                                      language, code_hash, created_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(file_path, node_type, name, class_name) DO UPDATE SET
                            line_start = excluded.line_start,
                            line_end = excluded.line_end,
                            code_hash = excluded.code_hash,
                            last_seen_at = excluded.last_seen_at,
                            language = excluded.language
                        """,
                        (node_type, name, class_name, clean_path, line_start, line_end,
                         language, code_hash, scan_ts, scan_ts),
                    )

                # Resolve node_id via SELECT — cur.lastrowid is unreliable for ON CONFLICT DO UPDATE
                # (returns last inserted rowid, not the updated row's id)
                row = self.conn.execute(
                    "SELECT id FROM structural_nodes WHERE file_path = ? AND node_type = ? AND name = ? AND class_name = ?",
                    (clean_path, node_type, name, class_name)
                ).fetchone()
                node_id = row["id"] if row else None

                if node_id:
                    node_ids.append(node_id)
                    upserted += 1

            # Stale pruning: delete rows for this file not seen in this scan.
            # module_ref rows are created by OTHER files' import resolution, so
            # they must survive this file's own prune pass.
            cur = self.conn.execute(
                "DELETE FROM structural_nodes WHERE file_path = ? AND last_seen_at != ? AND node_type != 'module_ref'",
                (clean_path, scan_ts)
            )
            pruned = cur.rowcount

            edges_calls, edges_imports = self._sync_structural_edges(
                clean_path, nodes, project_root, scan_ts)
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

        return {
            "upserted": upserted,
            "pruned": pruned,
            "nodes": node_ids,
            "scan_timestamp": scan_ts,
            "edges_calls": edges_calls,
            "edges_imports": edges_imports,
        }

    # ------------------------------------------------------------------
    # Code graph: structural_edges (calls / imports) built from the
    # tree-sitter extraction, resolved against the structural index.
    # ------------------------------------------------------------------

    _FROM_IMPORT_RE = re.compile(r"\s*from\s+([\w.]+)\s+import\s+(.+)")
    _PLAIN_IMPORT_RE = re.compile(r"\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?")

    def _resolve_module_file(self, module_name: str, project_root: Optional[str]) -> Optional[str]:
        """Resolve a dotted Python module name to a repo-relative file path, or None."""
        if not project_root or not module_name:
            return None
        rel = module_name.replace(".", "/")
        root = Path(project_root)
        for candidate in (rel + ".py", rel + "/__init__.py"):
            if (root / candidate).is_file():
                return candidate.replace("\\", "/")
        return None

    def _upsert_module_ref(self, target_file: str, scan_ts: str) -> Optional[int]:
        stem = target_file.split("/")[-1].rsplit(".", 1)[0]
        self.conn.execute(
            "INSERT INTO structural_nodes (node_type, name, class_name, file_path, line_start, line_end, "
            "language, code_hash, created_at, last_seen_at) "
            "VALUES ('module_ref', ?, '', ?, 1, 1, NULL, NULL, ?, ?) "
            "ON CONFLICT(file_path, node_type, name, class_name) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (stem, target_file, scan_ts, scan_ts),
        )
        row = self.conn.execute(
            "SELECT id FROM structural_nodes WHERE file_path = ? AND node_type = 'module_ref' AND name = ?",
            (target_file, stem),
        ).fetchone()
        return row["id"] if row else None

    def _parse_import_statement(self, stmt: str):
        """Return (module_name, [symbols]) for one import statement text."""
        m = self._FROM_IMPORT_RE.match(stmt or "")
        if m:
            symbols = [s.strip() for s in m.group(2).split(",")]
            symbols = [s for s in symbols if s and s != "as"]
            return m.group(1), symbols
        m = self._PLAIN_IMPORT_RE.match(stmt or "")
        if m:
            return m.group(1), []
        return None, []

    def _sync_structural_edges(self, clean_path: str, nodes: list[dict],
                               project_root: Optional[str], scan_ts: str) -> Tuple[int, int]:
        """Rebuild all structural_edges sourced from this file's nodes."""
        self.conn.execute(
            "DELETE FROM structural_edges WHERE caller_id IN "
            "(SELECT id FROM structural_nodes WHERE file_path = ?)",
            (clean_path,),
        )

        file_nodes = self.conn.execute(
            "SELECT id, node_type, name, class_name FROM structural_nodes WHERE file_path = ?",
            (clean_path,),
        ).fetchall()
        defs: Dict[Tuple[str, str, str], int] = {
            (r["node_type"], r["name"], r["class_name"]): r["id"] for r in file_nodes
        }

        # Import table for this file: imported symbol -> resolved repo file,
        # and module alias -> dotted module name (for attribute calls).
        imports_by_symbol: Dict[str, str] = {}
        alias_to_module: Dict[str, str] = {}
        import_node_rows: List[Tuple[int, str]] = []
        for r in file_nodes:
            if r["node_type"] != "import":
                continue
            import_node_rows.append((r["id"], r["name"]))
            module, symbols = self._parse_import_statement(r["name"])
            if not module:
                continue
            alias_to_module[module.split(".")[-1]] = module
            target_file = self._resolve_module_file(module, project_root)
            if target_file:
                alias_to_module[module] = module
                for sym in symbols:
                    imports_by_symbol[sym] = target_file

        def find_in_file(fp: str, name: str, types=("method", "function")):
            ph = ",".join("?" * len(types))
            return self.conn.execute(
                f"SELECT id FROM structural_nodes WHERE file_path = ? AND name = ? AND node_type IN ({ph})",
                (fp, name, *types),
            ).fetchall()

        # Composed-class map: class name -> ordered base-class tails, for
        # mixin-aware self.X resolution (e.g. MemoryStore -> its 4 mixins).
        # File-local rows are authoritative; cross-file entries fill in bases
        # declared elsewhere. Missing column (pre-migration DB) -> empty map.
        class_bases: Dict[str, List[str]] = {}
        try:
            base_rows = self.conn.execute(
                "SELECT name, file_path, base_classes FROM structural_nodes "
                "WHERE node_type = 'class'"
            ).fetchall()
        except sqlite3.OperationalError:
            base_rows = []
        for br in base_rows:
            try:
                parsed = json.loads(br["base_classes"]) if br["base_classes"] else []
            except (TypeError, ValueError):
                parsed = []
            if not isinstance(parsed, list):
                continue
            parsed = [b for b in parsed if isinstance(b, str) and b]
            if br["file_path"] == clean_path or br["name"] not in class_bases:
                class_bases[br["name"]] = parsed

        def resolve_in_composition(cls_name: str, method: str) -> Optional[int]:
            """Method X scoped to cls_name's composition family, in order.

            Two directions (both bounded, no type inference):
            (b2a) bases of the caller's own class — covers callers defined
                  ON a composed class (and JS/TS extends chains);
            (b2b) sibling mixins — the callers here live IN the mixins, not
                  in MemoryStore, so find composed classes C listing the
                  caller's class among their bases (MemoryStore lists all
                  four mixins) and search C itself, then C's bases in
                  declaration order.

            First scope entry listed wins; when X is defined in more than
            one scope class the ambiguity is logged (stderr via logging) so
            the choice is discoverable rather than silent.
            """
            if not cls_name:
                return None
            scope: List[str] = []
            scope.extend(class_bases.get(cls_name) or [])
            for comp in sorted(c for c, bs in class_bases.items() if cls_name in bs):
                scope.append(comp)
                scope.extend(class_bases.get(comp) or [])
            seen = set()
            ordered = [c for c in scope if c != cls_name and not (c in seen or seen.add(c))]
            if not ordered:
                return None
            hits: List[Tuple[str, int]] = []
            for cand in ordered:
                for r in self.conn.execute(
                    "SELECT id FROM structural_nodes WHERE name = ? AND class_name = ? "
                    "AND node_type IN ('method','function')",
                    (method, cand),
                ).fetchall():
                    hits.append((cand, r["id"]))
            if not hits:
                return None
            hit_classes = sorted({c for c, _ in hits})
            if len(hit_classes) > 1:
                logger.warning(
                    "ambiguous self.%s for class %s: defined in %s; "
                    "first scope entry (%s) wins",
                    method, cls_name, hit_classes, hits[0][0],
                )
            return hits[0][1]

        def resolve_callee(callee_text: str, caller_node: dict) -> Optional[int]:
            if "." in callee_text:
                prefix, suffix = callee_text.split(".", 1)
                if prefix == "self":
                    cls = caller_node.get("class_name") or ""
                    if cls:
                        nid = defs.get(("method", suffix, cls))
                        if nid:
                            return nid
                    # Composed-class family BEFORE the class-blind same-file
                    # fallback: find_in_file(clean_path, suffix) has no
                    # class_name filter, so whenever >=2 same-named methods
                    # share the caller's file it shadows composition scoping
                    # with an arbitrary (plan-dependent) winner. Own-class
                    # resolution above is untouched; this only reorders the
                    # two fallbacks. Covers self.X living on a base of the
                    # caller's class or on a sibling mixin under a common
                    # composed class (MemoryStore -> pm_* mixins).
                    nid = resolve_in_composition(cls, suffix)
                    if nid:
                        return nid
                    rows = find_in_file(clean_path, suffix)
                    if rows:
                        return rows[0]["id"]
                    # Mixin composition: self.X may live in another file/class.
                    rows = self.conn.execute(
                        "SELECT id FROM structural_nodes WHERE name = ? AND node_type IN ('method','function')",
                        (suffix,),
                    ).fetchall()
                    return rows[0]["id"] if len(rows) == 1 else None
                module = alias_to_module.get(prefix)
                if module:
                    target = self._resolve_module_file(module, project_root)
                    if target:
                        rows = find_in_file(target, suffix, types=("function", "method", "class"))
                        if rows:
                            return rows[0]["id"]
                # Instance-alias rewrite (extraction): VAR.method inside a
                # `with FACTORY() as VAR` body arrives as Class.method
                # (e.g. server.py `with _store() as _s` -> MemoryStore.*).
                # Own class first, then its composition family (b2a/b2b).
                # A known class with no such method resolves to nothing —
                # never leak into the repo-wide-unique fallback below.
                if prefix in class_bases:
                    own = self.conn.execute(
                        "SELECT id FROM structural_nodes WHERE name = ? AND class_name = ? "
                        "AND node_type IN ('method','function')",
                        (suffix, prefix),
                    ).fetchall()
                    if own:
                        if len(own) > 1:
                            logger.warning(
                                "ambiguous %s.%s: %d definitions; first row wins",
                                prefix, suffix, len(own),
                            )
                        return own[0]["id"]
                    return resolve_in_composition(prefix, suffix)
                return None
            key = ("function", callee_text, "")
            if key in defs:
                return defs[key]
            rows = find_in_file(clean_path, callee_text)
            if rows:
                return rows[0]["id"]
            if callee_text in imports_by_symbol:
                rows = find_in_file(imports_by_symbol[callee_text], callee_text,
                                    types=("function", "method", "class"))
                if rows:
                    return rows[0]["id"]
            # Last resort: a name unique across the whole repo (functions and
            # methods) — catches chained callees like _get_store().checkpoint_task
            # where the attribute collapses to the bare method name.
            rows = self.conn.execute(
                "SELECT id FROM structural_nodes WHERE name = ? AND node_type IN ('function','method')",
                (callee_text,),
            ).fetchall()
            return rows[0]["id"] if len(rows) == 1 else None

        calls_made = 0
        for node in nodes:
            calls = node.get("calls")
            if not calls:
                continue
            key = (node.get("type"), node.get("name", ""), node.get("class_name", "") or "")
            caller_id = defs.get(key)
            if not caller_id:
                continue
            for callee_text in calls:
                target_id = resolve_callee(callee_text, node)
                if target_id and target_id != caller_id:
                    cur = self.conn.execute(
                        "INSERT OR IGNORE INTO structural_edges (caller_id, callee_id, edge_type, created_at) "
                        "VALUES (?, ?, 'calls', ?)",
                        (caller_id, target_id, scan_ts),
                    )
                    calls_made += cur.rowcount

        imports_made = 0
        for node_id, stmt in import_node_rows:
            module, _symbols = self._parse_import_statement(stmt)
            if not module:
                continue
            target_file = self._resolve_module_file(module, project_root)
            if not target_file:
                continue
            ref_id = self._upsert_module_ref(target_file, scan_ts)
            if ref_id:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO structural_edges (caller_id, callee_id, edge_type, created_at) "
                    "VALUES (?, ?, 'imports', ?)",
                    (node_id, ref_id, scan_ts),
                )
                imports_made += cur.rowcount

        return calls_made, imports_made

    def who_calls(self, symbol: str) -> Dict[str, Any]:
        """ONE-HOP CODE GRAPH QUERY: who calls this symbol, and who imports its file.

        Accepts a bare name ('query_hybrid') or Class.method ('MemoryStore.write').
        Returns definitions, callers (name/class/file/lines), and importing files.
        """
        symbol = (symbol or "").strip()
        if not symbol:
            return {"symbol": symbol, "found": False, "definitions": [], "callers": [], "importers": []}
        if "." in symbol:
            cls, name = symbol.split(".", 1)
            targets = self.conn.execute(
                "SELECT id, node_type, name, class_name, file_path, line_start, line_end "
                "FROM structural_nodes WHERE name = ? AND class_name = ?",
                (name, cls),
            ).fetchall()
        else:
            targets = self.conn.execute(
                "SELECT id, node_type, name, class_name, file_path, line_start, line_end "
                "FROM structural_nodes WHERE name = ?",
                (symbol,),
            ).fetchall()
        targets = [t for t in targets if t["node_type"] != "module_ref"]
        if not targets:
            return {"symbol": symbol, "found": False, "definitions": [], "callers": [], "importers": []}

        target_ids = [t["id"] for t in targets]
        ph = ",".join("?" * len(target_ids))
        callers = self.conn.execute(
            f"SELECT DISTINCT n.node_type, n.name, n.class_name, n.file_path, n.line_start, n.line_end "
            f"FROM structural_edges e JOIN structural_nodes n ON e.caller_id = n.id "
            f"WHERE e.callee_id IN ({ph}) AND e.edge_type = 'calls' "
            f"ORDER BY n.file_path, n.line_start",
            target_ids,
        ).fetchall()

        files = sorted({t["file_path"] for t in targets})
        importers: List[str] = []
        if files:
            fph = ",".join("?" * len(files))
            rows = self.conn.execute(
                f"SELECT DISTINCT n2.file_path FROM structural_edges e "
                f"JOIN structural_nodes ref ON e.callee_id = ref.id "
                f"JOIN structural_nodes n2 ON e.caller_id = n2.id "
                f"WHERE e.edge_type = 'imports' AND ref.file_path IN ({fph}) "
                f"ORDER BY n2.file_path",
                files,
            ).fetchall()
            importers = [r["file_path"] for r in rows]

        return {
            "symbol": symbol,
            "found": True,
            "definitions": [dict(t) for t in targets],
            "callers": [dict(r) for r in callers],
            "importers": importers,
        }

    def project_shape(self) -> str:
        """Compact orientation briefing: indexed file inventory and code-graph size."""
        try:
            totals = self.conn.execute(
                "SELECT COUNT(DISTINCT file_path) AS f, "
                "SUM(CASE WHEN node_type IN ('function','method') THEN 1 ELSE 0 END) AS defs, "
                "SUM(CASE WHEN node_type = 'class' THEN 1 ELSE 0 END) AS classes "
                "FROM structural_nodes WHERE node_type != 'module_ref'"
            ).fetchone()
            files = self.conn.execute(
                "SELECT file_path, COUNT(*) AS defs FROM structural_nodes "
                "WHERE node_type IN ('function','method','class') "
                "GROUP BY file_path ORDER BY defs DESC, file_path"
            ).fetchall()
            call_edges = self.conn.execute(
                "SELECT COUNT(*) AS c FROM structural_edges WHERE edge_type = 'calls'").fetchone()["c"]
            import_edges = self.conn.execute(
                "SELECT COUNT(*) AS c FROM structural_edges WHERE edge_type = 'imports'").fetchone()["c"]
        except sqlite3.OperationalError:
            return "### Project Shape\n*Structural index not available yet.*"

        lines = [
            "### Project Shape",
            "",
            f"- **{totals['f'] or 0} indexed files** — {totals['defs'] or 0} functions/methods, {totals['classes'] or 0} classes",
            f"- Code graph: **{call_edges} call edges**, **{import_edges} import edges**",
            "",
            "#### Files (by definition count):",
        ]
        if files:
            for r in files:
                lines.append(f"- `{r['file_path']}` ({r['defs']} defs)")
        else:
            lines.append("*No structural index yet — run bootstrap or sync_structural_nodes.*")
        lines.append("")
        lines.append("*For one-hop questions ('who calls X?', 'who imports Y?') use memory_who_calls instead of re-reading files.*")
        return "\n".join(lines)

    def link_facts_to_structural_nodes(self, file_path: Optional[str] = None) -> dict:
        """
        Link facts to structural nodes via line-range overlap within the same file.
        
        Args:
            file_path: If given, scope to just that file's facts and nodes.
                       If None, run across all files (full relink).
        
        For each active fact with non-null file_path, line_start, line_end matching
        the scope, find all structural_nodes in the same file_path where ranges overlap:
            NOT (fact.line_end < node.line_start OR fact.line_start > node.line_end)
        
        link_reason:
            - 'exact_match' when node.line_start <= fact.line_start AND fact.line_end <= node.line_end
              (fact fully contained within node)
            - 'overlap' otherwise (partial overlap)
        
        A single fact CAN link to multiple nodes (e.g. a fact about a method may also
        overlap its enclosing class) — this is intentional.
        
        Returns:
            dict with keys: linked (new links created), skipped (already existed), scope_file
        """
        # Normalize file path if provided
        scope_file = None
        if file_path:
            scope_file = file_path.strip().replace("\\", "/")
        
        # Build WHERE clause for scoping
        where_clauses = ["m.deprecated_at IS NULL", "m.file_path IS NOT NULL", 
                         "m.line_start IS NOT NULL", "m.line_end IS NOT NULL"]
        params = []
        if scope_file:
            where_clauses.append("m.file_path = ?")
            params.append(scope_file)
        
        fact_where = " AND ".join(where_clauses)
        
        # Get all active facts with file_path and line ranges in scope
        facts = self.conn.execute(
            f"""
            SELECT id, file_path, line_start, line_end
            FROM memory m
            WHERE {fact_where}
            """,
            params
        ).fetchall()
        
        if not facts:
            return {"linked": 0, "skipped": 0, "scope_file": scope_file}
        
        linked = 0
        skipped = 0
        created_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        for fact in facts:
            fact_id = fact["id"]
            f_path = fact["file_path"]
            f_start = fact["line_start"]
            f_end = fact["line_end"]
            
            # Find overlapping structural nodes in the same file
            # Overlap condition: NOT (fact_end < node_start OR fact_start > node_end)
            # Which is: fact_end >= node_start AND fact_start <= node_end
            nodes = self.conn.execute(
                """
                SELECT id, line_start, line_end
                FROM structural_nodes
                WHERE file_path = ? 
                  AND line_end >= ? 
                  AND line_start <= ?
                """,
                (f_path, f_start, f_end)
            ).fetchall()
            
            for node in nodes:
                node_id = node["id"]
                n_start = node["line_start"]
                n_end = node["line_end"]
                
                # Determine link_reason
                if n_start <= f_start and f_end <= n_end:
                    link_reason = "exact_match"
                else:
                    link_reason = "overlap"
                
                # INSERT OR IGNORE uses the UNIQUE(fact_id, node_id) constraint
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO fact_node_links (fact_id, node_id, link_reason, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (fact_id, node_id, link_reason, created_ts)
                )
                
                if cur.rowcount > 0:
                    linked += 1
                else:
                    skipped += 1
        
        return {"linked": linked, "skipped": skipped, "scope_file": scope_file}

