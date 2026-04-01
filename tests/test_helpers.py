"""Tests for pure helper functions: resolve_user, display_user, _format_age, _parse_users."""

import os
import getpass

import pytest

from ssh_selector import (
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
    def test_default_resolves_to_current_user(self):
        assert resolve_user("default") == CURRENT_USER

    def test_named_user_passes_through(self):
        assert resolve_user("alice") == "alice"

    def test_named_user_with_at_sign(self):
        assert resolve_user("alice@domain") == "alice@domain"

    def test_empty_string_passes_through(self):
        # Empty string is not "default", so it is returned as-is
        assert resolve_user("") == ""


# ---------------------------------------------------------------------------
# display_user
# ---------------------------------------------------------------------------

class TestDisplayUser:
    def test_default_shows_current_user_with_label(self):
        result = display_user("default")
        assert CURRENT_USER in result
        assert "default" in result

    def test_named_user_returns_as_is(self):
        assert display_user("alice") == "alice"

    def test_named_user_no_label_appended(self):
        result = display_user("bob")
        assert "default" not in result


# ---------------------------------------------------------------------------
# _format_age
# ---------------------------------------------------------------------------

class TestFormatAge:
    def test_seconds_below_60(self):
        assert _format_age(0) == "0s"
        assert _format_age(1) == "1s"
        assert _format_age(59) == "59s"

    def test_minutes(self):
        assert _format_age(60) == "1m"
        assert _format_age(90) == "1m"
        assert _format_age(119) == "1m"
        assert _format_age(120) == "2m"
        assert _format_age(3599) == "59m"

    def test_hours_and_minutes(self):
        assert _format_age(3600) == "1h 00m"
        assert _format_age(3661) == "1h 01m"
        assert _format_age(3900) == "1h 05m"
        assert _format_age(7200) == "2h 00m"
        assert _format_age(7384) == "2h 03m"

    def test_float_truncated(self):
        assert _format_age(59.9) == "59s"
        assert _format_age(60.0) == "1m"


# ---------------------------------------------------------------------------
# _parse_users
# ---------------------------------------------------------------------------

class TestParseUsers:
    def test_list_of_strings(self):
        assert _parse_users(["alice", "bob"]) == ["alice", "bob"]

    def test_single_string_in_list(self):
        assert _parse_users(["alice"]) == ["alice"]

    def test_scalar_string(self):
        assert _parse_users("alice") == ["alice"]

    def test_none_returns_default(self):
        assert _parse_users(None) == ["default"]

    def test_empty_list_returns_default(self):
        assert _parse_users([]) == ["default"]

    def test_empty_string_returns_default(self):
        assert _parse_users("") == ["default"]

    def test_list_coerces_non_strings(self):
        result = _parse_users([1, 2])
        assert result == ["1", "2"]

    def test_scalar_coerces_non_string(self):
        result = _parse_users(42)
        assert result == ["42"]

    def test_default_token_preserved(self):
        assert _parse_users(["default", "admin"]) == ["default", "admin"]
