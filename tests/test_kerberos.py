"""Tests for Kerberos klist parsing helpers."""

from datetime import datetime
from unittest.mock import patch

import pytest

from ssh_selector import _format_klist, _parse_klist_date
from tests.conftest import KLIST_EMPTY, KLIST_EXPIRED, KLIST_LINUX, KLIST_MACOS


# ---------------------------------------------------------------------------
# _parse_klist_date
# ---------------------------------------------------------------------------

class TestParseKlistDate:
    def test_macos_format(self):
        # macOS Heimdal: "Apr  1 10:00:00 2026"  (double space before single-digit day)
        result = _parse_klist_date("Apr  1 10:00:00 2026")
        assert isinstance(result, datetime)
        assert result.month == 4
        assert result.day == 1
        assert result.year == 2026

    def test_macos_format_double_digit_day(self):
        result = _parse_klist_date("Apr 15 10:00:00 2026")
        assert result is not None
        assert result.day == 15

    def test_linux_format(self):
        # Linux MIT: "04/01/2026 10:00:00"
        result = _parse_klist_date("04/01/2026 10:00:00")
        assert isinstance(result, datetime)
        assert result.month == 4
        assert result.day == 1
        assert result.year == 2026

    def test_whitespace_normalised_before_parse(self):
        # Extra internal spaces (e.g. "Apr  1") should be handled
        result = _parse_klist_date("Apr  1 08:30:00 2026")
        assert result is not None

    def test_invalid_string_returns_none(self):
        assert _parse_klist_date("not a date") is None

    def test_empty_string_returns_none(self):
        assert _parse_klist_date("") is None

    def test_time_components_correct(self):
        result = _parse_klist_date("04/01/2026 14:30:45")
        assert result.hour == 14
        assert result.minute == 30
        assert result.second == 45


# ---------------------------------------------------------------------------
# _format_klist
# ---------------------------------------------------------------------------

# Pin "now" to a fixed point so expiry colour is deterministic.
_FIXED_NOW = datetime(2026, 4, 1, 12, 0, 0)   # noon; tickets expire Apr 2 → ~24 h left


class TestFormatKlist:
    def _fmt(self, klist_output: str) -> str:
        with patch("ssh_selector.datetime") as mock_dt:
            mock_dt.now.return_value = _FIXED_NOW
            mock_dt.strptime.side_effect = datetime.strptime
            return _format_klist(klist_output)

    # --- principal extraction ---

    def test_macos_principal_extracted(self):
        result = self._fmt(KLIST_MACOS)
        assert "testuser@EXAMPLE.ORG" in result

    def test_linux_principal_extracted(self):
        result = self._fmt(KLIST_LINUX)
        assert "testuser@EXAMPLE.ORG" in result

    # --- ticket lines present ---

    def test_macos_krbtgt_ticket_shown(self):
        result = self._fmt(KLIST_MACOS)
        assert "krbtgt/EXAMPLE.ORG@EXAMPLE.ORG" in result

    def test_linux_krbtgt_ticket_shown(self):
        result = self._fmt(KLIST_LINUX)
        assert "krbtgt/EXAMPLE.ORG@EXAMPLE.ORG" in result

    # --- colour coding ---

    def test_future_ticket_marked_green(self):
        # Expires Apr 2; now is Apr 1 noon → ~24 h remaining → green
        result = self._fmt(KLIST_MACOS)
        assert "[green]" in result

    def test_expired_ticket_marked_red(self):
        # KLIST_EXPIRED has a ticket that expired in 2020
        result = self._fmt(KLIST_EXPIRED)
        assert "[red]" in result
        assert "EXPIRED" in result

    # --- empty / no tickets ---

    def test_empty_output_no_tickets_message(self):
        result = self._fmt(KLIST_EMPTY)
        assert "No tickets found" in result

    # --- principal line not misread as ticket ---

    def test_principal_line_not_counted_as_ticket(self):
        result = self._fmt(KLIST_MACOS)
        # Principal line should be a header, not a coloured ticket row
        assert "Principal:" in result
