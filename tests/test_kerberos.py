"""!
@file test_kerberos.py
@brief Tests for ssh-selector Kerberos `klist` parsing: _parse_klist_date and _format_klist.

Uses the canned macOS Heimdal and Linux MIT klist outputs from conftest.py.
`datetime.now()` is pinned so the green/red expiry colouring is deterministic.
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from mu2edaq_cluster_tools.ssh_selector import _format_klist, _parse_klist_date
from tests.conftest import KLIST_EMPTY, KLIST_EXPIRED, KLIST_LINUX, KLIST_MACOS


# ---------------------------------------------------------------------------
# _parse_klist_date
# ---------------------------------------------------------------------------

class TestParseKlistDate:
    """!
    @brief _parse_klist_date() accepts both Heimdal and MIT date formats.
    """
    def test_macos_format(self):
        # macOS Heimdal: "Apr  1 10:00:00 2026"  (double space before single-digit day)
        """!
        @brief Heimdal `Mon  D HH:MM:SS YYYY` parses to the right date.
        """
        result = _parse_klist_date("Apr  1 10:00:00 2026")
        assert isinstance(result, datetime)
        assert result.month == 4
        assert result.day == 1
        assert result.year == 2026

    def test_macos_format_double_digit_day(self):
        """!
        @brief Heimdal dates with a two-digit day parse.
        """
        result = _parse_klist_date("Apr 15 10:00:00 2026")
        assert result is not None
        assert result.day == 15

    def test_linux_format(self):
        # Linux MIT: "04/01/2026 10:00:00"
        """!
        @brief MIT `MM/DD/YYYY HH:MM:SS` parses to the right date.
        """
        result = _parse_klist_date("04/01/2026 10:00:00")
        assert isinstance(result, datetime)
        assert result.month == 4
        assert result.day == 1
        assert result.year == 2026

    def test_whitespace_normalised_before_parse(self):
        # Extra internal spaces (e.g. "Apr  1") should be handled
        """!
        @brief Repeated internal spaces are collapsed before parsing.
        """
        result = _parse_klist_date("Apr  1 08:30:00 2026")
        assert result is not None

    def test_invalid_string_returns_none(self):
        """!
        @brief Unparseable text yields None.
        """
        assert _parse_klist_date("not a date") is None

    def test_empty_string_returns_none(self):
        """!
        @brief An empty string yields None.
        """
        assert _parse_klist_date("") is None

    def test_time_components_correct(self):
        """!
        @brief Hour, minute and second are preserved.
        """
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
    """!
    @brief _format_klist() Rich markup: principal header, ticket rows and expiry colour.
    """
    def _fmt(self, klist_output: str) -> str:
        """!
        @brief Format klist output with datetime.now() pinned to _FIXED_NOW.

        ssh_selector.datetime is patched as a whole, so strptime is re-pointed at the
        real implementation to keep date parsing working.

        @param klist_output  Raw klist text.
        @return Rich-markup string.
        """
        # Patch the module's datetime class to pin now(); strptime must still be the real one.
        with patch("mu2edaq_cluster_tools.ssh_selector.datetime") as mock_dt:
            mock_dt.now.return_value = _FIXED_NOW
            mock_dt.strptime.side_effect = datetime.strptime
            return _format_klist(klist_output)

    # --- principal extraction ---

    def test_macos_principal_extracted(self):
        """!
        @brief The Heimdal `Principal:` line is extracted.
        """
        result = self._fmt(KLIST_MACOS)
        assert "testuser@EXAMPLE.ORG" in result

    def test_linux_principal_extracted(self):
        """!
        @brief The MIT `Default principal:` line is extracted.
        """
        result = self._fmt(KLIST_LINUX)
        assert "testuser@EXAMPLE.ORG" in result

    # --- ticket lines present ---

    def test_macos_krbtgt_ticket_shown(self):
        """!
        @brief The krbtgt ticket row appears for Heimdal output.
        """
        result = self._fmt(KLIST_MACOS)
        assert "krbtgt/EXAMPLE.ORG@EXAMPLE.ORG" in result

    def test_linux_krbtgt_ticket_shown(self):
        """!
        @brief The krbtgt ticket row appears for MIT output.
        """
        result = self._fmt(KLIST_LINUX)
        assert "krbtgt/EXAMPLE.ORG@EXAMPLE.ORG" in result

    # --- colour coding ---

    def test_future_ticket_marked_green(self):
        # Expires Apr 2; now is Apr 1 noon → ~24 h remaining → green
        """!
        @brief A ticket with about 24 h left is coloured green.
        """
        result = self._fmt(KLIST_MACOS)
        assert "[green]" in result

    def test_expired_ticket_marked_red(self):
        # KLIST_EXPIRED has a ticket that expired in 2020
        """!
        @brief An expired ticket is coloured red and labelled EXPIRED.
        """
        result = self._fmt(KLIST_EXPIRED)
        assert "[red]" in result
        assert "EXPIRED" in result

    # --- empty / no tickets ---

    def test_empty_output_no_tickets_message(self):
        """!
        @brief Empty klist output reports `No tickets found`.
        """
        result = self._fmt(KLIST_EMPTY)
        assert "No tickets found" in result

    # --- principal line not misread as ticket ---

    def test_principal_line_not_counted_as_ticket(self):
        """!
        @brief The principal line is rendered as a header, not a ticket row.
        """
        result = self._fmt(KLIST_MACOS)
        # Principal line should be a header, not a coloured ticket row
        assert "Principal:" in result
