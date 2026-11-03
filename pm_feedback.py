"""Verified, task-conditioned retrieval feedback for CogniGraph.

Exposure is not success: a fact being retrieved never counts as positive
feedback. Only separately recorded, attributable outcomes affect ranking.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from pm_common import looks_like_secret, tokenize_text

_FEEDBACK_VALUES = {"helpful", "irrelevant", "incorrect"}
_EVIDENCE_VALUES = {"user", "test", "agent"}
_STOPWORDS = frozenset({
    "the", "a", "an", "i", "we", "my", "your", "our", "and", "or", "but",
    "is", "are", "was", "were", "to", "from", "with", "for", "of", "in",
    "on", "at", "by", "into", "this", "that", "these", "those", "can",
    "you", "it", "its", "do", "did", "does", "please", "need", "want",
    "how", "what", "when", "where", "why", "who", "all", "any", "some",
})


def _topic_tokens(text: str) -> set[str]:
    return {word for word in tokenize_text(text or "") - _STOPWORDS if len(word) > 2}


class FeedbackMixin:
    """Adds bounded relevance feedback to the existing MemoryStore connection."""

    def _ensure_relevance_feedback_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_relevance_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                retrieval_id INTEGER NOT NULL REFERENCES memory_usage_log(id) ON DELETE CASCADE,
                fact_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
                rating TEXT NOT NULL CHECK(rating IN ('helpful', 'irrelevant', 'incorrect')),
                evidence_type TEXT NOT NULL CHECK(evidence_type IN ('user', 'test', 'agent')),
                evidence_ref TEXT,
                recorded_at TEXT NOT NULL,
                UNIQUE (retrieval_id, fact_id)
            );
            CREATE INDEX IF NOT EXISTS idx_relevance_feedback_date
              ON memory_relevance_feedback(recorded_at);
            CREATE INDEX IF NOT EXISTS idx_relevance_feedback_fact
              ON memory_relevance_feedback(fact_id);
        """)

    def record_relevance_feedback(
        self, retrieval_id: int, ratings: List[Dict[str, Any]],
        evidence_type: str = "agent", evidence_ref: str = "",
        verification_evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Rate only facts returned by this retrieval, with explicit evidence.

        Agent-only opinions are logged but NEVER affect ranking.
        Test evidence is checked against HEAD, but commands are not executed
        by CogniGraph; caller-provided results are not independent verification.
        """
        try:
            rid = int(retrieval_id)
        except (TypeError, ValueError) as e:
            raise ValueError("retrieval_id must be an integer") from e
        if rid < 1 or not isinstance(ratings, list) or not (1 <= len(ratings) <= 20):
            raise ValueError("provide a valid retrieval_id and 1-20 ratings")
        if evidence_type not in _EVIDENCE_VALUES:
            raise ValueError("evidence_type must be user, test, or agent")
        if not isinstance(evidence_ref, str) or len(evidence_ref) > 256 or looks_like_secret(evidence_ref):
            raise ValueError("evidence_ref is too long or contains a suspected secret")
        if evidence_type == "test":
            ev = verification_evidence or {}
            current = self._current_git_commit()
            if not (
                isinstance(ev, dict)
                and isinstance(ev.get("command"), str)
                and ev["command"].strip()
                and type(ev.get("exit_code")) is int and ev["exit_code"] == 0
                and isinstance(ev.get("commit_sha"), str)
                and current
                and ev["commit_sha"].strip() == current.strip()
            ):
                raise ValueError("test evidence requires command, exit_code=0, and matching HEAD commit")

        row = self.conn.execute(
            "SELECT query_text, retrieved_fact_ids FROM memory_usage_log WHERE id=?", (rid,)
        ).fetchone()
        if row is None or looks_like_secret(row["query_text"]):
            raise ValueError("unknown retrieval or query contains a suspected secret")
        retrieved = {int(x) for x in (row["retrieved_fact_ids"] or "").split(",") if x.isdigit()}
        normalized: List[tuple[int, str]] = []
        seen: set[int] = set()
        for item in ratings:
            if not isinstance(item, dict):
                raise ValueError("each rating must have fact_id and rating")
            try:
                fact_id = int(item["fact_id"])
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError("fact_id must be an integer") from e
            rating = item.get("rating")
            if rating not in _FEEDBACK_VALUES or fact_id not in retrieved or fact_id in seen:
                raise ValueError("rating must be valid and fact_id must be unique and in this retrieval")
            active = self.conn.execute(
                "SELECT 1 FROM memory WHERE id=? AND deprecated_at IS NULL", (fact_id,)
            ).fetchone()
            if active is None:
                raise ValueError("cannot give feedback on an inactive or non-memory fact")
            normalized.append((fact_id, rating))
            seen.add(fact_id)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for fid, rating in normalized:
                self.conn.execute(
                    """INSERT INTO memory_relevance_feedback
                    (retrieval_id, fact_id, rating, evidence_type, evidence_ref, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(retrieval_id, fact_id) DO UPDATE SET
                       rating=excluded.rating, evidence_type=excluded.evidence_type,
                       evidence_ref=excluded.evidence_ref, recorded_at=excluded.recorded_at""",
                    (rid, fid, rating, evidence_type, evidence_ref or None, now),
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "retrieval_id": rid,
            "recorded": len(normalized),
            "ranking_effect": evidence_type in ("user", "test"),
            "note": "Agent-only ratings are observational and do not alter ranking.",
        }

    def _feedback_boost(self, query: str, lookback: int = 300) -> Dict[int, float]:
        """Compute capped, topic-local feedback multipliers; empty table = no change."""
        tokens = _topic_tokens(query)
        if len(tokens) < 2 or looks_like_secret(query):
            return {}
        rows = self.conn.execute(
            """SELECT f.fact_id, f.rating, f.evidence_type, f.recorded_at,
                      u.query_text
               FROM memory_relevance_feedback f
               JOIN memory_usage_log u ON u.id = f.retrieval_id
               JOIN memory m ON m.id = f.fact_id
               WHERE m.deprecated_at IS NULL AND f.evidence_type IN ('user','test')
               ORDER BY f.id DESC LIMIT ?""",
            (max(1, min(int(lookback), 1000)),),
        ).fetchall()
        now = datetime.datetime.now(datetime.timezone.utc)
        scores: Dict[int, float] = {}
        weights: Dict[int, float] = {}
        for row in rows:
            prior_query = row["query_text"] or ""
            if looks_like_secret(prior_query):
                continue
            prev = _topic_tokens(prior_query)
            shared = tokens & prev
            if len(shared) < 2 or len(shared) / max(1, min(len(tokens), len(prev))) < 0.5:
                continue
            try:
                when = datetime.datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=datetime.timezone.utc)
                days = max(0.0, (now - when).total_seconds() / 86400)
            except (ValueError, TypeError):
                continue
            if days > 90:
                continue
            evidence_weight = 1.2 if row["evidence_type"] == "test" else 1.0
            topic_weight = min(1.0, 2 * len(shared) / max(1, len(tokens | prev)))
            recency_weight = max(0.2, 1 - days / 90)
            weight = evidence_weight * topic_weight * recency_weight
            magnitude = {"helpful": 1.0, "irrelevant": -1.0, "incorrect": -1.5}[row["rating"]]
            fid = row["fact_id"]
            scores[fid] = scores.get(fid, 0.0) + weight * magnitude
            weights[fid] = weights.get(fid, 0.0) + weight
        # A four-pseudo-observation prior prevents one rating dominating ranking.
        return {
            fid: round(max(0.72, min(1.20, 1.0 + 0.4 * score / (4.0 + weights[fid]))), 5)
            for fid, score in scores.items()
        }

    def relevance_feedback_stats(self) -> Dict[str, Any]:
        rows = self.conn.execute(
            """SELECT evidence_type, rating, COUNT(*) AS cnt
            FROM memory_relevance_feedback GROUP BY evidence_type, rating"""
        ).fetchall()
        return {
            "total": sum(row["cnt"] for row in rows),
            "by_evidence_and_rating": {
                f"{row['evidence_type']}:{row['rating']}": row["cnt"] for row in rows
            },
        }
