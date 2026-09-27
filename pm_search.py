"""Search layer: semantic scoring lanes, the 7-lane hybrid RRF query engine, intent detection, and usage-pattern learning."""
import sqlite3
import os
import re
import datetime
from typing import Optional, List, Dict, Any, Tuple

from corpus_hygiene import STUB_PREFIXES
from pm_common import (
    logger, tokenize_text, MAX_LIMIT,
    _encode, _blob_to_vec, _cosine_sim, _vec_to_blob,
)

# Injectable "now" for time-deterministic evals: _recency_weight normally
# ages facts against the wall clock, so golden ranks drift as facts age.
# Eval/fixture tooling pins this env var to a fixed ISO instant so recall
# numbers reproduce exactly regardless of when CI runs.
PINNED_NOW_ENV = "PROJECT_MEMORY_NOW"


def _parse_iso_instant(value) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 date/datetime into an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def pinned_now() -> Optional[datetime.datetime]:
    """The pinned eval clock from PROJECT_MEMORY_NOW, or None for wall time."""
    return _parse_iso_instant(os.environ.get(PINNED_NOW_ENV))


class SearchMixin:
    def _semantic_scores(self, query_vec, session_id: Optional[str] = None) -> Dict[int, float]:
        """
        Compute cosine similarity between query_vec and all stored embeddings
        for ACTIVE (non-deprecated) facts only.
        Returns dict of {fact_id: cosine_similarity}.
        """
        if query_vec is None:
            return {}
        sql = """
        SELECT me.fact_id, me.embedding
        FROM memory_embeddings me
        JOIN memory m ON me.fact_id = m.id
        WHERE m.deprecated_at IS NULL
        """
        params: List[Any] = []
        if session_id:
            sql += " AND m.session_id = ?"
            params.append(session_id)
        scores: Dict[int, float] = {}
        for r in self.conn.execute(sql, params).fetchall():
            try:
                vec = _blob_to_vec(r["embedding"])
            except Exception:
                continue
            scores[r["fact_id"]] = _cosine_sim(query_vec, vec)
        return scores

    def _semantic_scores_vec(self, query_vec, session_id: Optional[str] = None) -> Dict[int, float]:
        """
        Use sqlite-vec vec0 virtual table (vec_memory) to compute nearest neighbors.
        Returns dict of {fact_id: cosine_similarity} using conversion from L2 distance
        (the vec0 default) to cosine similarity for normalized vectors:
            cosine_sim = 1 - (distance ** 2) / 2
        This function gracefully falls back to the brute-force _semantic_scores
        for the same query if the vec query fails for any reason.

        It requests a small top-K (K=64) matching downstream usage (query_hybrid
        takes the top 50), avoiding asking the extension to return everything.
        When the environment variable PROJECT_MEMORY_VEC_DEBUG=1, emit debugging
        information about the raw SQL rows returned to help diagnose behavior.
        """
        debug = os.environ.get("PROJECT_MEMORY_VEC_DEBUG") == "1"
        if query_vec is None or not getattr(self, '_vec_available', False):
            if debug:
                print("[_semantic_scores_vec] unavailable: no query_vec or vec not available")
            return {}
        try:
            blob = _vec_to_blob(query_vec)
        except Exception as e:
            if debug:
                print("[_semantic_scores_vec] _vec_to_blob error:", e)
            return self._semantic_scores(query_vec, session_id=session_id)
        sql = """
            SELECT v.rowid, v.distance
            FROM vec_memory v
            JOIN memory m ON v.rowid = m.id
            WHERE v.embedding MATCH ? AND m.deprecated_at IS NULL
            """
        params: List[Any] = [blob]
        if session_id:
            sql += " AND m.session_id = ?"
            params.append(session_id)
        sql += " ORDER BY v.distance LIMIT ?"
        params.append(64)
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except Exception as e:
            if debug:
                print("[_semantic_scores_vec] vec query error, falling back to brute:", e)
            return self._semantic_scores(query_vec, session_id=session_id)
        if debug:
            try:
                print("[_semantic_scores_vec] fetched ", len(rows), " rows (showing up to 10):")
                for r in rows[:10]:
                    print("  raw_row:", tuple(r))
            except Exception as e:
                print("[_semantic_scores_vec] debug print failed:", e)
        scores: Dict[int, float] = {}
        for r in rows:
            try:
                fid = r["rowid"]
                dist = float(r["distance"])
            except Exception as e:
                if debug:
                    print("[_semantic_scores_vec] failed to parse row:", r, "error:", e)
                continue
            scores[fid] = max(0.0, 1.0 - (dist ** 2) / 2.0)
        if debug:
            print("[_semantic_scores_vec] returning ", len(scores), " scores")
        return scores

    def _semantic_scores_episodic(self, query_vec, repo_id=None) -> Dict[int, float]:
        """Brute-force cosine for episodic, filtered to root ACTIVE."""
        if query_vec is None:
            return {}
        if repo_id is None:
            repo_id = self._derive_default_repo_id()
        branch_rows = self.conn.execute(
            "SELECT branch_id FROM memory_branches WHERE is_root=1 AND repo_id=?",
            (repo_id,),
        ).fetchall()
        branch_ids = [r["branch_id"] for r in branch_rows]
        if not branch_ids:
            return {}
        ph = ",".join(["?"] * len(branch_ids))
        rows = self.conn.execute(
            f"""
            SELECT ee.memory_id, ee.embedding
            FROM episodic_embeddings ee
            JOIN episodic_memories em ON ee.memory_id = em.memory_id
            WHERE em.branch_id IN ({ph}) AND em.status='ACTIVE' AND em.closed_revision IS NULL
            """,
            branch_ids,
        ).fetchall()
        scores: Dict[int, float] = {}
        for r in rows:
            try:
                vec = _blob_to_vec(r["embedding"])
            except Exception:
                continue
            scores[r["memory_id"]] = _cosine_sim(query_vec, vec)
        return scores

    def _semantic_scores_vec_episodic(self, query_vec, repo_id=None) -> Dict[int, float]:
        """vec0 accelerated episodic semantic, filtered to root ACTIVE."""
        debug = os.environ.get("PROJECT_MEMORY_VEC_DEBUG") == "1"
        if query_vec is None or not getattr(self, '_vec_available', False):
            return {}
        try:
            blob = _vec_to_blob(query_vec)
        except Exception as e:
            if debug:
                print("[_semantic_scores_vec_episodic] _vec_to_blob error:", e)
            return self._semantic_scores_episodic(query_vec, repo_id=repo_id)
        if repo_id is None:
            repo_id = self._derive_default_repo_id()
        branch_rows = self.conn.execute(
            "SELECT branch_id FROM memory_branches WHERE is_root=1 AND repo_id=?",
            (repo_id,),
        ).fetchall()
        branch_ids = [r["branch_id"] for r in branch_rows]
        if not branch_ids:
            return {}
        ph = ",".join(["?"] * len(branch_ids))
        try:
            rows = self.conn.execute(
                f"""
            SELECT v.rowid, v.distance
            FROM vec_episodic_memory v
            JOIN episodic_memories em ON v.rowid = em.memory_id
            WHERE v.embedding MATCH ? AND em.branch_id IN ({ph}) AND em.status='ACTIVE' AND em.closed_revision IS NULL
            ORDER BY v.distance LIMIT ?
            """,
                [blob] + branch_ids + [64],
            ).fetchall()
        except Exception as e:
            if debug:
                print("[_semantic_scores_vec_episodic] vec query error, falling back:", e)
            return self._semantic_scores_episodic(query_vec, repo_id=repo_id)
        scores: Dict[int, float] = {}
        for r in rows:
            try:
                fid = r["rowid"]
                dist = float(r["distance"])
            except Exception:
                continue
            scores[fid] = max(0.0, 1.0 - (dist ** 2) / 2.0)
        return scores

    def _recency_weight(self, created_at, access_count,
                        now: Optional[datetime.datetime] = None) -> float:
        """
        Compute a recency multiplier in [0.7, 1.3].
        Newer facts and frequently-accessed facts rank higher.
        - A fact written today gets ~1.3x; one from 180 days ago gets ~0.7x.
        - Each access adds a small boost capped at 0.15.

        `now` (or the PROJECT_MEMORY_NOW env var) pins the reference instant
        so evals are time-deterministic; default is the wall clock.
        """
        ref = now or pinned_now() or datetime.datetime.now(datetime.timezone.utc)
        age_days = 180
        if created_at:
            created = _parse_iso_instant(created_at)
            if created is not None:
                age_days = max(0, (ref - created).days)
        recency = 1.3 - (min(age_days, 180) / 180.0) * 0.6
        access_boost = min((access_count or 0) * 0.01, 0.15)
        return max(0.7, min(1.3, recency + access_boost))

    def _query_encode(self, text: str):
        """Embed a query for the semantic lane.

        Default is pm_common._encode (fastembed). Eval harnesses swap this
        for a lookup over precomputed vectors frozen into the fixture, so
        retrieval never loads — or downloads — the embedding model.
        """
        return _encode(text)

    def _log_usage(self, query_text, intent, agent_type, retrieved_fact_ids, result_count, session_id=None):
        """Record one retrieval event for usage-pattern learning."""
        try:
            ids_str = ",".join(str(int(fid)) for fid in (retrieved_fact_ids or []))
            now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.conn.execute(
                "INSERT INTO memory_usage_log (query_text, intent, agent_type, retrieved_fact_ids, result_count, session_id, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (query_text, intent, agent_type, ids_str, result_count, session_id, now_str),
            )
        except sqlite3.ProgrammingError as e:
            # A cross-thread connection here is a real bug (per-thread stores
            # exist precisely to prevent it) — never demote to debug.
            logger.warning("usage logging failed (cross-thread?): %r", e)
        except Exception as e:
            logger.debug("usage logging failed: %r", e)

    def _usage_boost(self, text: str, lookback: int = 200) -> Dict[int, float]:
        """
        Compute per-fact boost multipliers from past retrievals whose queries
        overlap the current query. A fact retrieved 3 times for similar queries
        gets a larger boost than one retrieved once. Caps at 1.15x.

        Cold start: while the usage log has fewer than 10 entries, boosts are
        also seeded from the access_count column (capped lower, at 1.10x) so
        frequently-opened facts rank better before retrieval history accrues.
        """
        try:
            rows = self.conn.execute(
                "SELECT query_text, retrieved_fact_ids FROM memory_usage_log ORDER BY logged_at DESC LIMIT ?",
                (lookback,),
            ).fetchall()
        except Exception:
            rows = []
        # "the/so/after" overlap is not topical overlap: without stopword
        # filtering every query matches every past query and this boost
        # degrades into pure popularity. Require 2+ content tokens instead.
        stopwords = {
            "the", "a", "an", "so", "it", "its", "is", "are", "was", "were",
            "be", "been", "do", "does", "did", "how", "what", "why", "when",
            "where", "who", "which", "that", "this", "these", "those", "and",
            "or", "but", "of", "on", "in", "to", "for", "with", "at", "by",
            "from", "into", "about", "after", "before", "over", "under", "as",
            "if", "then", "than", "we", "you", "they", "he", "she", "our",
            "your", "my", "their", "not", "no", "can", "will", "just", "up",
            "out", "all", "any", "some", "has", "have", "had",
        }
        cur_tokens = tokenize_text(text) - stopwords
        counts: Dict[int, int] = {}
        if cur_tokens:
            for row in rows:
                past_tokens = tokenize_text(row["query_text"] or "") - stopwords
                if not past_tokens or len(cur_tokens & past_tokens) < 2:
                    continue
                ids_str = row["retrieved_fact_ids"] or ""
                for tok in ids_str.split(","):
                    tok = tok.strip()
                    if not tok:
                        continue
                    try:
                        fid = int(tok)
                    except ValueError:
                        continue
                    counts[fid] = counts.get(fid, 0) + 1
        boosts = {fid: min(1.0 + 0.05 * cnt, 1.15) for fid, cnt in counts.items() if cnt > 0}
        if len(rows) < 10:
            try:
                for r in self.conn.execute(
                    "SELECT id, access_count FROM memory WHERE access_count >= 2 AND deprecated_at IS NULL"
                ).fetchall():
                    seed = min(1.0 + 0.02 * (r["access_count"] or 0), 1.10)
                    boosts[r["id"]] = max(boosts.get(r["id"], 1.0), seed)
            except Exception:
                pass
        return boosts

    def usage_stats(self, top_facts: int = 20) -> Dict[str, Any]:
        """
        Summarize usage patterns: total retrievals, intent distribution,
        most-retrieved facts (from the last 500 retrievals), and recent queries.
        """
        total = self.conn.execute("SELECT COUNT(*) AS cnt FROM memory_usage_log").fetchone()["cnt"]
        intent_rows = self.conn.execute(
            "SELECT intent, COUNT(*) AS cnt FROM memory_usage_log WHERE intent IS NOT NULL AND intent != '' GROUP BY intent ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        intent_distribution = {r["intent"]: r["cnt"] for r in intent_rows}

        counts: Dict[int, int] = {}
        for r in self.conn.execute(
            "SELECT retrieved_fact_ids FROM memory_usage_log ORDER BY logged_at DESC LIMIT 500"
        ).fetchall():
            ids_str = r["retrieved_fact_ids"] or ""
            for tok in ids_str.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    fid = int(tok)
                except ValueError:
                    continue
                counts[fid] = counts.get(fid, 0) + 1

        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_facts]
        facts: List[Dict[str, Any]] = []
        for fid, cnt in top:
            row = self.conn.execute("SELECT content, category FROM memory WHERE id = ?", (fid,)).fetchone()
            content = row["content"] if row else "(fact deprecated or deleted)"
            category = row["category"] if row else None
            facts.append({"fact_id": fid, "content": content, "category": category, "retrieval_count": cnt})

        recent = [
            {
                "query_text": r["query_text"],
                "intent": r["intent"],
                "agent_type": r["agent_type"],
                "result_count": r["result_count"],
                "logged_at": r["logged_at"],
            }
            for r in self.conn.execute(
                "SELECT query_text, intent, agent_type, result_count, logged_at FROM memory_usage_log ORDER BY logged_at DESC LIMIT 10"
            ).fetchall()
        ]

        return {
            "total_retrievals": total,
            "intent_distribution": intent_distribution,
            "most_retrieved_facts": facts,
            "recent_queries": recent,
        }

    def _structural_scores(self, text: str, category: Optional[str] = None, tag: Optional[str] = None,
                           session_id: Optional[str] = None) -> List[Tuple[int, float]]:
        """
        Structural lane: match query tokens against structural node names.
        

        Returns list of (fact_id, score) tuples ranked by match quality.
        Score is based on match tier (exact name match = 1.0, substring = 0.7).
        
        Matching logic (deterministic, no LLM):
        - Tier 1 (score 1.0): query token exactly equals node.name (case-insensitive)
        - Tier 2 (score 0.7): query token is substring of node.name or vice versa (case-insensitive)
        - For methods, also match against "class_name.name" composite (e.g., "MemoryStore.query_hybrid")
        
        Only includes facts that are currently active (deprecated_at IS NULL), verified
        at query time via fact_node_links join. Whole-file template stubs
        ("Source file:", "Key file:", "Directory '") are excluded via
        corpus_hygiene.STUB_PREFIXES: their line_start=1 ranges overlap every
        node in their file, so without this filter they drown the lane.
        """
        query_tokens = tokenize_text(text)
        if not query_tokens:
            return []

        # Build category/tag filters for the final fact join
        fact_filters = ["m.deprecated_at IS NULL"]
        fact_params = []
        if category:

            fact_filters.append("m.category = ?")
            fact_params.append(category.lower().strip())
        if tag:
            fact_filters.append("m.tags LIKE ?")
            fact_params.append(f"%{tag.strip()}%")
        if session_id:
            fact_filters.append("m.session_id = ?")
            fact_params.append(session_id)
        # Stub exclusion: template file-inventory facts carry no node-level
        # signal but link to every node in their file. Prefixes are the single
        # source of truth (tags are unreliable: stubs with tags=None and
        # junk-tagged non-stubs both exist).
        fact_filters.append(
            " AND ".join("m.content NOT LIKE ?" for _ in STUB_PREFIXES)
        )
        fact_params.extend(f"{p}%" for p in STUB_PREFIXES)
        fact_where = " AND ".join(fact_filters)

        # Fetch all structural nodes (full scan). A bounded scan here would be
        # an unordered pre-filter, not a ranked top-K: with no ORDER BY the
        # capped slice silently drops real nodes before RRF fusion sees them.
        rows = self.conn.execute(
            """
            SELECT id, name, node_type, class_name
            FROM structural_nodes
            """
        ).fetchall()

        matched_fact_ids: Dict[int, float] = {}
        for node in rows:
            node_id = node["id"]
            name = node["name"] or ""
            class_name = node["class_name"] or ""
            node_type = node["node_type"]

            # Build lowercase match candidates (methods also match "Class.method")
            match_names = [name.lower()]
            if class_name and node_type == "method":
                match_names.append(f"{class_name}.{name}".lower())

            best_node_score = 0.0
            for q in query_tokens:
                q_lower = q.lower()
                for match_name in match_names:
                    # Tier 1: exact match
                    if q_lower == match_name:
                        best_node_score = max(best_node_score, 1.0)
                    # Tier 2: substring match (either direction)
                    elif q_lower in match_name or match_name in q_lower:
                        best_node_score = max(best_node_score, 0.7)
            
            if best_node_score > 0:
                # Resolve fact_ids via fact_node_links, filtering for active facts at query time
                fact_rows = self.conn.execute(
                    f"""
                    SELECT fnl.fact_id
                    FROM fact_node_links fnl
                    JOIN memory m ON fnl.fact_id = m.id
                    WHERE fnl.node_id = ? AND {fact_where}
                    """,
                    [node_id] + fact_params
                ).fetchall()
                
                for f_row in fact_rows:
                    fact_id = f_row["fact_id"]
                    # Keep the best score across multiple matched nodes for the same fact
                    matched_fact_ids[fact_id] = max(matched_fact_ids.get(fact_id, 0.0), best_node_score)
        
        # Convert to sorted list of (fact_id, score)
        scored = sorted(matched_fact_ids.items(), key=lambda x: x[1], reverse=True)
        return scored[:50]  # same cap as other lanes

    def query(self, text: str, category: Optional[str] = None, limit: int = 10,
              include_deprecated: bool = False, tag: Optional[str] = None,
              session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_LIMIT))
        if not text or not text.strip():
            return []
        
        # Clean terms for safe FTS5 matching
        cleaned_text = re.sub(r'[^\w\s]', ' ', text.strip())
        terms = cleaned_text.split()
        if not terms:
            terms = text.strip().split()
        
        safe_terms = [t.replace('"', '') for t in terms if t.strip()]
        fts_query = " ".join(f'"{t}"*' for t in safe_terms if t) if safe_terms else ""


        sql = (
            "SELECT m.* FROM memory m "
            "JOIN memory_fts f ON m.id = f.rowid "
            "WHERE memory_fts MATCH ? "
        )
        params: List[Any] = [fts_query]
        if category:
            sql += " AND m.category = ? "
            params.append(category.lower().strip())
        if tag:
            sql += " AND m.tags LIKE ? "
            params.append(f"%{tag.strip()}%")
        if session_id:
            sql += " AND m.session_id = ? "
            params.append(session_id)
        if not include_deprecated:
            sql += " AND m.deprecated_at IS NULL "
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        try:
            if not fts_query:
                raise sqlite3.OperationalError("empty fts query")
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Fallback to multi-column LIKE matching
            like = f"%{text.strip()}%"
            sql2 = "SELECT * FROM memory WHERE (content LIKE ? OR rationale LIKE ? OR tags LIKE ? OR file_path LIKE ?)"
            params2: List[Any] = [like, like, like, like]
            if category:
                sql2 += " AND category = ?"
                params2.append(category.lower().strip())
            if tag:
                sql2 += " AND tags LIKE ?"
                params2.append(f"%{tag.strip()}%")
            if session_id:
                sql2 += " AND session_id = ?"
                params2.append(session_id)
            if not include_deprecated:
                sql2 += " AND deprecated_at IS NULL"
            sql2 += " LIMIT ?"
            params2.append(limit)
            rows = self.conn.execute(sql2, params2).fetchall()

        return [self._row_to_dict(r) for r in rows]

    _FILE_MENTION_RE = re.compile(
        r"[A-Za-z0-9_\-./\\]+\.(?:py|js|jsx|ts|tsx|rs|go|java|cs|rb|php|sh|ps1|md|sql|json|toml|ya?ml|txt)\b",
        re.IGNORECASE,
    )

    def _mentioned_files(self, text: str) -> List[str]:
        """Extract file-path-like mentions from query text, normalized to lowercase forward slashes."""
        mentions: List[str] = []
        for m in self._FILE_MENTION_RE.finditer(text or ""):
            token = m.group(0).strip().replace("\\", "/").lower().strip(".,;:")
            if token and token not in mentions:
                mentions.append(token)
        return mentions

    def query_hybrid(self, text: str, category: Optional[str] = None, tag: Optional[str] = None,
                      limit: int = 10, expand_graph: bool = True, session_id: Optional[str] = None,
                      repo_id: Optional[int] = None, intent: str = "", agent_type: str = "",
                      log_usage: bool = True, explain: bool = False) -> List[Dict[str, Any]]:
        """
        7-lane Hybrid RRF query (4 memory lanes + 3 episodic lanes):
          Memory Lane 1 — FTS5 BM25 lexical (memory_fts)
          Memory Lane 2 — Tokenized Jaccard over memory
          Memory Lane 3 — Semantic cosine over memory_embeddings/vec_memory
          Episodic Lane 1b — FTS5 over episodic_fts (root ACTIVE only)
          Episodic Lane 2b — Tokenized Jaccard over episodic_memories (root ACTIVE)
          Episodic Lane 3b — Semantic cosine over episodic_embeddings/vec_episodic_memory (root ACTIVE)
          Lane 4 — Structural node name matching (memory-only, unchanged)

        explain=True attaches a per-result `lanes` trace (every contributing
        lane with its rank and weight) and `fused_score`. The fused number is
        otherwise not decomposable: seven lanes combine with per-agent weights,
        intent boosts, importance weighting, recency decay and file-anchor
        boosts. Opt-in because the trace would multiply the token cost of every
        result on every query. This is the tool for verifying a lane change --
        the MAX_LIMIT fix, for instance, could previously only be evidenced by a
        before/after eval-gate comparison.

        All lanes are fused via RRF (k=5) with importance + recency weighting,
        but RRF is now keyed by (source, id) tuples to avoid the integer ID-space
        collision between memory.id and episodic_memories.memory_id (proven to collide
        at 500). Each result is tagged with "source": "memory" or "episodic".

        repo_id: if provided, episodic lanes are filtered to that repo's root branch_id.
          If None, all root branches are considered — this is a real gap: callers
          should pass repo_id when available, otherwise "all roots" is used rather than
          silently defaulting to "all branches" (which would leak experimental content).
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        if not text or not text.strip():
            return []

        if not intent:
            intent = self._detect_intent(text)

        # Determine root branch_ids for episodic filtering
        # Gap: if repo_id is None, no caller passed repo context. For single-repo deployment we derive
        # the "own" repo as the first repo in the DB (ORDER BY repo_id LIMIT 1) and use its root.
        # This is safer than unioning all roots (which would leak cross-repo canonical content in multi-repo).
        # Callers that know their repo should pass repo_id explicitly; server.py now does so via store's default repo.
        try:
            if repo_id is None:
                repo_id = self._derive_default_repo_id()
            if repo_id is not None:
                root_ids = [r[0] for r in self.conn.execute("SELECT branch_id FROM memory_branches WHERE is_root=1 AND repo_id=?", (repo_id,)).fetchall()]
            else:
                root_ids = []
        except sqlite3.OperationalError:
            root_ids = []

        # --- Memory Lane 1: FTS5 BM25 lexical ---
        fts_results = self.query(text, category=category, tag=tag, limit=50, session_id=session_id)

        # --- Memory Lane 2: Tokenized Jaccard ---
        # limit=None: the lane scored only the 200 most recent facts, so older
        # facts could never compete. Unlike the STRUCTURAL lane (whose cap has
        # a standing do-not-touch rule, fact #498) this lane is pure token
        # overlap with no structural coupling, so untruncating it only widens
        # candidacy. The eval gate is the arbiter: if this re-ranks and
        # regresses, revert here rather than adjusting the baseline.
        all_active = self.get_state(category=category, limit=None, session_id=session_id)
        query_words = tokenize_text(text)
        query_lower = text.lower().strip()
        tok_scores: List[Tuple[int, float]] = []
        for f in all_active:
            if tag and (not f.get("tags") or tag.lower() not in f["tags"].lower()):
                continue
            combined = (
                f"{f['content']} {f.get('rationale') or ''} "
                f"{f.get('tags') or ''} {f.get('file_path') or ''}"
            )
            doc_words = tokenize_text(combined)
            if doc_words and query_words:
                overlap = query_words & doc_words
                if overlap:
                    base_sim = len(overlap) / (len(query_words | doc_words) ** 0.5)
                    exact_bonus = 1.3 if query_lower in combined.lower() else 1.0
                    imp_boost = 1.0 + (f.get("importance", 3) - 1) * 0.08
                    # De-boost generic file-coverage bootstrap facts (templated "Documentation: X (Y lines)") to avoid docs outranking code
                    tags_lower = (f.get("tags") or "").lower()
                    if "file-coverage" in tags_lower:
                        imp_boost *= 0.55
                    elif "bootstrap" in tags_lower and f.get("category") == "other":
                        imp_boost *= 0.8
                    tok_scores.append((f["id"], base_sim * exact_bonus * imp_boost))
        tok_scores.sort(key=lambda x: x[1], reverse=True)
        tok_top = tok_scores[:50]

        # --- Memory Lane 3: Semantic cosine ---
        query_vec = self._query_encode(text)
        if getattr(self, '_vec_available', False) and query_vec is not None:
            sem_scores = self._semantic_scores_vec(query_vec, session_id=session_id)
        else:
            sem_scores = self._semantic_scores(query_vec, session_id=session_id)
        sem_top = sorted(sem_scores.items(), key=lambda x: x[1], reverse=True)[:50]

        # --- Episodic Lane 1b: FTS5 over episodic_fts (root ACTIVE only) ---
        episodic_fts_results: List[Dict[str, Any]] = []
        try:
            # Build FTS query like query() does
            cleaned = __import__('re').sub(r'[^\w\s]', ' ', text.strip())
            terms = cleaned.split()
            safe_terms = [t.replace('"', '') for t in terms if t.strip()]
            fts_q = " ".join(f'"{t}"*' for t in safe_terms if t) if safe_terms else ""
            if fts_q and root_ids:
                placeholders_roots = ",".join("?" * len(root_ids))
                sql = (
                    "SELECT em.* FROM episodic_memories em "
                    "JOIN episodic_fts f ON em.memory_id = f.rowid "
                    f"WHERE episodic_fts MATCH ? AND em.branch_id IN ({placeholders_roots}) AND em.status='ACTIVE' AND em.closed_revision IS NULL "
                )
                params: List[Any] = [fts_q] + root_ids
                if category:
                    sql += " AND em.category = ? "
                    params.append(category.lower().strip())
                if tag:
                    sql += " AND em.tags LIKE ? "
                    params.append(f"%{tag.strip()}%")
                sql += " ORDER BY rank LIMIT 50"
                rows = self.conn.execute(sql, params).fetchall()
                for r in rows:
                    # normalize to same shape as memory for RRF, but keep episodic id
                    episodic_fts_results.append({"id": r["memory_id"], "source": "episodic", "_row": r})
        except sqlite3.OperationalError:
            # episodic_fts may not exist on old DBs
            pass

        # --- Episodic Lane 2b: Tokenized Jaccard over episodic_memories ---
        episodic_tok_scores: List[Tuple[int, float]] = []
        try:
            all_episodic = self.get_episodic_state(category=category, limit=MAX_LIMIT, repo_id=repo_id)
            for f in all_episodic:
                if tag and (not f.get("tags") or tag.lower() not in f["tags"].lower()):
                    continue
                combined = f"{f['content']} {f.get('tags') or ''} {f.get('category') or ''}"
                doc_words = tokenize_text(combined)
                if doc_words and query_words:
                    overlap = query_words & doc_words
                    if overlap:
                        base_sim = len(overlap) / (len(query_words | doc_words) ** 0.5)
                        exact_bonus = 1.3 if query_lower in combined.lower() else 1.0
                        imp_boost = 1.0 + (f.get("importance", 3) - 1) * 0.08
                        episodic_tok_scores.append((f["memory_id"], base_sim * exact_bonus * imp_boost))
            episodic_tok_scores.sort(key=lambda x: x[1], reverse=True)
        except Exception as e:
            logger.warning("episodic tokenized lane failed for query %r: %r", text[:80], e)
        episodic_tok_top = episodic_tok_scores[:50]

        # --- Episodic Lane 3b: Semantic cosine over episodic_embeddings ---
        episodic_sem_scores: Dict[int, float] = {}
        episodic_sem_top: List[Tuple[int, float]] = []
        try:
            if getattr(self, '_vec_available', False) and query_vec is not None:
                episodic_sem_scores = self._semantic_scores_vec_episodic(query_vec, repo_id=repo_id)
            else:
                episodic_sem_scores = self._semantic_scores_episodic(query_vec, repo_id=repo_id)
            episodic_sem_top = sorted(episodic_sem_scores.items(), key=lambda x: x[1], reverse=True)[:50]
        except Exception as e:
            logger.warning("episodic semantic lane failed for query %r: %r", text[:80], e)

        # Lane weights are resolved before Lane 4 so a zero-weighted
        # structural lane can skip its scan entirely (see below).
        # Per-agent lane weight tuning: different LLM consumers benefit from
        # different retrieval-lane emphasis (e.g. cursor leans on structural).
        AGENT_LANE_WEIGHTS = {
            "default":  {"fts": 1.0,  "tok": 0.85, "sem": 1.2, "struct": 0.0,  "epi_fts": 1.0,  "epi_tok": 0.85, "epi_sem": 1.2},
            "cursor":   {"fts": 1.0,  "tok": 0.85, "sem": 1.3, "struct": 1.2,  "epi_fts": 1.0,  "epi_tok": 0.85, "epi_sem": 1.2},

            "claude":   {"fts": 1.1,  "tok": 0.85, "sem": 1.3, "struct": 1.0,  "epi_fts": 1.1,  "epi_tok": 0.85, "epi_sem": 1.3},
            "opencode": {"fts": 1.0,  "tok": 0.9,  "sem": 1.2, "struct": 1.1,  "epi_fts": 1.0,  "epi_tok": 0.9,  "epi_sem": 1.2},
            "zed":      {"fts": 1.05, "tok": 0.85, "sem": 1.2, "struct": 1.15, "epi_fts": 1.0,  "epi_tok": 0.85, "epi_sem": 1.2},
        }

        lane_w = AGENT_LANE_WEIGHTS.get(agent_type, AGENT_LANE_WEIGHTS["default"])

        # --- Lane 4: Structural (memory-only) ---
        # The default struct weight is 0: the lane contributes nothing to
        # fusion (stub-excluded sweep: w=0.1 bit-identical to baseline,
        # w>=0.2 loses recall@5). A zero weight skips
        # the full-table scan entirely rather than computing scores only to
        # multiply them by zero. Non-zero agent weights (e.g. cursor) still
        # scan; their lane output is stub-excluded (see _structural_scores).
        struct_top = (
            self._structural_scores(text, category=category, tag=tag, session_id=session_id)
            if lane_w["struct"] != 0 else []
        )

        # --- RRF Fusion with recency decay + importance weighting — now keyed by (source, id) ---
        rrf: Dict[tuple, float] = {}
        # k=5 chosen via holdout-validated 52-pair golden sweep (Phase 5);
        # k=60 measured near-worst (recall@5 0.583 tuning / 0.625 holdout).
        # k stays 5 deliberately: the LIMIT-200 retune re-tested k in {1,2,3,4}
        # and every k<5 reproduces the Phase 5 rejection (holdout #173 lost,
        # recall@10 1.0->0.9375). The #173 loss is a k-effect, not a lane effect.
        k = 5

        # Fetch recency/access info for memory candidates (episodic has no access_count)
        all_memory_ids = (
            {r["id"] for r in fts_results}
            | {fid for fid, _ in tok_top}
            | {fid for fid, _ in sem_top}
            | {fid for fid, _ in struct_top}
        )
        recency_info: Dict[int, tuple] = {}
        # Also fetch tags for file-coverage de-boost in RRF
        tags_info: Dict[int, str] = {}
        category_info: Dict[int, str] = {}
        file_info: Dict[int, str] = {}
        if all_memory_ids:
            placeholders = ",".join("?" * len(all_memory_ids))
            for row in self.conn.execute(
                f"SELECT id, created_at, access_count, importance, tags, category, file_path FROM memory WHERE id IN ({placeholders})",
                list(all_memory_ids)
            ).fetchall():
                recency_info[row["id"]] = (row["created_at"], row["access_count"] or 0, row["importance"] or 3)
                tags_info[row["id"]] = row["tags"] or ""
                category_info[row["id"]] = row["category"] or ""
                file_info[row["id"]] = row["file_path"] or ""

        # For episodic, fetch created_at/importance for recency (no access_count)
        all_episodic_ids = (
            {r["id"] for r in episodic_fts_results}
            | {fid for fid, _ in episodic_tok_top}
            | {fid for fid, _ in episodic_sem_top}
        )
        episodic_recency: Dict[int, tuple] = {}
        if all_episodic_ids:
            placeholders = ",".join("?" * len(all_episodic_ids))
            try:
                for row in self.conn.execute(
                    f"SELECT memory_id, created_at, importance FROM episodic_memories WHERE memory_id IN ({placeholders})",
                    list(all_episodic_ids)
                ).fetchall():
                    episodic_recency[row["memory_id"]] = (row["created_at"], 0, row["importance"] or 3)
            except sqlite3.OperationalError:
                pass

        # Deprecation is authoritative in `memory`, not in the episodic store.
        # MemoryStore.deprecate() sets memory.deprecated_at and deliberately
        # leaves episodic_memories.status alone, because that table is the
        # historical/branch record. But episodic lanes 1b/2b/3b filter only
        # episodic status, so a deprecated fact used to keep ranking in hybrid
        # results -- the default path behind memory_query, memory_get_context
        # and prepare_task. `memory` is the single source of truth for whether a
        # fact is active, so drop any episodic candidate whose memory row is
        # deprecated. An episodic id with NO memory row is not deprecated and
        # must still be served (that is a real state, not a filtered one).
        deprecated_memory_ids: set = set()
        if all_episodic_ids:
            placeholders = ",".join("?" * len(all_episodic_ids))
            try:
                deprecated_memory_ids = {
                    r["id"] for r in self.conn.execute(
                        f"SELECT id FROM memory WHERE deprecated_at IS NOT NULL "
                        f"AND id IN ({placeholders})",
                        list(all_episodic_ids),
                    ).fetchall()
                }
            except sqlite3.OperationalError:
                deprecated_memory_ids = set()

        # File-anchor boost: facts anchored to a file named (or basenamed) in the
        # query rank higher, so "fix watcher.py ..." surfaces watcher.py facts.
        file_mentions = self._mentioned_files(text)
        file_anchored_ids: set = set()
        if all_memory_ids:
            for fid in all_memory_ids:
                fp = file_info.get(fid, "").strip().replace("\\", "/").lower()
                if not fp:
                    continue
                hit = any(self._paths_match(m, fp) for m in file_mentions)
                if not hit:
                    fname = fp.split("/")[-1]
                    stem = fname.rsplit(".", 1)[0]
                    hit = fname in query_words or (len(stem) >= 4 and stem in query_words)
                if hit:
                    file_anchored_ids.add(fid)

        INTENT_CATEGORY_BOOSTS = {
            "fix": {"constraint": 1.5, "decision": 1.2, "state": 1.1},
            "implement": {"decision": 1.4, "component": 1.2, "constraint": 1.15},
            "refactor": {"decision": 1.4, "constraint": 1.3},
            "add": {"component": 1.3, "decision": 1.2},
            "remove": {"constraint": 1.4, "component": 1.2},
            "optimize": {"constraint": 1.3, "decision": 1.2},
            "test": {"constraint": 1.3, "component": 1.15},
            "debug": {"state": 1.4, "component": 1.2},
            "review": {"constraint": 1.3, "decision": 1.2},
            "document": {"component": 1.2},
        }
        intent_boosts = INTENT_CATEGORY_BOOSTS.get(intent, {})

        # (AGENT_LANE_WEIGHTS lives above Lane 4 so a zero struct weight can
        # skip the structural scan; lane_w is already resolved there.)
        usage_boosts = self._usage_boost(text)

        # Per-lane provenance, recorded only when asked for: (source, id) ->
        # [{lane, rank, weight}]. Always collected into a local so the fusion
        # arithmetic below is untouched, and only attached to results when
        # explain=True.
        lane_trace: Dict[tuple, List[Dict[str, Any]]] = {}

        def rrf_add(source: str, fact_id: int, rank: int, lane_weight: float = 1.0,
                    lane: str = ""):
            if source == "episodic" and fact_id in deprecated_memory_ids:
                return  # deprecated in `memory`; the episodic copy is history
            if lane:
                lane_trace.setdefault((source, fact_id), []).append(
                    {"lane": lane, "rank": rank, "weight": round(lane_weight, 4)})
            if source == "memory":
                created_at, access_count, importance = recency_info.get(fact_id, (None, 0, 3))
                tags = tags_info.get(fact_id, "")
                # De-boost generic file-coverage bootstrap docs in RRF as well
                if "file-coverage" in tags.lower():
                    lane_weight *= 0.6
                elif "bootstrap" in tags.lower():
                    # Slight de-boost for pure bootstrap file lists vs curated phase facts
                    lane_weight *= 0.9
                cat = category_info.get(fact_id)
                if cat in intent_boosts:
                    lane_weight *= intent_boosts[cat]
                if fact_id in usage_boosts:
                    lane_weight *= usage_boosts[fact_id]
                if fact_id in file_anchored_ids:
                    lane_weight *= 1.6
            else:
                created_at, access_count, importance = episodic_recency.get(fact_id, (None, 0, 3))
            recency = self._recency_weight(created_at, access_count)
            imp_mult = 1.0 + (importance - 1) * 0.05
            key = (source, fact_id)
            rrf[key] = rrf.get(key, 0.0) + (1.0 / (k + rank)) * recency * imp_mult * lane_weight

        for rank, r in enumerate(fts_results, start=1):
            rrf_add("memory", r["id"], rank, lane_weight=lane_w["fts"], lane="fts")
        for rank, (fid, score) in enumerate(tok_top, start=1):
            if score > 0:
                rrf_add("memory", fid, rank, lane_weight=lane_w["tok"], lane="tok")
        for rank, (fid, sim) in enumerate(sem_top, start=1):
            if sim > 0.1:
                rrf_add("memory", fid, rank, lane_weight=lane_w["sem"], lane="sem")
        for rank, r in enumerate(episodic_fts_results, start=1):
            rrf_add("episodic", r["id"], rank, lane_weight=lane_w["epi_fts"], lane="epi_fts")
        for rank, (fid, score) in enumerate(episodic_tok_top, start=1):
            if score > 0:
                rrf_add("episodic", fid, rank, lane_weight=lane_w["epi_tok"], lane="epi_tok")
        for rank, (fid, sim) in enumerate(episodic_sem_top, start=1):
            if sim > 0.1:
                rrf_add("episodic", fid, rank, lane_weight=lane_w["epi_sem"], lane="epi_sem")
        for rank, (fid, score) in enumerate(struct_top, start=1):
            if score > 0:
                rrf_add("memory", fid, rank, lane_weight=lane_w["struct"], lane="struct")

        sorted_keys = sorted(rrf.keys(), key=lambda x: rrf[x], reverse=True)[:limit]
        if not sorted_keys:
            return []

        # Split by source for separate SELECTs
        memory_ids = [fid for src, fid in sorted_keys if src == "memory"]
        episodic_ids = [fid for src, fid in sorted_keys if src == "episodic"]

        row_dict: Dict[tuple, Dict[str, Any]] = {}
        if memory_ids:
            placeholders = ",".join("?" * len(memory_ids))
            rows = self.conn.execute(f"SELECT * FROM memory WHERE id IN ({placeholders})", memory_ids).fetchall()
            for r in rows:
                d = self._row_to_dict(r)
                d["source"] = "memory"
                row_dict[("memory", r["id"])] = d
        if episodic_ids:
            placeholders = ",".join("?" * len(episodic_ids))
            try:
                rows = self.conn.execute(f"SELECT * FROM episodic_memories WHERE memory_id IN ({placeholders})", episodic_ids).fetchall()
                for r in rows:
                    d = dict(r)
                    # normalize episodic to same shape as memory where possible
                    d["id"] = r["memory_id"]
                    d["source"] = "episodic"
                    # map episodic fields to memory-like keys for display
                    d["rationale"] = None
                    d["file_path"] = None
                    d["line_start"] = None
                    d["line_end"] = None
                    d["code_hash"] = None
                    d["git_commit_sha"] = None
                    d["session_id"] = None
                    d["version"] = 1
                    d["access_count"] = 0
                    d["last_accessed_at"] = None
                    d["superseded_by"] = r["superseded_by_memory_id"]
                    d["deprecated_at"] = None if r["status"] == "ACTIVE" else r["closed_revision"]
                    row_dict[("episodic", r["memory_id"])] = d
            except sqlite3.OperationalError:
                pass


        results = [row_dict[k] for k in sorted_keys if k in row_dict]
        if explain:
            for r in results:
                r["fused_score"] = round(rrf[(r.get("source"), r["id"])], 6)
                r["lanes"] = lane_trace.get((r.get("source"), r["id"]), [])

        # Access counters and usage logging apply only to real agent queries;
        # internal seeding and eval calls pass log_usage=False so they cannot
        # feed a popularity feedback loop into usage learning. Both stay behind
        # the SAME flag (never split): access_count feeds the cold-start boost,
        # so gating only one side would reopen the loop through the back door.
        # Counter bumps + log insert run in one transaction so concurrent
        # retrievals can't interleave them; still best-effort, never breaks
        # retrieval — except cross-thread misuse, which is a real bug.
        if log_usage:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    if memory_ids:
                        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        for fid in memory_ids:
                            if ("memory", fid) in row_dict:
                                self.conn.execute(
                                    "UPDATE memory SET access_count = access_count + 1, last_accessed_at = ? WHERE id = ?",
                                    (now_str, fid),
                                )

                    # Usage logging for pattern learning (best-effort, never breaks retrieval)
                    self._log_usage(text, intent, agent_type or None, memory_ids, len(results), session_id)
                    self.conn.execute("COMMIT")
                except Exception:
                    try:
                        self.conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
            except sqlite3.ProgrammingError as e:
                logger.warning("usage accounting failed (cross-thread?): %r", e)
            except Exception as e:
                logger.debug("usage accounting failed: %r", e)

        # Auto-expand graph neighbors only for memory (episodic graph not yet)
        if expand_graph:
            for item in results:
                if item.get("source") != "memory":
                    continue
                fid = item["id"]
                out_edges = self.get_graph(fid, max_depth=1)
                inc_edges = self.get_incoming_graph(fid)
                if out_edges or inc_edges:
                    item["graph_neighbors"] = out_edges
                    item["incoming_neighbors"] = inc_edges

        return results

    def search(self, text: str, **kwargs) -> List[Dict[str, Any]]:
        """Convenient alias for query_hybrid."""
        return self.query_hybrid(text, **kwargs)

    def _detect_intent(self, query: str) -> str:
        """
        Detect the user's intent from the query to enable intent-based context expansion.
        Returns one of: 'implement', 'fix', 'refactor', 'add', 'remove', 'optimize',
        'test', 'document', 'debug', 'review'.

        Matching uses word boundaries and is ordered most-specific first, so a
        specific intent (fix/test/refactor) wins over the generic 'implement'
        when both match (e.g. "write unit tests" -> 'test', not 'implement').
        """
        query_lower = query.lower().strip()

        # Ordered most-specific to most-generic; first match wins.
        ordered_intents = [
            ('fix', ['fix', 'bug', 'error', 'issue', 'correct', 'resolve', 'patch', 'broken']),
            ('debug', ['debug', 'troubleshoot', 'investigate', 'trace']),
            ('test', ['tests', 'test', 'unit test', 'integration test', 'coverage', 'jest', 'pytest', 'spec']),
            ('refactor', ['refactor', 'clean', 'restructure', 'reorganize', 'simplify', 'modernize']),
            ('optimize', ['optimize', 'performance', 'speed', 'efficient', 'cache', 'memoize']),
            ('remove', ['remove', 'delete', 'eliminate', 'cleanup', 'prune']),

            ('document', ['document', 'docs', 'readme', 'comment', 'explain']),
            ('review', ['review', 'audit', 'verify', 'inspect', 'analyze']),
            ('add', ['add', 'extend', 'include', 'append', 'integrate']),
            ('implement', ['implement', 'create', 'build', 'develop', 'write']),
        ]

        for intent, keywords in ordered_intents:
            for keyword in keywords:
                if re.search(r'\b' + re.escape(keyword) + r'\b', query_lower):
                    return intent

        return 'implement'

