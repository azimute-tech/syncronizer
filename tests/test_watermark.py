"""Regression tests for numeric-aware watermark comparison (integer/sequence
incremental columns must not freeze or regress when round-tripped through TEXT).
"""
from syncronizer.core.orchestrator import _watermark_max


def test_integer_watermark_advances_through_text_roundtrip():
    # watermark stored as TEXT '9', next extracted row has int id 10
    assert _watermark_max("9", 10) == 10
    assert _watermark_max("99", 100) == 100


def test_integer_watermark_does_not_regress():
    assert _watermark_max("10", 9) == "10"  # keeps the larger, no lexicographic regression


def test_none_handling():
    assert _watermark_max(None, 5) == 5
    assert _watermark_max(5, None) == 5


def test_timestamp_string_watermark():
    a = "2020-01-01T10:00:00"
    b = "2020-01-01T11:00:00"
    assert _watermark_max(a, b) == b
    assert _watermark_max(b, a) == b
