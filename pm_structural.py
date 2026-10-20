"""Structural layer: tree-sitter node sync and fact-to-node linking."""
import json
import sqlite3
import logging
import re
import datetime
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from corpus_hygiene import STUB_PREFIXES
from pm_common import _epistemic_status, _is_test_file

logger = logging.getLogger(__name__)



class StructuralMixin:
    _epistemic_status = staticmethod(_epistemic_status)
    _is_test_file = staticmethod(_is_test_file)
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

        If the file no longer exists on disk, all indexed rows for it are
        deleted instead (see delete_structural_file) — a sync must never
        be a no-op for a deleted file, or orphans accumulate permanently.
        
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
            # File deleted (or never existed): prune any indexed rows so a
            # deletion doesn't leave permanent orphans. Child tables
            # (structural_edges, call_sites, fact_node_links) cascade via
            # ON DELETE CASCADE. Reported as pruned to keep the return
            # contract (rows removed for this file).
            result = self.delete_structural_file(clean_path)
            return {"upserted": 0, "pruned": result["deleted"], "nodes": [],
                    "scan_timestamp": scan_ts,
                    "edges_calls": 0, "edges_imports": 0}
        try:
            file_lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except (OSError, FileNotFoundError):
            # Replaced or deleted between the exists check and the read.
            # Only a confirmed-gone file gets pruned; a transient read
            # error must not wipe the index.
            if not full_path.exists():
                result = self.delete_structural_file(clean_path)
                return {"upserted": 0, "pruned": result["deleted"], "nodes": [],
                        "scan_timestamp": scan_ts,
                        "edges_calls": 0, "edges_imports": 0}
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

    def delete_structural_file(self, file_path: str) -> dict:
        """Delete ALL structural_nodes rows for one file path.

        The file-deletion complement to sync_structural_nodes: the per-file
        prune inside sync only runs when the file still exists, so without
        this, deleted files leave permanent orphan rows (plus dangling
        edges/links). Child tables (structural_edges, call_sites,
        fact_node_links) clean up automatically via ON DELETE CASCADE —
        no explicit multi-table deletion. module_ref rows keyed to this
        path are deleted too: their import target no longer exists, and the
        next sync of any surviving importer will not recreate them
        (_resolve_module_file returns None for a missing target).

        Runs in its own BEGIN IMMEDIATE transaction, matching the sync
        atomicity convention. Never raises for a path with no rows.

        Returns:
            dict with keys: deleted (rows removed), file_path (normalized)
        """
        clean_path = file_path.strip().replace("\\", "/")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "DELETE FROM structural_nodes WHERE file_path = ?",
                (clean_path,),
            )
            deleted = cur.rowcount
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return {"deleted": deleted, "file_path": clean_path}

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

        # call_sites exists on migrated databases; hand-rolled test fixtures
        # may predate it, in which case sites are skipped silently (same
        # convention as the base_classes column check in sync_structural_nodes).
        try:
            _cs_cols = [c["name"] for c in
                        self.conn.execute("PRAGMA table_info(call_sites)").fetchall()]
            has_sites_table = bool(_cs_cols)
        except Exception:
            has_sites_table = False
        if has_sites_table:
            self.conn.execute(
                "DELETE FROM call_sites WHERE caller_id IN "
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
                # Imported-class family fallback: VAR.method where VAR is an
                # unknown local (e.g. `store = MemoryStore(...)` then
                # `store.query_hybrid(...)`). The prefix is neither self, a
                # module alias, nor a known class, so scope the suffix to the
                # composition families of classes imported into the caller's
                # file. Exactly one hit class resolves; zero or ambiguous
                # stays unresolved (same conservative contract as the
                # repo-wide-unique fallback below).
                if imports_by_symbol:
                    try:
                        syms = list(imports_by_symbol.keys())
                        sph = ",".join("?" * len(syms))
                        imported_classes = [
                            r["name"] for r in self.conn.execute(
                                "SELECT DISTINCT name FROM structural_nodes "
                                f"WHERE node_type = 'class' AND name IN ({sph})",
                                syms,
                            ).fetchall()
                        ]
                    except sqlite3.OperationalError:
                        imported_classes = []
                    fam_hits: List[Tuple[str, int]] = []
                    for cand_cls in imported_classes:
                        for r in self.conn.execute(
                            "SELECT id FROM structural_nodes WHERE name = ? AND class_name = ? "
                            "AND node_type IN ('method','function')",
                            (suffix, cand_cls),
                        ).fetchall():
                            fam_hits.append((cand_cls, r["id"]))
                        comp_id = resolve_in_composition(cand_cls, suffix)
                        if comp_id is not None:
                            fam_hits.append((cand_cls, comp_id))
                    hit_classes = sorted({c for c, _ in fam_hits})
                    if len(hit_classes) == 1:
                        if len(fam_hits) > 1:
                            logger.warning(
                                "ambiguous %s.%s in imported family %s: %d hits; first wins",
                                prefix, suffix, hit_classes[0], len(fam_hits),
                            )
                        return fam_hits[0][1]
                    if len(hit_classes) > 1:
                        logger.warning(
                            "ambiguous %s.%s across imported families %s; unresolved",
                            prefix, suffix, hit_classes,
                        )
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
                    if has_sites_table:
                        # Resolved pairs only: unresolved names have no
                        # callee_id and get no rows, by design. Old-shape
                        # node dicts (no 'call_sites' key) simply store none.
                        raw_sites = (node.get("call_sites") or {}).get(callee_text) or []
                        for site_line in raw_sites:
                            try:
                                site_line = int(site_line)
                            except (TypeError, ValueError):
                                continue
                            self.conn.execute(
                                "INSERT OR IGNORE INTO call_sites (caller_id, callee_id, line, created_at) "
                                "VALUES (?, ?, ?, ?)",
                                (caller_id, target_id, site_line, scan_ts),
                            )

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
        Returns definitions, callers (name/class/file/lines, plus call_lines:
        sorted 1-indexed lines where the caller invokes this symbol, [] when
        unknown), and importing files.
        """
        symbol = (symbol or "").strip()
        if not symbol:
            return {"symbol": symbol, "found": False, "definitions": [], "callers": [], "importers": []}
        if "." in symbol:
            cls, name = symbol.split(".", 1)
            targets = self.conn.execute(
                "SELECT id, node_type, name, class_name, file_path, line_start, line_end, base_classes "
                "FROM structural_nodes WHERE name = ? AND class_name = ?",
                (name, cls),
            ).fetchall()
        else:
            targets = self.conn.execute(
                "SELECT id, node_type, name, class_name, file_path, line_start, line_end, base_classes "
                "FROM structural_nodes WHERE name = ?",
                (symbol,),
            ).fetchall()
        targets = [t for t in targets if t["node_type"] != "module_ref"]
        if not targets:
            return {"symbol": symbol, "found": False, "definitions": [], "callers": [], "importers": []}

        target_ids = [t["id"] for t in targets]
        ph = ",".join("?" * len(target_ids))
        caller_rows = self.conn.execute(
            f"SELECT DISTINCT n.id AS caller_id, n.node_type, n.name, n.class_name, n.file_path, n.line_start, n.line_end "
            f"FROM structural_edges e JOIN structural_nodes n ON e.caller_id = n.id "
            f"WHERE e.callee_id IN ({ph}) AND e.edge_type = 'calls' "
            f"ORDER BY n.file_path, n.line_start",
            target_ids,
        ).fetchall()

        # Per-caller call-site lines into these targets; callers with no
        # stored sites (pre-migration rows) get [], never null.
        lines_by_caller: Dict[int, List[int]] = {}
        try:
            cph = ",".join("?" * len(target_ids))
            for r in self.conn.execute(
                f"SELECT caller_id, line FROM call_sites WHERE callee_id IN ({cph})",
                target_ids,
            ).fetchall():
                lines_by_caller.setdefault(r["caller_id"], []).append(r["line"])
        except sqlite3.OperationalError:
            pass
        callers = []
        for r in caller_rows:
            d = dict(r)
            cid = d.pop("caller_id")
            d["call_lines"] = sorted(set(lines_by_caller.get(cid, [])))
            callers.append(d)

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

        # Parse base_classes for class definitions
        defs_out: List[Dict[str, Any]] = []
        for t in targets:
            d = dict(t)
            raw = d.get("base_classes")
            if isinstance(raw, str):
                try:
                    d["base_classes"] = json.loads(raw)
                except Exception:
                    d["base_classes"] = []
            elif raw is None and d.get("node_type") == "class":
                d["base_classes"] = []
            defs_out.append(d)
        return {
            "symbol": symbol,
            "found": True,
            "definitions": defs_out,
            "callers": [dict(r) for r in callers],
            "importers": importers,
        }

    def who_inherits(self, symbol: str, max_depth: int = 5) -> Dict[str, Any]:
        """INHERITANCE QUERY: which classes inherit from SYMBOL (exact match).

        Finds all class nodes where base_classes JSON array contains exactly
        `symbol` (value equality via json_each), not substring — 'Parent'
        never matches 'ParentMixin' or 'GrandParent'. Transitive cone via
        recursive CTE (depth 1=direct, 2+=indirect). No schema migration.

        Returns found, definitions (base class nodes), inheritors (list of
        {name, file_path, line_start, line_end, base_classes, depth}),
        depth_counts, total. Single-hop callers could filter depth==1.
        """
        symbol = (symbol or "").strip()
        if not symbol:
            return {"symbol": symbol, "found": False, "definitions": [], "inheritors": []}
        # Definitions: class nodes named symbol
        try:
            targets = self.conn.execute(
                "SELECT id, node_type, name, class_name, file_path, line_start, line_end, base_classes "
                "FROM structural_nodes WHERE name = ? AND node_type = 'class'",
                (symbol,),
            ).fetchall()
        except sqlite3.OperationalError:
            return {"symbol": symbol, "found": False, "definitions": [], "inheritors": []}
        # Parse definitions base_classes
        defs_parsed: List[Dict[str, Any]] = []
        for t in targets:
            d = dict(t)
            raw = d.get("base_classes")
            if isinstance(raw, str):
                try:
                    d["base_classes"] = json.loads(raw)
                except Exception:
                    d["base_classes"] = []
            elif raw is None:
                d["base_classes"] = []
            defs_parsed.append(d)
        if not targets:
            return {"symbol": symbol, "found": False, "definitions": [], "inheritors": []}
        try:
            max_depth = int(max_depth)
        except (TypeError, ValueError):
            max_depth = 5
        max_depth = max(1, min(10, max_depth))
        # Transitive via recursive CTE with exact json_each match
        combined: Dict[int, Dict[str, Any]] = {}
        try:
            rows = self.conn.execute(
                "WITH RECURSIVE inheritors(id, name, file_path, line_start, line_end, base_classes, depth) AS ("
                " SELECT id, name, file_path, line_start, line_end, base_classes, 1 FROM structural_nodes "
                " WHERE node_type='class' AND EXISTS (SELECT 1 FROM json_each(base_classes) WHERE value=?)"
                " UNION ALL"
                " SELECT c.id, c.name, c.file_path, c.line_start, c.line_end, c.base_classes, i.depth+1 FROM structural_nodes c "
                " JOIN inheritors i ON EXISTS (SELECT 1 FROM json_each(c.base_classes) WHERE value=i.name)"
                " WHERE i.depth < ?"
                ") SELECT id, name, file_path, line_start, line_end, base_classes, MIN(depth) AS depth FROM inheritors GROUP BY id ORDER BY depth, name",
                (symbol, max_depth),
            ).fetchall()
            for r in rows:
                nid = r["id"]
                # Exclude self-definitions
                if any(t["id"] == nid for t in targets):
                    continue
                d = dict(r)
                raw = d.get("base_classes")
                if isinstance(raw, str):
                    try:
                        d["base_classes"] = json.loads(raw)
                    except Exception:
                        d["base_classes"] = []
                elif raw is None:
                    d["base_classes"] = []
                combined[nid] = d
        except sqlite3.OperationalError:
            # JSON1 unavailable — Python fallback exact
            frontier = {symbol}
            visited_names = {symbol}
            depth = 1
            while frontier and depth <= max_depth:
                cand = self.conn.execute(
                    "SELECT id, name, file_path, line_start, line_end, base_classes FROM structural_nodes WHERE node_type='class'",
                ).fetchall()
                next_frontier: set = set()
                for row in cand:
                    nid = row["id"]
                    if nid in combined or any(t["id"] == nid for t in targets):
                        continue
                    try:
                        bases = json.loads(row["base_classes"]) if row["base_classes"] else []
                    except Exception:
                        bases = []
                    if not isinstance(bases, list):
                        continue
                    if any(b in frontier for b in bases):
                        d = dict(row)
                        d["base_classes"] = bases
                        d["depth"] = depth
                        combined[nid] = d
                        if row["name"] not in visited_names:
                            next_frontier.add(row["name"])
                visited_names.update(next_frontier)
                frontier = next_frontier
                depth += 1
        inheritors = sorted(combined.values(), key=lambda x: (x["depth"], x["name"]))
        depth_counts: Dict[int, int] = {}
        for it in inheritors:
            depth_counts[it["depth"]] = depth_counts.get(it["depth"], 0) + 1
        return {
            "symbol": symbol,
            "found": True,
            "definitions": defs_parsed,
            "inheritors": inheritors,
            "depth_counts": depth_counts,
            "total": len(inheritors),
        }

    def blast_radius(self, symbol: str, class_name: str = "", file_path: str = "",
                       max_depth: int = 5, direction: str = "callers",
                       file_detail: str = "", max_symbols_per_file: int = 20,
                       project_root: Optional[str] = None) -> Dict[str, Any]:
        """TRANSITIVE BLAST RADIUS: who depends on this symbol, transitively.

        Default direction is callers-only: blast radius = "what breaks if I
        change this", i.e. who depends on it. Pass direction="callees" for
        the downstream cone ("what does this pull in") or "both" for the
        union of the two.

        Symbol resolution mirrors who_calls: a bare name ("write") matches
        every definition; "Class.method" scopes to that class. The optional
        class_name / file_path params narrow an ambiguous bare name without
        duplicating ambiguity-handling logic. When several definitions still
        match, the traversal covers their union and ambiguous=True is set.

        Traversal is a bounded-depth recursive CTE over structural_edges
        (edge_type='calls') with a visited-path guard, so mutual and
        self-recursion terminate. Seed definitions are excluded from the
        affected set: the radius counts who ELSE breaks.

        Summary-first output: counts and per-hop frontiers stay compact even
        for a large radius; per-file symbol lists are capped at
        max_symbols_per_file entries (truncated=True flags the cap). Pass
        file_detail=<file> for that file's full uncapped symbol/line detail.
        """
        known_limitations = [
            "recall is not complete for indirect/dynamic calls and decorators",
            "edges reflect the last watcher sync — counts and "
            "unresolved_estimate may be temporarily incomplete immediately "
            "after bulk edits until the watcher converges",
        ]

        symbol = (symbol or "").strip()
        try:
            max_depth = int(max_depth)
        except (TypeError, ValueError):
            max_depth = 5
        max_depth = max(1, min(10, max_depth))
        if direction not in ("callers", "callees", "both", "inherits"):
            direction = "callers"
        try:
            max_symbols_per_file = int(max_symbols_per_file)
        except (TypeError, ValueError):
            max_symbols_per_file = 20
        max_symbols_per_file = max(1, min(200, max_symbols_per_file))
        scope_cls = (class_name or "").strip()
        scope_file = (file_path or "").strip().replace("\\", "/")
        file_detail = (file_detail or "").strip().replace("\\", "/")

        if not symbol:
            return {"symbol": symbol, "found": False,
                    "known_limitations": known_limitations}

        if "." in symbol and not scope_cls:
            cls, name = symbol.split(".", 1)
            targets = self.conn.execute(
                "SELECT id, node_type, name, class_name, file_path, line_start, line_end, base_classes "
                "FROM structural_nodes WHERE name = ? AND class_name = ?",
                (name, cls),
            ).fetchall()
        else:
            name = symbol.rsplit(".", 1)[-1] if "." in symbol else symbol
            if scope_cls:
                targets = self.conn.execute(
                    "SELECT id, node_type, name, class_name, file_path, line_start, line_end, base_classes "
                    "FROM structural_nodes WHERE name = ? AND class_name = ?",
                    (name, scope_cls),
                ).fetchall()
            else:
                targets = self.conn.execute(
                    "SELECT id, node_type, name, class_name, file_path, line_start, line_end, base_classes "
                    "FROM structural_nodes WHERE name = ?",
                    (name,),
                ).fetchall()
        targets = [t for t in targets if t["node_type"] != "module_ref"]
        if scope_file:
            targets = [t for t in targets if t["file_path"] == scope_file]
        if not targets:
            return {"symbol": symbol, "found": False,
                    "known_limitations": known_limitations}

        seed_ids = [t["id"] for t in targets]
        seed_set = set(seed_ids)
        ph = ",".join("?" * len(seed_ids))

        def _walk_callers() -> Dict[int, int]:
            rows = self.conn.execute(
                "WITH RECURSIVE br(id, depth, path) AS ("
                " SELECT e.caller_id, 1, ',' || e.caller_id || ','"
                f" FROM structural_edges e WHERE e.callee_id IN ({ph})"
                " AND e.edge_type = 'calls'"
                " UNION ALL"
                " SELECT e.caller_id, br.depth + 1, br.path || e.caller_id || ','"
                " FROM structural_edges e JOIN br ON e.callee_id = br.id"
                " WHERE br.depth < ? AND e.edge_type = 'calls'"
                " AND instr(br.path, ',' || e.caller_id || ',') = 0"
                ") SELECT id, MIN(depth) AS depth FROM br GROUP BY id",
                (*seed_ids, max_depth),
            ).fetchall()
            return {r["id"]: r["depth"] for r in rows}

        def _walk_callees() -> Dict[int, int]:
            rows = self.conn.execute(
                "WITH RECURSIVE br(id, depth, path) AS ("
                " SELECT e.callee_id, 1, ',' || e.callee_id || ','"
                f" FROM structural_edges e WHERE e.caller_id IN ({ph})"
                " AND e.edge_type = 'calls'"
                " UNION ALL"
                " SELECT e.callee_id, br.depth + 1, br.path || e.callee_id || ','"
                " FROM structural_edges e JOIN br ON e.caller_id = br.id"
                " WHERE br.depth < ? AND e.edge_type = 'calls'"
                " AND instr(br.path, ',' || e.callee_id || ',') = 0"
                ") SELECT id, MIN(depth) AS depth FROM br GROUP BY id",
                (*seed_ids, max_depth),
            ).fetchall()
            return {r["id"]: r["depth"] for r in rows}

        def _walk_inherits() -> Dict[int, int]:
            """Transitive subclass cone via exact base_classes json_each match.

            No migration: base_classes is JSON array persisted by sync_structural_nodes.
            Exact match uses json_each(value=?) so 'Parent' never matches
            'ParentMixin' or 'GrandParent' (LIKE pitfall). Falls back to
            Python json.loads exact check if JSON1 unavailable.
            Depth is subclass distance (1=direct child).
            """
            seed_names = sorted({t["name"] for t in targets if t["node_type"] == "class"})
            if not seed_names:
                return {}
            combined: Dict[int, int] = {}
            for sname in seed_names:
                try:
                    rows = self.conn.execute(
                        "WITH RECURSIVE inheritors(id, name, depth) AS ("
                        " SELECT id, name, 1 FROM structural_nodes "
                        " WHERE node_type='class' AND EXISTS (SELECT 1 FROM json_each(base_classes) WHERE value=?)"
                        " UNION ALL"
                        " SELECT c.id, c.name, i.depth+1 FROM structural_nodes c "
                        " JOIN inheritors i ON EXISTS (SELECT 1 FROM json_each(c.base_classes) WHERE value=i.name)"
                        " WHERE i.depth < ?"
                        ") SELECT id, MIN(depth) AS depth FROM inheritors GROUP BY id",
                        (sname, max_depth),
                    ).fetchall()
                    for r in rows:
                        nid = r["id"]
                        d = r["depth"]
                        if nid in seed_ids:
                            continue
                        if nid not in combined or d < combined[nid]:
                            combined[nid] = d
                except sqlite3.OperationalError:
                    # JSON1 unavailable — Python fallback with exact set membership
                    frontier = {sname}
                    visited_names = {sname}
                    depth = 1
                    while frontier and depth <= max_depth:
                        # fetch all class candidates and filter in Python
                        cand = self.conn.execute(
                            "SELECT id, name, base_classes FROM structural_nodes WHERE node_type='class'",
                        ).fetchall()
                        next_frontier: set = set()
                        for row in cand:
                            nid = row["id"]
                            if nid in seed_ids or nid in combined:
                                continue
                            try:
                                bases = json.loads(row["base_classes"]) if row["base_classes"] else []
                            except Exception:
                                bases = []
                            if not isinstance(bases, list):
                                continue
                            if any(b in frontier for b in bases):
                                if nid not in combined or depth < combined[nid]:
                                    combined[nid] = depth
                                if row["name"] not in visited_names:
                                    next_frontier.add(row["name"])
                        visited_names.update(next_frontier)
                        frontier = next_frontier
                        depth += 1
                    break  # single fallback pass covers all seeds already
            return combined

        if direction == "callers":
            depths = _walk_callers()
        elif direction == "callees":
            depths = _walk_callees()
        elif direction == "inherits":
            depths = _walk_inherits()
        else:
            depths = _walk_callers()
            for nid, d in _walk_callees().items():
                if nid not in depths or d < depths[nid]:
                    depths[nid] = d
        for sid in seed_set:
            depths.pop(sid, None)

        affected_ids = sorted(depths)
        node_rows: List[Any] = []
        if affected_ids:
            aph = ",".join("?" * len(affected_ids))
            node_rows = self.conn.execute(
                "SELECT id, node_type, name, class_name, file_path, line_start, line_end, base_classes "
                f"FROM structural_nodes WHERE id IN ({aph})",
                affected_ids,
            ).fetchall()
        node_rows = [r for r in node_rows if r["node_type"] != "module_ref"]
        nodes = {r["id"]: dict(r) for r in node_rows}
        for nid in list(depths):
            if nid not in nodes:
                depths.pop(nid, None)

        total_edges = 0
        if depths and direction != "inherits":
            vph = ",".join("?" * len(depths))
            sph = ",".join("?" * len(seed_set))
            total_edges = self.conn.execute(
                "SELECT COUNT(*) AS c FROM structural_edges "
                f"WHERE edge_type = 'calls' AND caller_id IN ({vph}) "
                f"AND callee_id IN ({vph}, {sph})",
                (*depths, *depths, *seed_set),
            ).fetchone()["c"]

        depth_counts: Dict[int, int] = {}
        for d in depths.values():
            depth_counts[d] = depth_counts.get(d, 0) + 1
        max_reached = max(depths.values()) if depths else 0

        by_file: Dict[str, List[Dict[str, Any]]] = {}
        # Call-site lines per affected node: lines where it calls any node
        # also in the affected+seed set (option ii — the traversed-edge
        # direction is not tracked, so this is the within-radius call set,
        # not specifically the hop toward the seed). Missing table or no
        # stored sites -> [].
        lines_by_node: Dict[int, List[int]] = {}
        if nodes:
            try:
                scope_ids = list(set(depths) | seed_set)
                nph = ",".join("?" * len(nodes))
                sph = ",".join("?" * len(scope_ids))
                for r in self.conn.execute(
                    "SELECT caller_id, line FROM call_sites "
                    f"WHERE caller_id IN ({nph}) AND callee_id IN ({sph})",
                    (*nodes, *scope_ids),
                ).fetchall():
                    lines_by_node.setdefault(r["caller_id"], []).append(r["line"])
            except sqlite3.OperationalError:
                pass
        for nid, node in nodes.items():
            entry = {"name": node["name"], "class_name": node["class_name"],
                     "node_type": node["node_type"],
                     "line_start": node["line_start"], "line_end": node["line_end"],
                     "call_lines": sorted(set(lines_by_node.get(nid, []))) if direction != "inherits" else [],
                     "depth": depths[nid]}
            # Surface base_classes for class nodes so callers can see hierarchy without extra lookup
            if node.get("base_classes") is not None:
                try:
                    entry["base_classes"] = json.loads(node["base_classes"]) if isinstance(node["base_classes"], str) else list(node["base_classes"] or [])
                except Exception:
                    entry["base_classes"] = []
            elif node["node_type"] == "class":
                entry["base_classes"] = []
            by_file.setdefault(node["file_path"], []).append(entry)
        for entries in by_file.values():
            entries.sort(key=lambda e: (e["depth"], e["line_start"]))

        files: List[Dict[str, Any]] = []
        if file_detail:
            entries = by_file.get(file_detail, [])
            files = [{"file_path": file_detail, "count": len(entries),
                      "symbols": entries, "truncated": False}]
        else:
            for fp in sorted(by_file):
                entries = by_file[fp]
                capped = entries[:max_symbols_per_file]
                files.append({"file_path": fp, "count": len(entries),
                              "symbols": capped,
                              "truncated": len(entries) > len(capped)})

        if direction == "inherits":
            unresolved, skipped = 0, []
        else:
            unresolved, skipped = self._blast_unresolved(
                [dict(t) for t in targets], nodes, project_root)

        # Parse base_classes JSON for definitions for ergonomic output
        defs_out: List[Dict[str, Any]] = []
        for t in targets:
            d = dict(t)
            raw = d.get("base_classes")
            if isinstance(raw, str):
                try:
                    d["base_classes"] = json.loads(raw)
                except Exception:
                    d["base_classes"] = []
            elif raw is None and d.get("node_type") == "class":
                d["base_classes"] = []
            defs_out.append(d)
        result: Dict[str, Any] = {
            "symbol": symbol,
            "found": True,
            "ambiguous": len(targets) > 1,
            "definitions": defs_out,
            "direction": direction,
            "max_depth_requested": max_depth,
            "max_depth_reached": max_reached,
            "total_files": len(by_file),
            "total_callers": len(depths),
            "total_edges": total_edges,
            "depth_counts": depth_counts,
            "files": files,
            "unresolved_estimate": unresolved,
            "unresolved_note": (
                "Sum over traversed function/method nodes of "
                "(re-extracted calls[] length minus resolved 'calls' edges); "
                "an honesty signal for known recall gaps, not a completeness proof."
            ),
            "unresolved_skipped_files": skipped,
            "known_limitations": known_limitations,
        }
        if file_detail:
            result["file_detail"] = file_detail
            result["file_detail_found"] = file_detail in by_file
            if file_detail not in by_file:
                result["note"] = (
                    f"{file_detail} has no affected symbols in this blast radius.")
        return result

    def _blast_unresolved(self, seeds: List[Dict[str, Any]],
                          nodes: Dict[int, Dict[str, Any]],
                          project_root: Optional[str]) -> Tuple[int, List[str]]:
        """Honesty signal: raw calls[] vs resolved edges, summed over traversal.

        structural_nodes persists definitions, not the raw calls[] lists the
        extractor saw, so the gap is recomputed by re-extracting the touched
        files from disk (same extract_structures() the sync path uses) and
        diffing each traversed function/method's calls[] length against its
        resolved outgoing 'calls' edge count. Files that cannot be read are
        skipped and reported, never silently counted as zero.
        """
        if project_root is None:
            try:
                root = Path(getattr(self, "db_path", "") or "").resolve().parent.parent
                project_root = str(root) if root.is_dir() else None
            except Exception:
                project_root = None
        if not project_root:
            return 0, ["project root unavailable — estimate skipped"]

        by_file: Dict[str, List[Dict[str, Any]]] = {}
        for row in list(seeds) + list(nodes.values()):
            if row.get("node_type") not in ("function", "method"):
                continue
            by_file.setdefault(row["file_path"], []).append(row)

        all_ids = [r["id"] for rows in by_file.values() for r in rows if r.get("id")]
        resolved: Dict[int, int] = {}
        if all_ids:
            cph = ",".join("?" * len(all_ids))
            for r in self.conn.execute(
                "SELECT caller_id, COUNT(*) AS c FROM structural_edges "
                f"WHERE edge_type = 'calls' AND caller_id IN ({cph}) GROUP BY caller_id",
                all_ids,
            ).fetchall():
                resolved[r["caller_id"]] = r["c"]

        try:
            from structural_extract import extract_structures
        except Exception:
            return 0, ["structural extractor unavailable — estimate skipped"]

        estimate = 0
        skipped: List[str] = []
        for fp, rows in by_file.items():
            disk = Path(project_root) / fp
            try:
                fresh = extract_structures(disk)
            except Exception:
                skipped.append(fp)
                continue
            # Pairs-semantics (locked): both sides count unique callees, not
            # call sites — len(calls) is the deduped name list and the
            # resolved side counts one row per (caller, callee) edge. The
            # call_sites line lists are display data and must NOT feed this
            # gap, or multi-site pairs would inflate it. .get() defaults keep
            # old-shape node dicts (no 'call_sites' key) working.
            raw = {(n.get("type"), n.get("name", ""), n.get("class_name", "") or ""):
                   len(n.get("calls") or []) for n in fresh}
            for row in rows:
                key = (row.get("node_type"), row.get("name", ""),
                       row.get("class_name", "") or "")
                if key not in raw:
                    continue
                gap = raw[key] - resolved.get(row.get("id", -1), 0)
                if gap > 0:
                    estimate += gap
        return estimate, skipped

    # ------------------------------------------------------------------
    # Scoped-line code reader: symbol-or-range batch reads of live disk text.
    # ------------------------------------------------------------------

    _READ_KNOWN_LIMITATIONS = [
        "text is read live from disk but line numbers come from the last "
        "watcher sync — a stale:true flag means the indexed range may have "
        "shifted since it was recorded",
        "symbol ranges are definition spans from the structural index; "
        "decorators or comments above the def line are not included",
    ]

    def _read_project_root(self, project_root: Optional[str]) -> Optional[str]:
        """Derive the containment root the same way _blast_unresolved does."""
        if project_root:
            return project_root
        try:
            root = Path(getattr(self, "db_path", "") or "").resolve().parent.parent
            return str(root) if root.is_dir() else None
        except Exception:
            return None

    @staticmethod
    def _read_contained_path(file_path: str, project_root: str) -> Optional[Path]:
        """Resolve file_path under project_root; None when it escapes.

        Containment is checked AFTER resolve(), so ".." segments,
        absolute-path injection, and symlinks pointing outside the root are
        all rejected before any read happens.
        """
        try:
            clean = (file_path or "").strip().replace("\\", "/")
            if not clean:
                return None
            root = Path(project_root).resolve()
            candidate = (root / clean).resolve()
            candidate.relative_to(root)
            return candidate
        except Exception:
            return None

    @staticmethod
    def _read_last_seen_ts(value: Any) -> Optional[float]:
        try:
            dt = datetime.datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    def read_code(self, items: Optional[List[Dict[str, Any]]] = None,
                  max_lines_per_item: int = 200,
                  max_total_chars: int = 24000,
                  context: int = 0,
                  project_root: Optional[str] = None) -> Dict[str, Any]:
        """BATCHED SCOPED-LINE READER: definition bodies or explicit ranges.

        Each item addresses text exactly one way: {"symbol": ...} with
        optional class_name/file_path scoping (same disambiguation as
        who_calls/blast_radius — an ambiguous bare name returns one entry
        per matched definition with ambiguous=True), or
        {"file_path":..., "line_start":..., "line_end":...} for an arbitrary
        1-indexed inclusive range. The context margin (default 0, clamp
        0..10) expands explicit ranges only and is ignored for symbols.

        Request order is preserved in results. Over-long symbols are cut at
        max_lines_per_item (default 200, clamp 1..500) with truncated=True
        and the full span in actual_lines. Items that would exceed the
        running max_total_chars budget (default 24000, clamp 1k..64k)
        return a per-item total_budget_exceeded error — never partial or
        shortened text — so the caller re-requests them in a smaller batch.
        Every failure (bad spec, unknown symbol, path escape, missing file)
        is an in-band per-item error; one bad item never fails the batch.
        """
        try:
            max_lines = int(max_lines_per_item)
        except (TypeError, ValueError):
            max_lines = 200
        max_lines = max(1, min(500, max_lines))
        try:
            budget = int(max_total_chars)
        except (TypeError, ValueError):
            budget = 24000
        budget = max(1000, min(64000, budget))
        try:
            default_ctx = int(context)
        except (TypeError, ValueError):
            default_ctx = 0
        default_ctx = max(0, min(10, default_ctx))

        root = self._read_project_root(project_root)
        seq = items if isinstance(items, list) else []
        results: List[Dict[str, Any]] = []
        total_chars = 0

        def _stale_by_mtime(disk: Path, clean: str) -> Tuple[bool, Optional[str]]:
            try:
                mtime = disk.stat().st_mtime
            except OSError:
                return False, "staleness_unknown"
            try:
                row = self.conn.execute(
                    "SELECT last_seen_at FROM structural_nodes "
                    "WHERE file_path = ? ORDER BY last_seen_at DESC LIMIT 1",
                    (clean,),
                ).fetchone()
            except sqlite3.OperationalError:
                return False, "staleness_unknown"
            if row is None:
                return False, "file_not_indexed"
            ts = self._read_last_seen_ts(row["last_seen_at"])
            if ts is None:
                return False, "staleness_unknown"
            if mtime > ts:
                return True, "file_modified_since_sync"
            return False, None

        def _emit(kind: str, clean: str, want_lo: int, want_hi: int,
                  actual: List[int], requested: List[int],
                  symbol: Optional[str] = None,
                  ambiguous: bool = False,
                  stored_hash: Optional[str] = None) -> None:
            nonlocal total_chars
            base: Dict[str, Any] = {"kind": kind, "file_path": clean}
            if symbol is not None:
                base["symbol"] = symbol
                base["ambiguous"] = ambiguous
            if root is None:
                results.append({**base, "ok": False,
                                "error": "project_root_unavailable",
                                "message": "project root unavailable — cannot "
                                           "resolve file paths safely"})
                return
            disk = self._read_contained_path(clean, root)
            if disk is None:
                results.append({**base, "ok": False, "error": "path_escape",
                                "message": f"{clean} escapes the project root "
                                           "and was not read"})
                return
            if not disk.is_file():
                results.append({**base, "ok": False, "error": "file_not_found",
                                "message": f"{clean} does not exist on disk"})
                return
            try:
                disk_lines = disk.read_text(encoding="utf-8",
                                            errors="replace").splitlines(keepends=True)
            except OSError:
                results.append({**base, "ok": False, "error": "unreadable",
                                "message": f"{clean} could not be read"})
                return
            n = len(disk_lines)
            lo = max(1, min(want_lo, want_hi))
            hi = min(n, max(want_lo, want_hi))
            # Provenance hash. verify_provenance recomputes this as
            # sha256("".join(file_lines[line_start-1:line_end])) with
            # splitlines(keepends=True), so trailing newlines are part of the
            # digest and an agent recomputing it by hand is likely to get the
            # line-ending convention wrong. It is computed over the FULL
            # `actual` span, NOT the possibly-truncated [lo, hi] text returned
            # below, because `actual` is what a caller records as
            # line_start/line_end and therefore what will be re-hashed later.
            prov_lo = max(1, min(n, actual[0]) if n else 1)
            prov_hi = min(n, max(1, actual[1]))
            prov_snippet = "".join(disk_lines[prov_lo - 1:prov_hi]) if n else ""
            prov_hash = hashlib.sha256(prov_snippet.encode("utf-8")).hexdigest()
            if stored_hash:
                stale = prov_hash != stored_hash
                reason = None if not stale else "snippet_hash_mismatch"
            else:
                stale, reason = _stale_by_mtime(disk, clean)
            span = max(0, hi - lo + 1) if n else 0
            truncated = span > max_lines
            if truncated:
                hi = lo + max_lines - 1
            text = "".join(disk_lines[lo - 1:hi]) if n and hi >= lo else ""
            if len(text) + total_chars > budget and text:
                results.append({**base, "ok": False,
                                "error": "total_budget_exceeded",
                                "requested_lines": requested,
                                "message": "batch character budget exceeded — "
                                           "re-request this item in a smaller batch"})
                return
            total_chars += len(text)
            results.append({**base, "ok": True, "requested_lines": requested,
                            "returned_lines": [lo, hi] if text or n else [],
                            "actual_lines": actual, "truncated": truncated,
                            "stale": stale, "stale_reason": reason,
                            "provenance_lines": [prov_lo, prov_hi],
                            "code_hash": prov_hash,
                            "text": text})

        for raw in seq:
            if not isinstance(raw, dict):
                results.append({"ok": False, "kind": "unknown",
                                "error": "invalid_spec",
                                "message": "each item must be an object with "
                                           "either symbol or file_path+line range"})
                continue
            sym_raw = raw.get("symbol")
            symbol_given = isinstance(sym_raw, str) and sym_raw.strip() != ""
            if ("symbol" in raw and sym_raw is not None
                    and not isinstance(sym_raw, str)):
                results.append({"ok": False, "kind": "unknown",
                                "error": "invalid_spec",
                                "message": "symbol must be a non-empty string"})
                continue
            has_line_keys = "line_start" in raw or "line_end" in raw
            if symbol_given and has_line_keys:
                results.append({"ok": False, "kind": "symbol",
                                "symbol": sym_raw.strip(),
                                "error": "invalid_spec",
                                "message": "specify either symbol OR "
                                           "file_path+line_start+line_end, not both"})
                continue
            if symbol_given:
                symbol = sym_raw.strip()
                scope_cls = str(raw.get("class_name") or "").strip()
                scope_file = str(raw.get("file_path") or "").strip().replace("\\", "/")
                if "." in symbol and not scope_cls:
                    cls, name = symbol.split(".", 1)
                    targets = self.conn.execute(
                        "SELECT id, node_type, name, class_name, file_path, "
                        "line_start, line_end, code_hash, last_seen_at "
                        "FROM structural_nodes WHERE name = ? AND class_name = ?",
                        (name, cls),
                    ).fetchall()
                else:
                    name = symbol.rsplit(".", 1)[-1] if "." in symbol else symbol
                    if scope_cls:
                        targets = self.conn.execute(
                            "SELECT id, node_type, name, class_name, file_path, "
                            "line_start, line_end, code_hash, last_seen_at "
                            "FROM structural_nodes WHERE name = ? AND class_name = ?",
                            (name, scope_cls),
                        ).fetchall()
                    else:
                        targets = self.conn.execute(
                            "SELECT id, node_type, name, class_name, file_path, "
                            "line_start, line_end, code_hash, last_seen_at "
                            "FROM structural_nodes WHERE name = ?",
                            (name,),
                        ).fetchall()
                targets = [t for t in targets if t["node_type"] != "module_ref"]
                if scope_file:
                    targets = [t for t in targets if t["file_path"] == scope_file]
                if not targets:
                    results.append({"ok": False, "kind": "symbol", "symbol": symbol,
                                    "error": "symbol_not_found",
                                    "message": f"no indexed definition for {symbol!r}"})
                    continue
                ambiguous = len(targets) > 1
                for t in targets:
                    ls, le = t["line_start"], t["line_end"]
                    _emit("symbol", t["file_path"], ls, le, [ls, le], [ls, le],
                          symbol=symbol, ambiguous=ambiguous,
                          stored_hash=t["code_hash"])
                continue
            fp_raw = raw.get("file_path")
            if "line_start" not in raw or "line_end" not in raw:
                results.append({"ok": False, "kind": "unknown",
                                "error": "invalid_spec",
                                "message": "specify either symbol OR "
                                           "file_path+line_start+line_end"})
                continue
            if not isinstance(fp_raw, str) or not fp_raw.strip():
                results.append({"ok": False, "kind": "range",
                                "error": "invalid_spec",
                                "message": "explicit range needs file_path as a "
                                           "non-empty string"})
                continue
            try:
                rs, re_ = int(raw["line_start"]), int(raw["line_end"])
            except (TypeError, ValueError):
                results.append({"ok": False, "kind": "range",
                                "file_path": fp_raw.strip().replace("\\", "/"),
                                "error": "invalid_range",
                                "message": "line_start/line_end must be integers"})
                continue
            try:
                ctx = int(raw.get("context", default_ctx))
            except (TypeError, ValueError):
                ctx = default_ctx
            ctx = max(0, min(10, ctx))
            clean = fp_raw.strip().replace("\\", "/")
            elo, ehi = min(rs, re_) - ctx, max(rs, re_) + ctx
            _emit("range", clean, elo, ehi, [max(1, elo), ehi], [rs, re_])

        return {"results": results, "total_chars": total_chars,
                "max_total_chars": budget, "max_lines_per_item": max_lines,
                "known_limitations": list(self._READ_KNOWN_LIMITATIONS)}

    def search_symbols(self, query: str, limit: int = 20) -> dict:
        """Fuzzy/ambiguous symbol discovery over structural_nodes (LIKE, ranked).

        Returns file:line:name:type hits. Limit capped at 100. Ranking (lower
        score first): exact name, prefix, substring.
        """
        q = (query or "").strip()
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 20
        cap = max(1, min(cap, 100))
        if not q:
            return {"query": query, "count": 0, "hits": [],
                    "known_limitations": ["empty query"]}
        pattern = "%{}%".format(q)
        rows = self.conn.execute(
            "SELECT node_type, name, class_name, file_path, line_start, line_end "
            "FROM structural_nodes WHERE name LIKE ?",
            (pattern,),
        ).fetchall()
        scored = []
        ql = q.lower()
        for r in rows:
            name = r["name"] or ""
            nl = name.lower()
            if nl == ql:
                score = 0
            elif nl.startswith(ql):
                score = 1
            elif ql in nl:
                score = 2
            else:
                score = 3
            scored.append((score, name, r))
        scored.sort(key=lambda x: (x[0], x[1]))
        hits = []
        for score, _name, r in scored[:cap]:
            hits.append({
                "file_path": r["file_path"],
                "line_start": r["line_start"],
                "line_end": r["line_end"],
                "name": r["name"],
                "type": r["node_type"],
                "class_name": r["class_name"],
                "score": score,
            })
        return {"query": query, "count": len(hits), "hits": hits,
                "total_candidates": len(scored)}

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

        test_file_set = {r["file_path"] for r in files if _is_test_file(r["file_path"])}
        lines = [
            "### Project Shape",
            "",
            f"- **{totals['f'] or 0} indexed files** — {totals['defs'] or 0} functions/methods, {totals['classes'] or 0} classes",
            f"- Test files indexed: **{len(test_file_set)}**",
            f"- Code graph: **{call_edges} call edges**, **{import_edges} import edges**",
            "",
            "#### Files (by definition count):",
        ]
        if files:
            for r in files:
                tag = " [test]" if _is_test_file(r["file_path"]) else ""
                lines.append(f"- `{r['file_path']}` ({r['defs']} defs){tag}")
        else:
            lines.append("*No structural index yet — run bootstrap or sync_structural_nodes.*")
        # Class hierarchy per file (exact bases)
        try:
            class_rows = self.conn.execute(
                "SELECT file_path, name, base_classes FROM structural_nodes WHERE node_type='class' ORDER BY file_path, name"
            ).fetchall()
            by_file_bases: dict = {}
            for cr in class_rows:
                raw = cr["base_classes"]
                try:
                    bases = json.loads(raw) if raw else []
                except Exception:
                    bases = []
                if bases:
                    by_file_bases.setdefault(cr["file_path"], []).append(f"{cr['name']}({', '.join(bases)})")
            if by_file_bases:
                lines.append("")
                lines.append("#### Class hierarchy (bases):")
                for fp in sorted(by_file_bases):
                    entries = ", ".join(by_file_bases[fp][:8])
                    suffix = "…" if len(by_file_bases[fp]) > 8 else ""
                    lines.append(f"- `{fp}`: {entries}{suffix}")
        except Exception:
            pass
        lines.append("")
        lines.append("*For one-hop questions ('who calls X?', 'who imports Y?') use memory_who_calls instead of re-reading files.*")
        lines.append("*For inheritance: who_inherits('Parent') or blast_radius('Parent', direction='inherits').*")
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

        Whole-file template stubs (corpus_hygiene.STUB_PREFIXES) are excluded
        from link creation: they carry no node-level signal.
        
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
        # Stub exclusion: whole-file template stubs ("Source file:", "Key file:",
        # "Directory '") carry no node-level signal but their line ranges overlap
        # every node in their file. Skip them at link-creation time so the rows
        # are never written (query-time filtering in _structural_scores remains
        # as defense-in-depth for legacy rows). Prefixes are the single source
        # of truth (tags are unreliable: stubs with tags=None and junk-tagged
        # non-stubs both exist; link_reason can't filter since legitimate
        # facts also link with reason 'overlap').
        where_clauses.append(
            " AND ".join("m.content NOT LIKE ?" for _ in STUB_PREFIXES)
        )
        params.extend(f"{p}%" for p in STUB_PREFIXES)
        
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

