"""Opt-in field projection on read paths: pay for the keys you actually read.

Measured 2026-09-27 on the live corpus before building this. A routine
10-result hybrid query returns 21 keys per result and 12,311 characters, of
which the reader (an agent) looks at exactly three: `id`, `category`,
`content`. Projected, the same payload is 5,510 chars — a 55.2% reduction.
Four of the 21 keys (`session_id`, `superseded_by`, `deprecated_at`,
`deprecated_reason`) were null on every single result, so they were pure
formatting cost.

The saving is real, but the hazard is not: silently dropping a key means an
agent that needed `file_path` for a provenance check gets a result that looks
complete and simply omits the anchor. So the design rules are:

- Opt-in only. The default response is byte-identical.
- An UNKNOWN field is an error, never a silent no-op. Asking for a field that
  does not exist should fail loudly rather than hand back a plausible-looking
  short result.
- The projection is SELF-DESCRIBING: the response reports which fields were
  projected, so a trimmed result can never be mistaken for a complete one.
- `id` is not projectable away. A result you cannot act on is not a result.

Run with: python -m pytest test_field_projection.py -v
"""
import os
import tempfile

import pytest

from memory_store import MemoryStore
from pm_common import (KNOWN_RESULT_FIELDS, ProjectFieldError, project_fields,
                       parse_fields)


@pytest.fixture
def store():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    s = MemoryStore(db_path=tmp.name)
    s._current_git_commit = lambda: None
    yield s
    try:
        s.close()
    except Exception:
        pass
    try:
        os.unlink(tmp.name)
    except Exception:
        pass


def _seed(store, n=3):
    for i in range(n):
        store.write(category="other", content=f"fact number {i} about widgets",
                    embed=False, force=True)


# --- parsing -------------------------------------------------------------

def test_parses_comma_string():
    assert parse_fields("id, category ,content") == ["id", "category", "content"]


def test_parses_list():
    assert parse_fields(["id", "content"]) == ["id", "content"]


def test_none_and_empty_mean_no_projection():
    assert parse_fields(None) is None
    assert parse_fields("") is None
    assert parse_fields("   ") is None


def test_strips_blanks_from_trailing_commas():
    assert parse_fields("id,content,") == ["id", "content"]


def test_deduplicates_preserving_order():
    assert parse_fields("content,id,content") == ["content", "id"]


# --- the safety rules ----------------------------------------------------

def test_unknown_field_is_an_error_not_a_silent_drop():
    """The whole point of the feature is trusting it; that needs a loud failure."""
    with pytest.raises(ProjectFieldError) as e:
        project_fields([{"id": 1, "content": "x"}], ["id", "nonsense_field"])
    assert "nonsense_field" in str(e.value)


def test_error_lists_the_valid_fields():
    with pytest.raises(ProjectFieldError) as e:
        project_fields([{"id": 1}], ["id", "typo_here"])
    msg = str(e.value)
    assert "id" in msg and "content" in msg


def test_id_cannot_be_projected_away():
    """Auto-added, not required. A required id creates an error path whose
    obvious 'fix' is to drop the field the caller actually wanted."""
    out = project_fields([{"id": 1, "content": "x"}], ["content"])
    assert set(out[0]) == {"id", "content"}


def test_id_is_retained_even_when_another_field_is_requested():
    out = project_fields([{"id": 7, "category": "c", "content": "x",
                           "file_path": "a.py"}], ["content"])
    assert set(out[0]) == {"id", "content"}


def test_field_absent_from_this_result_is_simply_not_invented():
    """A requested field is omitted, never populated with a fabricated value."""
    out = project_fields([{"id": 1, "content": "x"}], ["content", "file_path"])
    assert "file_path" not in out[0]
    assert out[0] == {"id": 1, "content": "x"}


def test_empty_result_set_does_not_mask_a_bad_field_list():
    """Validate BEFORE iterating, so a typo is caught even when there is
    nothing to return -- exactly the case where it would otherwise go unseen."""
    with pytest.raises(ProjectFieldError):
        project_fields([], ["id", "typo"])


def test_empty_input_is_a_no_op():
    assert project_fields([], ["id", "content"]) == []


# --- integration with query_hybrid ---------------------------------------

def test_default_response_is_unchanged(store):
    _seed(store)
    res = store.query_hybrid("widgets", limit=5, log_usage=False)
    assert res
    for r in res:
        assert set(r.keys()) <= KNOWN_RESULT_FIELDS
        assert "rationale" in r          # the verbose default survives


def test_every_live_result_key_is_known():
    """A new result key must be registered here, or valid projections start raising.

    KNOWN_RESULT_FIELDS is hand-maintained, so this is the guard that stops a
    future column addition from silently turning a working projection into a
    ProjectFieldError. Run against every read path that shares the shape:
    query_hybrid (memory + episodic lanes), explain, and locate_fact.
    """
    from pm_common import KNOWN_RESULT_FIELDS as K
    import server
    with server._store() as store:
        seen = set()
        for kwargs in ({}, {"explain": True}):
            for r in store.query_hybrid("episodic deprecation rrf_add filter",
                                        limit=5, log_usage=False, **kwargs):
                seen |= set(r)
        for r in store.query_hybrid("zzzz no match qqqq", limit=5, log_usage=False):
            seen |= set(r)
        located = store.locate_fact(755)
        if isinstance(located, dict):
            for v in located.values():
                if isinstance(v, dict):
                    seen |= set(v)
    unknown = seen - K
    assert not unknown, (
        f"result keys missing from KNOWN_RESULT_FIELDS: {sorted(unknown)}. "
        "Projection onto them would raise ProjectFieldError.")


def test_field_names_are_well_formed():
    """Catches an accidental camelCase or typo'd entry in the hand-maintained set."""
    import re
    for f in KNOWN_RESULT_FIELDS:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", f), f


def test_projection_narrows_the_keys(store):
    _seed(store)
    full = store.query_hybrid("widgets", limit=5, log_usage=False)
    proj = store.query_hybrid("widgets", limit=5, log_usage=False,
                              fields="id,content")
    assert len(full) == len(proj)
    assert set(proj[0]) == {"id", "content"}


def test_projection_does_not_change_ranking(store):
    _seed(store, n=6)
    a = [r["id"] for r in store.query_hybrid("widgets", limit=5, log_usage=False)]
    b = [r["id"] for r in store.query_hybrid("widgets", limit=5, log_usage=False,
                                             fields="id")]
    assert a == b


def test_projection_actually_shrinks_the_payload(store):
    _seed(store, n=8)
    full = store.query_hybrid("widgets", limit=8, log_usage=False)
    proj = store.query_hybrid("widgets", limit=8, log_usage=False,
                              fields="id,category,content")
    assert len(str(proj)) < len(str(full))


def test_projection_composes_with_explain(store):
    """A flag you passed is itself a request for its keys.

    `fields` is a whitelist, so 'id,content' alone would correctly drop the
    explain keys. But explain=True is an explicit request for the trace, and
    having the two flags fight is a trap. The trace survives the narrowing.
    """
    _seed(store, n=4)
    res = store.query_hybrid("widgets", limit=3, log_usage=False,
                             fields="id,content", explain=True)
    assert res
    assert set(res[0]) == {"id", "content", "fused_score", "lanes"}


def test_explain_keys_are_projectable_too(store):
    _seed(store, n=4)
    res = store.query_hybrid("widgets", limit=3, log_usage=False,
                             fields="id", explain=True)
    assert set(res[0]) == {"id", "fused_score", "lanes"}


def test_projection_does_not_invent_explain_keys_when_explain_is_off(store):
    _seed(store, n=4)
    res = store.query_hybrid("widgets", limit=3, log_usage=False,
                             fields="id,fused_score")
    assert set(res[0]) == {"id"}   # absent, not null-filled


def test_bad_field_reaches_the_caller_rather_than_being_swallowed(store):
    _seed(store)
    with pytest.raises(ProjectFieldError):
        store.query_hybrid("widgets", limit=3, log_usage=False,
                           fields="id,bogus")


def test_empty_string_fields_means_full_response(store):
    _seed(store)
    res = store.query_hybrid("widgets", limit=3, log_usage=False, fields="")
    assert "rationale" in res[0]
