import threading

import pytest

import websessions
from websessions import SessionStore


def test_create_returns_unique_tokens_and_stores_only_digests():
    store = SessionStore(clock=lambda: 100.0)
    first, first_expiry = store.create()
    second, second_expiry = store.create()

    assert first != second
    assert first_expiry == second_expiry == 43_300.0
    assert first not in repr(store._sessions)
    assert second not in repr(store._sessions)
    assert store.expiry_for(first) == 43_300.0


def test_create_retries_collisions_without_altering_the_existing_session(
    monkeypatch,
):
    candidates = iter(
        ["repeated-token", "repeated-token", "repeated-token", "unique-token"]
    )
    monkeypatch.setattr(
        websessions.secrets, "token_urlsafe", lambda _size: next(candidates)
    )
    now = [10.0]
    store = SessionStore(ttl_seconds=5, max_sessions=2, clock=lambda: now[0])
    first, first_expiry = store.create()
    now[0] = 11.0

    second, second_expiry = store.create()

    assert first == "repeated-token"
    assert second == "unique-token"
    assert first != second
    assert store.expiry_for(first) == first_expiry == 15.0
    assert store.expiry_for(second) == second_expiry == 16.0


def test_expired_and_revoked_sessions_are_rejected():
    now = [10.0]
    store = SessionStore(ttl_seconds=5, clock=lambda: now[0])
    token, _ = store.create()

    assert store.revoke(token)
    assert store.expiry_for(token) is None

    token, _ = store.create()
    now[0] = 16.0
    assert store.expiry_for(token) is None


def test_revoke_is_idempotent_only_until_the_original_session_expiry():
    now = [10.0]
    store = SessionStore(ttl_seconds=5, clock=lambda: now[0])
    token, expiry = store.create()

    assert store.revoke(token) is True
    assert store.revoke(token) is True
    assert store.expiry_for(token) is None
    assert store.revoke("never-valid") is False

    now[0] = expiry
    assert store.revoke(token) is False


def test_revocation_tombstones_are_digest_only_bounded_and_do_not_reduce_capacity():
    now = [0.0]
    store = SessionStore(ttl_seconds=10, max_sessions=2, clock=lambda: now[0])
    revoked = []
    for _ in range(3):
        token, _ = store.create()
        revoked.append(token)
        assert store.revoke(token) is True
        now[0] += 1.0

    tombstones = getattr(store, "_revoked", {})
    assert len(tombstones) == 2
    assert all(token not in repr(tombstones) for token in revoked)
    assert store.revoke(revoked[0]) is False

    first_active, first_expiry = store.create()
    now[0] += 1.0
    second_active, second_expiry = store.create()
    assert store.expiry_for(first_active) == first_expiry
    assert store.expiry_for(second_active) == second_expiry


def test_create_does_not_reissue_an_unexpired_revoked_token(monkeypatch):
    candidates = iter(["revoked-token", "revoked-token", "fresh-token"])
    monkeypatch.setattr(
        websessions.secrets, "token_urlsafe", lambda _size: next(candidates)
    )
    store = SessionStore(clock=lambda: 0.0)
    revoked, _ = store.create()
    assert store.revoke(revoked) is True

    current, _ = store.create()

    assert revoked == "revoked-token"
    assert current == "fresh-token"
    assert store.revoke(revoked) is True
    assert store.expiry_for(current) is not None


def test_capacity_evicts_the_soonest_expiring_session():
    now = [0.0]
    store = SessionStore(ttl_seconds=10, max_sessions=2, clock=lambda: now[0])
    first, _ = store.create()
    now[0] = 1.0
    second, _ = store.create()
    now[0] = 2.0
    third, third_expiry = store.create()

    assert store.expiry_for(first) is None
    assert store.expiry_for(second) == 11.0
    assert store.expiry_for(third) == third_expiry == 12.0


def test_create_prunes_expired_sessions_before_enforcing_capacity():
    now = [0.0]
    store = SessionStore(ttl_seconds=5, max_sessions=1, clock=lambda: now[0])
    expired, _ = store.create()
    now[0] = 5.0
    current, current_expiry = store.create()

    assert store.expiry_for(expired) is None
    assert store.expiry_for(current) == current_expiry == 10.0


def test_unknown_and_altered_tokens_are_rejected_without_affecting_valid_session():
    store = SessionStore(clock=lambda: 0.0)
    token, expiry = store.create()
    altered = token[:-1] + ("A" if token[-1] != "A" else "B")

    assert store.expiry_for("unknown") is None
    assert store.revoke(altered) is False
    assert store.expiry_for(token) == expiry


def test_clear_revokes_all_sessions():
    store = SessionStore(clock=lambda: 0.0)
    first, _ = store.create()
    second, _ = store.create()
    assert store.revoke(first) is True

    store.clear()

    assert store.expiry_for(first) is None
    assert store.expiry_for(second) is None
    assert store.revoke(first) is False
    assert store.revoke(second) is False


def test_session_limits_must_be_positive():
    with pytest.raises(ValueError, match="session limits must be positive"):
        SessionStore(ttl_seconds=0)
    with pytest.raises(ValueError, match="session limits must be positive"):
        SessionStore(max_sessions=0)


def test_concurrent_create_validate_and_revoke_loops_leave_no_sessions():
    store = SessionStore(max_sessions=128)
    barrier = threading.Barrier(8)
    failures = []

    def worker():
        try:
            barrier.wait()
            for _ in range(100):
                token, expiry = store.create()
                assert store.expiry_for(token) == expiry
                assert store.revoke(token) is True
                assert store.expiry_for(token) is None
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert store._sessions == {}
    assert len(getattr(store, "_revoked", {})) <= store._max_sessions
