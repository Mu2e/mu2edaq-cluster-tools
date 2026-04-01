"""Tests for the user-count cache: freshness checks and expiry."""

import time

import pytest

from ssh_selector import Config, Host, SSHSelector


# ---------------------------------------------------------------------------
# Fixture: minimal SSHSelector instance (not running)
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """Return an SSHSelector instance with default cache_lifetime=600."""
    cfg = Config(
        hosts=[Host(hostname="node.example.com", nickname="Node")],
        grouplist=[],
        cache_lifetime=600,
    )
    return SSHSelector(cfg)


@pytest.fixture
def short_app():
    """SSHSelector with a very short cache_lifetime for expiry tests."""
    cfg = Config(
        hosts=[Host(hostname="node.example.com", nickname="Node")],
        grouplist=[],
        cache_lifetime=5,
    )
    return SSHSelector(cfg)


# ---------------------------------------------------------------------------
# _user_count_is_fresh
# ---------------------------------------------------------------------------

class TestUserCountIsFresh:
    def test_absent_key_not_fresh(self, app):
        assert app._user_count_is_fresh("node.example.com") is False

    def test_freshly_stored_is_fresh(self, app):
        app._user_counts["node.example.com"] = (3, time.monotonic())
        assert app._user_count_is_fresh("node.example.com") is True

    def test_unknown_host_not_fresh(self, app):
        app._user_counts["node.example.com"] = (2, time.monotonic())
        assert app._user_count_is_fresh("other.example.com") is False

    def test_expired_count_not_fresh(self, app):
        # Store a result timestamped 700 seconds ago (lifetime is 600)
        stale_time = time.monotonic() - 700
        app._user_counts["node.example.com"] = (1, stale_time)
        assert app._user_count_is_fresh("node.example.com") is False

    def test_just_within_lifetime_is_fresh(self, app):
        # 1 second before expiry
        recent_time = time.monotonic() - (app._cache_lifetime - 1)
        app._user_counts["node.example.com"] = (0, recent_time)
        assert app._user_count_is_fresh("node.example.com") is True

    def test_exactly_at_expiry_not_fresh(self, app):
        # Exactly at the boundary (>= lifetime) → stale
        boundary_time = time.monotonic() - app._cache_lifetime
        app._user_counts["node.example.com"] = (0, boundary_time)
        assert app._user_count_is_fresh("node.example.com") is False

    def test_zero_count_treated_as_valid(self, app):
        # count=0 is a valid result (no users logged in), not a miss
        app._user_counts["node.example.com"] = (0, time.monotonic())
        assert app._user_count_is_fresh("node.example.com") is True

    def test_none_count_treated_as_valid(self, app):
        # count=None means "unreachable" — still a cached result, not a miss
        app._user_counts["node.example.com"] = (None, time.monotonic())
        assert app._user_count_is_fresh("node.example.com") is True


# ---------------------------------------------------------------------------
# cache_lifetime propagation from Config
# ---------------------------------------------------------------------------

class TestCacheLifetimePropagation:
    def test_default_lifetime(self):
        cfg = Config(hosts=[], grouplist=[])
        app = SSHSelector(cfg)
        assert app._cache_lifetime == 600

    def test_custom_lifetime_from_config(self):
        cfg = Config(hosts=[], grouplist=[], cache_lifetime=30)
        app = SSHSelector(cfg)
        assert app._cache_lifetime == 30

    def test_zero_lifetime_always_stale(self):
        cfg = Config(hosts=[], grouplist=[], cache_lifetime=0)
        app = SSHSelector(cfg)
        app._user_counts["node.example.com"] = (1, time.monotonic())
        # With a 0-second lifetime every result is immediately stale
        assert app._user_count_is_fresh("node.example.com") is False
