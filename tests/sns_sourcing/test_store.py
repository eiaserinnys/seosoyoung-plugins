from seosoyoung_plugins.sns_sourcing.store import SnsSourcingStore


def test_cursor_advances_only_forward(tmp_path):
    store = SnsSourcingStore(tmp_path)

    store.advance_cursor("C1", "1000.000002")
    store.advance_cursor("C1", "1000.000001")

    assert store.get_cursor("C1") == "1000.000002"


def test_ledger_keys_are_idempotent(tmp_path):
    store = SnsSourcingStore(tmp_path)

    store.append_ledger({"channel_id": "C1", "ts": "1.000001"})
    store.append_ledger({"channel_id": "C1", "ts": "1.000001"})

    assert store.ledger_keys() == {"C1:1.000001"}
    assert store.has_ledger("C1", "1.000001") is True


def test_run_marker(tmp_path):
    store = SnsSourcingStore(tmp_path)

    assert store.has_run_marker("2026-07-02:10:30") is False
    store.mark_run_slot("2026-07-02:10:30", "2026-07-02T01:30:00+00:00")

    assert store.has_run_marker("2026-07-02:10:30") is True

