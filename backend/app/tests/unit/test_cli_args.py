"""Numeric-list CLI arguments.

Written after a one-shot production command died with a raw traceback:

    ValueError: could not convert string to float: '30 120'

PowerShell treats an unquoted `30,120` as ARRAY syntax and joins it back
into a single space-separated token before handing it to a native command,
so `--backoff-seconds 30,120` arrived as the one string `"30 120"`. The
parser split on commas only, found none, and handed `float()` something it
could not read.

A command an operator runs once, against frozen research rules, should not
turn a shell quoting rule into a stack trace.
"""

from __future__ import annotations

import argparse

import pytest

from app.cli_args import int_list, number_list


@pytest.mark.parametrize(
    "raw",
    [
        "30,120",      # bash / sh
        "30 120",      # what PowerShell actually delivers
        "30, 120",     # a human adding a space after the comma
        " 30 , 120 ",  # both, plus stray padding
        "30,,120",     # an empty field between separators
    ],
)
def test_every_plausible_separator_parses_to_the_same_list(raw):
    assert number_list(raw) == [30.0, 120.0]


def test_a_single_value_needs_no_separator():
    assert number_list("30") == [30.0]


@pytest.mark.parametrize("raw", ["", "   ", ",", " , "])
def test_an_empty_list_is_refused(raw):
    with pytest.raises(argparse.ArgumentTypeError, match="no values"):
        number_list(raw)


def test_a_non_number_names_the_offending_token_and_the_whole_input():
    """The original failure printed only `'30 120'` from deep inside a list
    comprehension. The message should say which argument, which token, and
    what to do about it."""

    with pytest.raises(argparse.ArgumentTypeError) as exc:
        number_list("30,abc", field="--backoff-seconds")
    message = str(exc.value)
    assert "--backoff-seconds" in message
    assert "'abc'" in message
    assert "'30,abc'" in message
    assert "PowerShell" in message


@pytest.mark.parametrize("raw", ["-1", "30,-5"])
def test_negative_delays_are_refused(raw):
    with pytest.raises(argparse.ArgumentTypeError, match=">= 0"):
        number_list(raw)


@pytest.mark.parametrize("raw", ["inf", "nan", "30,inf"])
def test_non_finite_delays_are_refused(raw):
    with pytest.raises(argparse.ArgumentTypeError):
        number_list(raw)


def test_int_list_refuses_a_fraction_rather_than_truncating():
    """`int(3.7)` is 3. Silently accepting that in a frozen rule would
    change behaviour without changing what anyone typed."""

    with pytest.raises(argparse.ArgumentTypeError, match="not an integer"):
        int_list("60,3.7")


def test_the_amendment_cli_accepts_the_powershell_form():
    """End to end through the real parser, both ways."""

    from app.services.amend_capture_policy import _backoff

    assert _backoff("30,120") == _backoff("30 120") == [30.0, 120.0]


def test_the_calibration_cli_always_scores_the_ungated_baseline():
    from app.forecast_lab.calibrate_cli import _candidates

    assert _candidates("900 3600") == [None, 900, 3600]
    assert _candidates("900,3600") == [None, 900, 3600]
