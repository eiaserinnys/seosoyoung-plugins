from seosoyoung_plugins.channel_observer.store import ChannelStore


def test_pas_state_defaults_and_atomic_round_trip(tmp_path):
    store = ChannelStore(tmp_path)

    assert store.load_pas_state("C001") == {
        "channel_id": "C001",
        "session_id": None,
        "initialized_context": False,
        "read_ts": "",
        "read_thread_ts_map": {},
        "last_posted_ts": None,
        "last_posted_trigger_ts": None,
        "last_posted_batch_id": None,
        "updated_at": None,
    }

    saved = store.save_pas_state("C001", {"session_id": "pas-1", "read_ts": "1.2"})
    loaded = store.load_pas_state("C001")
    assert loaded["session_id"] == "pas-1"
    assert loaded["read_ts"] == "1.2"
    assert loaded["channel_id"] == "C001"
    assert loaded["updated_at"] == saved["updated_at"]
    assert loaded["updated_at"]


def test_corrupt_pas_state_returns_defaults(tmp_path):
    store = ChannelStore(tmp_path)
    state_path = store._channel_dir("C001") / "pas_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{", encoding="utf-8")

    assert store.load_pas_state("C001")["session_id"] is None


def test_move_snapshot_to_judged_preserves_new_thread_replies(tmp_path):
    store = ChannelStore(tmp_path)
    store.upsert_thread_message("C001", "10.0", {"ts": "10.1", "text": "snapshot"})
    store.upsert_thread_message("C001", "10.0", {"ts": "10.2", "text": "arrived during run"})

    store.move_snapshot_to_judged(
        "C001",
        set(),
        {"10.0"},
        {"10.0": {"10.1"}},
    )

    assert [item["ts"] for item in store.load_judged("C001")] == ["10.1"]
    assert [item["ts"] for item in store.load_thread_buffer("C001", "10.0")] == ["10.2"]


def test_move_snapshot_legacy_consumes_entire_selected_thread(tmp_path):
    store = ChannelStore(tmp_path)
    store.upsert_thread_message("C001", "10.0", {"ts": "10.1"})
    store.upsert_thread_message("C001", "10.0", {"ts": "10.2"})

    store.move_snapshot_to_judged("C001", set(), {"10.0"})

    assert [item["ts"] for item in store.load_judged("C001")] == ["10.1", "10.2"]
    assert store.load_thread_buffer("C001", "10.0") == []
