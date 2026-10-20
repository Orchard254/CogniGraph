import datetime as dt
import threading


def _mk(tmp_path):
    from memory_store import MemoryStore
    return MemoryStore(str(tmp_path / "t.db"))


def test_expired_lease_released_and_reacquired(tmp_path):
    s = _mk(tmp_path)
    try:
        # Lease expired in the past cannot hold the resource
        assert s.acquire_lock("r", "agent-a", 1) is True
        # Force expiry by waiting? Instead manually expire
        s.conn.execute("UPDATE memory_locks SET expires_at = ? WHERE resource = ?", ("2000-01-01T00:00:00+00:00", "r"))
        s.conn.commit()
        assert s.lock_status("r") is None
        assert s.acquire_lock("r", "agent-b", 60) is True
        assert s.validate_lease("r", "agent-b") is True
    finally:
        s.close()


def test_release_requires_owner(tmp_path):
    s = _mk(tmp_path)
    try:
        assert s.acquire_lock("r", "owner", 60) is True
        assert s.release_lock("r", "outsider") is False
        assert s.validate_lease("r", "owner") is True
        assert s.release_lock("r", "owner") is True
        assert s.lock_status("r") is None
    finally:
        s.close()


def test_acquire_rejects_empty_inputs(tmp_path):
    s = _mk(tmp_path)
    try:
        assert s.acquire_lock("", "agent-a", 60) is False
        assert s.acquire_lock("r", "", 60) is False
    finally:
        s.close()


def test_concurrent_acquire_only_one_wins(tmp_path):
    s = _mk(tmp_path)
    try:
        s.close()
    except Exception:
        pass

    store_path = str(tmp_path / "c.db")
    from memory_store import MemoryStore
    # Prepare a fresh store to set schema
    prep = MemoryStore(store_path)
    prep.close()

    results = []
    barrier = threading.Barrier(2)

    def worker(owner):
        store = MemoryStore(store_path)
        try:
            barrier.wait()
            results.append(store.acquire_lock("r", owner, 60))
            store.close()
        except Exception as e:
            results.append(("err", str(e)))

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results.count(True) == 1 and results.count(False) == 1, results