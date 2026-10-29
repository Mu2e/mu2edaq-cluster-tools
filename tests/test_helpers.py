"""!
@file test_helpers.py
@brief Tests for ssh-selector pure helpers: resolve_user, display_user, _format_age and _parse_users.

None of these touch the network, the filesystem or Textual.
"""

import os
import getpass

import pytest

from mu2edaq_cluster_tools.ssh_selector import (
    CURRENT_USER,
    _format_age,
    _parse_users,
    display_user,
    resolve_user,
)


# ---------------------------------------------------------------------------
# resolve_user
# ---------------------------------------------------------------------------

class TestResolveUser:
    """!
    @brief resolve_user() maps the `default` token to the current login.
    """
    def test_default_resolves_to_current_user(self):
        """!
        @brief `default` resolves to CURRENT_USER.
        """
        assert resolve_user("default") == CURRENT_USER

    def test_named_user_passes_through(self):
        """!
        @brief A named user is returned unchanged.
        """
        assert resolve_user("alice") == "alice"

    def test_named_user_with_at_sign(self):
        """!
        @brief A user containing `@` is returned unchanged.
        """
        assert resolve_user("alice@domain") == "alice@domain"

    def test_empty_string_passes_through(self):
        # Empty string is not "default", so it is returned as-is
        """!
        @brief An empty string is not `default` and is returned as-is.
        """
        assert resolve_user("") == ""


# ---------------------------------------------------------------------------
# display_user
# ---------------------------------------------------------------------------

class TestDisplayUser:
    """!
    @brief display_user() labels for the user picker.
    """
    def test_default_shows_current_user_with_label(self):
        """!
        @brief `default` shows the current login together with a `default` label.
        """
        result = display_user("default")
        assert CURRENT_USER in result
        assert "default" in result

    def test_named_user_returns_as_is(self):
        """!
        @brief A named user is displayed unchanged.
        """
        assert display_user("alice") == "alice"

    def test_named_user_no_label_appended(self):
        """!
        @brief A named user gets no `default` label.
        """
        result = display_user("bob")
        assert "default" not in result


# ---------------------------------------------------------------------------
# _format_age
# ---------------------------------------------------------------------------

class TestFormatAge:
    """!
    @brief _format_age() renders cache ages as s, m or h mm.
    """
    def test_seconds_below_60(self):
        """!
        @brief Ages below 60 s render as `Ns`.
        """
        assert _format_age(0) == "0s"
        assert _format_age(1) == "1s"
        assert _format_age(59) == "59s"

    def test_minutes(self):
        """!
        @brief Ages below 1 h render as whole minutes, truncated.
        """
        assert _format_age(60) == "1m"
        assert _format_age(90) == "1m"
        assert _format_age(119) == "1m"
        assert _format_age(120) == "2m"
        assert _format_age(3599) == "59m"

    def test_hours_and_minutes(self):
        """!
        @brief Ages of 1 h or more render as `Hh MMm` with zero-padded minutes.
        """
        assert _format_age(3600) == "1h 00m"
        assert _format_age(3661) == "1h 01m"
        assert _format_age(3900) == "1h 05m"
        assert _format_age(7200) == "2h 00m"
        assert _format_age(7384) == "2h 03m"

    def test_float_truncated(self):
        """!
        @brief Fractional seconds are truncated, not rounded.
        """
        assert _format_age(59.9) == "59s"
        assert _format_age(60.0) == "1m"


# ---------------------------------------------------------------------------
# _parse_users
# ---------------------------------------------------------------------------

class TestParseUsers:
    """!
    @brief _parse_users() normalises the YAML users/user field to a list of strings.
    """
    def test_list_of_strings(self):
        """!
        @brief A list of strings is returned as-is.
        """
        assert _parse_users(["alice", "bob"]) == ["alice", "bob"]

    def test_single_string_in_list(self):
        """!
        @brief A one-element list is returned as-is.
        """
        assert _parse_users(["alice"]) == ["alice"]

    def test_scalar_string(self):
        """!
        @brief A scalar string becomes a one-element list.
        """
        assert _parse_users("alice") == ["alice"]

    def test_none_returns_default(self):
        """!
        @brief None yields ['default'].
        """
        assert _parse_users(None) == ["default"]

    def test_empty_list_returns_default(self):
        """!
        @brief An empty list yields ['default'].
        """
        assert _parse_users([]) == ["default"]

    def test_empty_string_returns_default(self):
        """!
        @brief An empty string yields ['default'].
        """
        assert _parse_users("") == ["default"]

    def test_list_coerces_non_strings(self):
        """!
        @brief Non-string list items are converted with str().
        """
        result = _parse_users([1, 2])
        assert result == ["1", "2"]

    def test_scalar_coerces_non_string(self):
        """!
        @brief A non-string scalar is converted with str().
        """
        result = _parse_users(42)
        assert result == ["42"]

    def test_default_token_preserved(self):
        """!
        @brief The `default` token is kept in place, not resolved.
        """
        assert _parse_users(["default", "admin"]) == ["default", "admin"]
