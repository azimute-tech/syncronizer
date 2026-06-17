from datetime import datetime
from decimal import Decimal

from syncronizer.core.hashing import canonical_json, row_hash


def test_key_order_invariant():
    assert row_hash({"a": 1, "b": 2}) == row_hash({"b": 2, "a": 1})


def test_value_change_changes_hash():
    assert row_hash({"a": 1}) != row_hash({"a": 2})


def test_decimal_datetime_deterministic():
    rec = {"preco": Decimal("1.50"), "dt": datetime(2020, 1, 1, 12, 0, 0)}
    assert row_hash(rec) == row_hash(dict(rec))
    assert canonical_json(rec) == canonical_json(dict(rec))


def test_decimal_serialized_as_string():
    assert canonical_json({"p": Decimal("1.50")}) == '{"p":"1.50"}'


def test_none_serialized_as_null():
    assert canonical_json({"x": None}) == '{"x":null}'
