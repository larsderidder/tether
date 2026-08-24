"""Tests for bridge output policy command helpers."""

import pytest

from tether.bridges.output_policy_api import parse_buffer_arg, parse_verbosity_arg


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_parse_buffer_rejects_non_finite_values(value: str) -> None:
    """Buffer commands accept only finite durations."""
    seconds, error, clear = parse_buffer_arg(value)

    assert seconds is None
    assert error is not None
    assert clear is False


def test_parse_buffer_off_clears_session_override() -> None:
    """The off command restores global buffer policy."""
    assert parse_buffer_arg("off") == (None, None, True)


def test_parse_verbosity_accepts_known_level() -> None:
    """Known verbosity levels are normalized for the API."""
    assert parse_verbosity_arg(" Medium ") == ("medium", None, False)
