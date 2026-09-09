"""Regression: _find_near_duplicate must catch embed=False facts even when an encoder is available.

Bug: when _encode() returned a vector, _find_near_duplicate took the semantic
path and returned None on no match without trying the tokenized-Jaccard
fallback, so facts stored with embed=False (no vector row) were invisible to dedup.

Run with: python -m pytest test_dedup_embed.py -v
"""
import tempfile
import os

import pytest

import memory_store as ms_mod
import pm_search
from memory_store import MemoryStore


class _FakeVec:
    def tobytes(self):
        return b"fake-vector-123"


def _install_fake_encoder(monkeypatch):
    """Simulate 'encoder available': _encode returns non-None; semantic scores
    return 1.0 only for facts that actually have an embedding row."""
    def fake_encode(text):
        if not text or not text.strip():
            return None
        return _FakeVec()

    def fake_semantic_scores(self, query_vec, session_id=None):
        if query_vec is None:
            return {}
        rows = self.conn.execute(
            "SELECT me.fact_id FROM memory_embeddings me "
            "JOIN memory m ON me.fact_id = m.id "
            "WHERE m.deprecated_at IS NULL"
        ).fetchall()
        return {r["fact_id"]: 1.0 for r in rows}

    monkeypatch.setattr(ms_mod, "_encode", fake_encode)
    monkeypatch.setattr(
        pm_search.SearchMixin, "_semantic_scores", fake_semantic_scores
    )


@pytest.fixture
def temp_store():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    store = MemoryStore(db_path=tmp.name)
    store._current_git_commit = lambda: None
    yield store
    try:
        store.close()
    except Exception:
        pass
    try:
        os.unlink(tmp.name)
    except Exception:
        pass


TEXT = "dedup probe alpha beta gamma delta epsilon zeta"
RAT = "probe rationale one"


def test_embed_false_existing_caught_when_encoder_available(temp_store, monkeypatch):
    _install_fake_encoder(monkeypatch)
    a = temp_store.write(
        category="other", content=TEXT, rationale=RAT,
        embed=False, force=True,
    )
    assert a > 0
    assert list(temp_store.conn.execute("SELECT fact_id FROM memory_embeddings")) == []
    dup = temp_store.write(category="other", content=TEXT, rationale=RAT, embed=True)
    assert dup == -a, f"identical fact escaped dedup (got {dup}, want {-a})"


def test_embed_true_existing_caught_when_encoder_available(temp_store, monkeypatch):
    _install_fake_encoder(monkeypatch)
    a = temp_store.write(
        category="other", content=TEXT, rationale=RAT,
        embed=True, force=True,
    )
    assert a > 0
    dup = temp_store.write(category="other", content=TEXT, rationale=RAT, embed=False)
    assert dup == -a


def test_both_embed_false_caught_when_encoder_available(temp_store, monkeypatch):
    _install_fake_encoder(monkeypatch)
    a = temp_store.write(
        category="other", content=TEXT, rationale=RAT,
        embed=False, force=True,
    )
    assert a > 0
    dup = temp_store.write(category="other", content=TEXT, rationale=RAT, embed=False)
    assert dup == -a
