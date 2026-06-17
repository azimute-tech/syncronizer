import json

import pytest

from syncronizer.db.store import ControlStore


def make_store(tmp_path):
    return ControlStore(tmp_path / "control.db")


def test_wal_pragma_applied(tmp_path):
    s = make_store(tmp_path)
    mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    s.close()


def test_upsert_new_changed_unchanged_and_sent_lifecycle(tmp_path):
    s = make_store(tmp_path)
    s.ensure_endpoint_table("produtos")

    counts = s.upsert_many("produtos", [("1", json.dumps({"id": "1", "v": 1}), "hashA")])
    assert counts == {"inserted": 1, "changed": 0, "unchanged": 0}

    # send it
    rows = s.select_unsent("produtos", 10)
    assert [r["pk"] for r in rows] == ["1"]
    s.mark_sent("produtos", ["1"])
    assert s.select_unsent("produtos", 10) == []

    # identical data -> no-op, sent flag preserved
    counts = s.upsert_many("produtos", [("1", json.dumps({"id": "1", "v": 1}), "hashA")])
    assert counts == {"inserted": 0, "changed": 0, "unchanged": 1}
    assert s.select_unsent("produtos", 10) == []

    # changed data -> sent reset to 0
    counts = s.upsert_many("produtos", [("1", json.dumps({"id": "1", "v": 2}), "hashB")])
    assert counts == {"inserted": 0, "changed": 1, "unchanged": 0}
    assert [r["pk"] for r in s.select_unsent("produtos", 10)] == ["1"]
    s.close()


def test_mark_failed_keeps_unsent_and_records_error(tmp_path):
    s = make_store(tmp_path)
    s.ensure_endpoint_table("e")
    s.upsert_many("e", [("1", "{}", "h")])
    s.mark_failed("e", "1", "boom")
    assert len(s.select_unsent("e", 10)) == 1
    attempts, last_error = s.conn.execute(
        "SELECT send_attempts, last_error FROM ep_e WHERE pk='1'"
    ).fetchone()
    assert attempts == 1 and last_error == "boom"
    s.close()


def test_reconcile_marks_missing_deleted(tmp_path):
    s = make_store(tmp_path)
    s.ensure_endpoint_table("e")
    s.upsert_many("e", [("1", "{}", "h1"), ("2", "{}", "h2")])
    s.mark_sent("e", ["1", "2"])

    n = s.mark_missing_deleted("e", ["1"])  # only pk 1 still present
    assert n == 1
    rows = s.select_unsent("e", 10)
    assert len(rows) == 1
    assert rows[0]["pk"] == "2" and rows[0]["deleted"] is True
    s.close()


def test_resurrected_row_resets_sent(tmp_path):
    s = make_store(tmp_path)
    s.ensure_endpoint_table("e")
    s.upsert_many("e", [("1", "{}", "h")])
    s.mark_sent("e", ["1"])
    s.mark_missing_deleted("e", [])  # tombstone everything (pk 1 absent)
    # same hash but row was deleted -> treated as changed (resurrected), sent reset
    counts = s.upsert_many("e", [("1", "{}", "h")])
    assert counts["changed"] == 1
    rows = s.select_unsent("e", 10)
    assert rows[0]["pk"] == "1" and rows[0]["deleted"] is False
    s.close()


def test_watermark_roundtrip(tmp_path):
    s = make_store(tmp_path)
    assert s.get_watermark("e") is None
    s.set_watermark("e", "2020-01-01T00:00:00")
    assert s.get_watermark("e") == "2020-01-01T00:00:00"
    s.close()


def test_invalid_endpoint_name_rejected(tmp_path):
    s = make_store(tmp_path)
    with pytest.raises(ValueError):
        s.ensure_endpoint_table("bad-name!")
    s.close()


def test_schema_version(tmp_path):
    from syncronizer.core.migrations import run_migrations
    s = make_store(tmp_path)
    assert s.schema_version is None
    run_migrations(s)
    assert s.schema_version == 1
    s.close()
