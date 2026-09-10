"""Tests for the injectable "now" in recency weighting (Phase 6).

_recency_weight used to age facts against the wall clock, so golden-set
ranks drifted as facts aged. The reference instant is now injectable via
an explicit parameter or the PROJECT_MEMORY_NOW env var (parameter wins),
defaulting to real time, so eval numbers reproduce exactly with the clock
pinned.
"""
import datetime
import types

import pytest

import pm_search
from memory_store import MemoryStore
from pm_search import PINNED_NOW_ENV, SearchMixin, _parse_iso_instant, pinned_now

UTC = datetime.timezone.utc
PINNED = datetime.datetime(2026, 9, 11, tzinfo=UTC)


class _Stub:
    """Bare stand-in for self: _recency_weight reads no instance state."""


def _weight(created_at, access_count=0, now=None):
    return SearchMixin._recency_weight(_Stub(), created_at, access_count, now=now)


def test_parse_iso_instant_variants():
    assert _parse_iso_instant("2026-09-11") == PINNED
    assert _parse_iso_instant("2026-09-11T00:00:00Z") == PINNED
    assert _parse_iso_instant("2026-09-11T06:00:00+06:00") == PINNED
    assert _parse_iso_instant(None) is None
    assert _parse_iso_instant("") is None
    assert _parse_iso_instant("not-a-date") is None


def test_recency_weight_honors_injected_now():
    created = "2026-09-01T00:00:00+00:00"  # 10 days before PINNED
    assert _weight(created, 0, now=PINNED) == pytest.approx(1.3 - (10 / 180.0) * 0.6)
    assert _weight("2026-09-11T00:00:00+00:00", 0, now=PINNED) == pytest.approx(1.3)
    assert _weight("2026-01-01T00:00:00+00:00", 0, now=PINNED) == pytest.approx(0.7)  # >180d
    assert _weight("2026-12-31T00:00:00+00:00", 0, now=PINNED) == pytest.approx(1.3)  # future
    assert _weight(None, 0, now=PINNED) == pytest.approx(0.7)  # missing -> max age
    # Access boost still applies on top of the pinned clock.
    assert _weight(created, 2, now=PINNED) == pytest.approx(1.3 - (10 / 180.0) * 0.6 + 0.02)
    # Result clamps at 1.3 even with a large access boost.
    assert _weight("2026-09-11T00:00:00+00:00", 20, now=PINNED) == pytest.approx(1.3)


def test_recency_weight_env_pin_matches_parameter(monkeypatch):
    monkeypatch.setenv(PINNED_NOW_ENV, "2026-09-11")
    assert pinned_now() == PINNED
    created = "2026-06-13T00:00:00+00:00"  # 90 days before PINNED
    assert _weight(created) == _weight(created, now=PINNED)
    # Explicit parameter wins over the env var.
    other = datetime.datetime(2026, 6, 13, tzinfo=UTC)
    assert _weight(created, now=other) == pytest.approx(1.3)


def test_recency_weight_default_uses_wall_clock(monkeypatch):
    monkeypatch.delenv(PINNED_NOW_ENV, raising=False)

    class _FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return PINNED.astimezone(tz) if tz else PINNED

    fake = types.SimpleNamespace(datetime=_FixedDatetime, timezone=datetime.timezone)
    monkeypatch.setattr(pm_search, "datetime", fake)
    created = "2026-06-13T00:00:00+00:00"
    assert _weight(created) == _weight(created, now=PINNED)


def test_pinned_clock_freezes_recency_drift():
    created = "2026-08-12T00:00:00+00:00"
    younger = _weight(created, now=datetime.datetime(2026, 8, 12, tzinfo=UTC))
    older = _weight(created, now=PINNED)
    assert younger > older  # the drift the pin eliminates, reproduced on demand
    assert _weight(created, now=PINNED) == older  # pinned value is exact


def test_query_hybrid_deterministic_under_pinned_clock(tmp_path, monkeypatch):
    monkeypatch.setenv(PINNED_NOW_ENV, "2026-09-11")
    store = MemoryStore(str(tmp_path / "memory.db"))
    try:
        store.write(category="component",
                    content="RRF fusion constant tuned via the golden sweep",
                    confidence="confirmed")
        store.write(category="decision",
                    content="eval gate blocks merges on retrieval regression",
                    confidence="confirmed")
        store.write(category="constraint",
                    content="fixture embeddings precomputed so CI never downloads models",
                    confidence="confirmed")
        runs = [
            [(r.get("source"), r["id"]) for r in
             store.query_hybrid("golden sweep fusion constant", limit=5, log_usage=False)]
            for _ in range(2)
        ]
        assert runs[0] and runs[0] == runs[1]
    finally:
        store.close()
