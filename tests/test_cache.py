"""!
@file test_cache.py
@brief Tests for the ssh-selector user-count cache: freshness checks and expiry.

Covers SSHSelector._user_count_is_fresh() and propagation of
Config.cache_lifetime into the app. The selector is constructed but never
run, so no Textual screen or SSH probe is involved.
"""

import time

import pytest

from mu2edaq_cluster_tools.ssh_selector import Config, Host, SSHSelector


# ---------------------------------------------------------------------------
# Fixture: minimal SSHSelector instance (not running)
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """!
    @brief SSHSelector over one host with the default cache_lifetime of 600 s.
    @return A non-running SSHSelector instance.
    """
    cfg = Config(
        hosts=[Host(hostname="node.example.com", nickname="Node")],
        grouplist=[],
        cache_lifetime=600,
    )
    return SSHSelector(cfg)


@pytest.fixture
def short_app():
    """!
    @brief SSHSelector over one host with a 5 s cache_lifetime, for expiry tests.
    @return A non-running SSHSelector instance.
    """
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
    """!
    @brief Freshness of cached `who` counts against the cache lifetime.
    """
    def test_absent_key_not_fresh(self, app):
        """!
        @brief A host with no cached count is not fresh.
        """
        assert app._user_count_is_fresh("node.example.com") is False

    def test_freshly_stored_is_fresh(self, app):
        """!
        @brief A count stored now is fresh.
        """
        app._user_counts["node.example.com"] = (3, time.monotonic())
        assert app._user_count_is_fresh("node.example.com") is True

    def test_unknown_host_not_fresh(self, app):
        """!
        @brief A cached count for one host does not make another host fresh.
        """
        app._user_counts["node.example.com"] = (2, time.monotonic())
        assert app._user_count_is_fresh("other.example.com") is False

    def test_expired_count_not_fresh(self, app):
        # Store a result timestamped 700 seconds ago (lifetime is 600)
        """!
        @brief A count older than the lifetime is stale.
        """
        stale_time = time.monotonic() - 700
        app._user_counts["node.example.com"] = (1, stale_time)
        assert app._user_count_is_fresh("node.example.com") is False

    def test_just_within_lifetime_is_fresh(self, app):
        # 1 second before expiry
        """!
        @brief A count 1 s younger than the lifetime is still fresh.
        """
        recent_time = time.monotonic() - (app._cache_lifetime - 1)
        app._user_counts["node.example.com"] = (0, recent_time)
        assert app._user_count_is_fresh("node.example.com") is True

    def test_exactly_at_expiry_not_fresh(self, app):
        # Exactly at the boundary (>= lifetime) → stale
        """!
        @brief A count exactly at the lifetime boundary is stale (age >= lifetime).
        """
        boundary_time = time.monotonic() - app._cache_lifetime
        app._user_counts["node.example.com"] = (0, boundary_time)
        assert app._user_count_is_fresh("node.example.com") is False

    def test_zero_count_treated_as_valid(self, app):
        # count=0 is a valid result (no users logged in), not a miss
        """!
        @brief A cached count of 0 (nobody logged in) is a hit, not a miss.
        """
        app._user_counts["node.example.com"] = (0, time.monotonic())
        assert app._user_count_is_fresh("node.example.com") is True

    def test_none_count_treated_as_valid(self, app):
        # count=None means "unreachable" — still a cached result, not a miss
        """!
        @brief A cached None (host unreachable) is a hit, not a miss.
        """
        app._user_counts["node.example.com"] = (None, time.monotonic())
        assert app._user_count_is_fresh("node.example.com") is True


# ---------------------------------------------------------------------------
# cache_lifetime propagation from Config
# ---------------------------------------------------------------------------

class TestCacheLifetimePropagation:
    """!
    @brief Config.cache_lifetime reaches SSHSelector._cache_lifetime.
    """
    def test_default_lifetime(self):
        """!
        @brief Without an explicit value the lifetime is 600 s.
        """
        cfg = Config(hosts=[], grouplist=[])
        app = SSHSelector(cfg)
        assert app._cache_lifetime == 600

    def test_custom_lifetime_from_config(self):
        """!
        @brief An explicit cache_lifetime is copied into the app.
        """
        cfg = Config(hosts=[], grouplist=[], cache_lifetime=30)
        app = SSHSelector(cfg)
        assert app._cache_lifetime == 30

    def test_zero_lifetime_always_stale(self):
        """!
        @brief A lifetime of 0 makes every cached count immediately stale.
        """
        cfg = Config(hosts=[], grouplist=[], cache_lifetime=0)
        app = SSHSelector(cfg)
        app._user_counts["node.example.com"] = (1, time.monotonic())
        # With a 0-second lifetime every result is immediately stale
        assert app._user_count_is_fresh("node.example.com") is False
