from decimal import Decimal

from syncronizer.core.types import normalize


def test_rstrip_char_padding():
    assert normalize("abc   ") == "abc"


def test_does_not_strip_interior_or_leading():
    assert normalize("  a b ") == "  a b"


def test_none_passthrough():
    assert normalize(None) is None


def test_bytes_decoded_as_text():
    assert normalize(b"hi") == "hi"
    assert normalize(bytearray(b"x")) == "x"


def test_numbers_pass_through():
    assert normalize(Decimal("1.50")) == Decimal("1.50")
    assert normalize(5) == 5
    assert normalize(2.5) == 2.5
