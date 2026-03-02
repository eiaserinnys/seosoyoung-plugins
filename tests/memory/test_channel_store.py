"""ChannelStore 단위 테스트"""

import json

import pytest

from seosoyoung_plugins.channel_observer.store import ChannelStore


@pytest.fixture
def store(tmp_path):
    return ChannelStore(base_dir=tmp_path)


class TestPendingBuffer:
    """pending 버퍼 테스트 (구 채널 루트 메시지 버퍼)"""

    def test_append_and_load_pending(self, store):
        msg = {"ts": "1234.5678", "user": "U001", "text": "안녕하세요"}
        store.append_pending("C001", msg)

        loaded = store.load_pending("C001")
        assert len(loaded) == 1
        assert loaded[0]["text"] == "안녕하세요"

    def test_append_accumulates(self, store):
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "첫 번째"})
        store.append_pending("C001", {"ts": "2", "user": "U002", "text": "두 번째"})

        loaded = store.load_pending("C001")
        assert len(loaded) == 2

    def test_load_empty_buffer(self, store):
        assert store.load_pending("NONEXISTENT") == []

    def test_preserves_unicode(self, store):
        msg = {"ts": "1", "user": "U001", "text": "이모지 테스트 🔥"}
        store.append_pending("C001", msg)
        loaded = store.load_pending("C001")
        assert loaded[0]["text"] == "이모지 테스트 🔥"

    def test_independent_per_channel(self, store):
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "A"})
        store.append_pending("C002", {"ts": "2", "user": "U002", "text": "B"})

        assert store.load_pending("C001")[0]["text"] == "A"
        assert store.load_pending("C002")[0]["text"] == "B"

    def test_clear_pending(self, store):
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "삭제될 메시지"})
        assert len(store.load_pending("C001")) == 1

        store.clear_pending("C001")
        assert store.load_pending("C001") == []

    def test_clear_pending_nonexistent(self, store):
        """존재하지 않는 채널 pending 비우기도 에러 없음"""
        store.clear_pending("NONEXISTENT")


class TestBackwardCompatibility:
    """하위호환 별칭 테스트"""

    def test_append_channel_message_alias(self, store):
        store.append_channel_message("C001", {"ts": "1", "user": "U001", "text": "test"})
        assert len(store.load_pending("C001")) == 1

    def test_load_channel_buffer_alias(self, store):
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "test"})
        loaded = store.load_channel_buffer("C001")
        assert len(loaded) == 1
        assert loaded[0]["text"] == "test"


class TestJudgedBuffer:
    """judged 버퍼 테스트"""

    def test_append_and_load_judged(self, store):
        messages = [
            {"ts": "1", "user": "U001", "text": "판단 완료 메시지 1"},
            {"ts": "2", "user": "U002", "text": "판단 완료 메시지 2"},
        ]
        store.append_judged("C001", messages)

        loaded = store.load_judged("C001")
        assert len(loaded) == 2
        assert loaded[0]["text"] == "판단 완료 메시지 1"
        assert loaded[1]["text"] == "판단 완료 메시지 2"

    def test_append_judged_accumulates(self, store):
        store.append_judged("C001", [{"ts": "1", "user": "U001", "text": "첫 번째"}])
        store.append_judged("C001", [{"ts": "2", "user": "U002", "text": "두 번째"}])

        loaded = store.load_judged("C001")
        assert len(loaded) == 2

    def test_load_empty_judged(self, store):
        assert store.load_judged("NONEXISTENT") == []

    def test_clear_judged(self, store):
        store.append_judged("C001", [{"ts": "1", "user": "U001", "text": "test"}])
        assert len(store.load_judged("C001")) == 1

        store.clear_judged("C001")
        assert store.load_judged("C001") == []

    def test_clear_judged_nonexistent(self, store):
        store.clear_judged("NONEXISTENT")

    def test_independent_per_channel(self, store):
        store.append_judged("C001", [{"ts": "1", "user": "U001", "text": "A"}])
        store.append_judged("C002", [{"ts": "2", "user": "U002", "text": "B"}])

        assert store.load_judged("C001")[0]["text"] == "A"
        assert store.load_judged("C002")[0]["text"] == "B"


class TestMovePendingToJudged:
    """pending → judged 이동 테스트"""

    def test_move_pending_to_judged(self, store):
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "pending msg 1"})
        store.append_pending("C001", {"ts": "2", "user": "U002", "text": "pending msg 2"})

        store.move_pending_to_judged("C001")

        # pending은 비어야 함
        assert store.load_pending("C001") == []
        # judged에 이동되어야 함
        judged = store.load_judged("C001")
        assert len(judged) == 2
        assert judged[0]["text"] == "pending msg 1"
        assert judged[1]["text"] == "pending msg 2"

    def test_move_appends_to_existing_judged(self, store):
        """기존 judged에 pending이 추가되는지 확인"""
        store.append_judged("C001", [{"ts": "0", "user": "U001", "text": "기존 judged"}])
        store.append_pending("C001", {"ts": "1", "user": "U002", "text": "새 pending"})

        store.move_pending_to_judged("C001")

        judged = store.load_judged("C001")
        assert len(judged) == 2
        assert judged[0]["text"] == "기존 judged"
        assert judged[1]["text"] == "새 pending"

    def test_move_empty_pending(self, store):
        """빈 pending 이동도 에러 없음"""
        store.move_pending_to_judged("C001")
        assert store.load_judged("C001") == []
        assert store.load_pending("C001") == []

    def test_move_includes_thread_buffers(self, store):
        """스레드 버퍼도 judged로 이동되고 비워짐"""
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "채널 msg"})
        store.append_thread_message("C001", "ts_a", {"ts": "2", "user": "U002", "text": "스레드 msg A"})
        store.append_thread_message("C001", "ts_b", {"ts": "3", "user": "U003", "text": "스레드 msg B"})

        store.move_pending_to_judged("C001")

        # pending과 스레드 버퍼 모두 비어야 함
        assert store.load_pending("C001") == []
        assert store.load_all_thread_buffers("C001") == {}

        # judged에 채널 + 스레드 메시지가 모두 있어야 함
        judged = store.load_judged("C001")
        assert len(judged) == 3
        texts = [m["text"] for m in judged]
        assert "채널 msg" in texts
        assert "스레드 msg A" in texts
        assert "스레드 msg B" in texts

    def test_move_thread_only(self, store):
        """채널 pending 없이 스레드만 있어도 정상 동작"""
        store.append_thread_message("C001", "ts_a", {"ts": "1", "user": "U001", "text": "스레드만"})

        store.move_pending_to_judged("C001")

        assert store.load_all_thread_buffers("C001") == {}
        judged = store.load_judged("C001")
        assert len(judged) == 1
        assert judged[0]["text"] == "스레드만"


class TestMoveSnapshotToJudged:
    """스냅샷 기반 pending → judged 이동 테스트"""

    def test_snapshot_moves_only_specified_ts(self, store):
        """스냅샷에 포함된 ts만 judged로 이동하고 나머지는 pending에 남음"""
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "msg1"})
        store.append_pending("C001", {"ts": "2", "user": "U002", "text": "msg2"})
        store.append_pending("C001", {"ts": "3", "user": "U003", "text": "msg3 (새로 도착)"})

        store.move_snapshot_to_judged("C001", snapshot_ts={"1", "2"})

        # msg1, msg2는 judged로 이동
        judged = store.load_judged("C001")
        assert len(judged) == 2
        assert {m["ts"] for m in judged} == {"1", "2"}

        # msg3은 pending에 남음
        remaining = store.load_pending("C001")
        assert len(remaining) == 1
        assert remaining[0]["ts"] == "3"

    def test_snapshot_all_moved(self, store):
        """모든 pending이 스냅샷에 포함되면 pending이 비워짐"""
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "msg1"})
        store.append_pending("C001", {"ts": "2", "user": "U002", "text": "msg2"})

        store.move_snapshot_to_judged("C001", snapshot_ts={"1", "2"})

        assert store.load_judged("C001") != []
        assert store.load_pending("C001") == []

    def test_snapshot_empty_ts(self, store):
        """빈 스냅샷이면 아무것도 이동하지 않음"""
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "msg1"})

        store.move_snapshot_to_judged("C001", snapshot_ts=set())

        assert store.load_judged("C001") == []
        assert len(store.load_pending("C001")) == 1

    def test_snapshot_with_threads(self, store):
        """스냅샷에 포함된 스레드 버퍼만 judged로 이동"""
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "채널 msg"})
        store.append_thread_message("C001", "t_a", {"ts": "2", "user": "U002", "text": "스레드 A"})
        store.append_thread_message("C001", "t_b", {"ts": "3", "user": "U003", "text": "스레드 B (새로 도착)"})

        store.move_snapshot_to_judged(
            "C001", snapshot_ts={"1"}, snapshot_thread_ts={"t_a"},
        )

        judged = store.load_judged("C001")
        texts = [m["text"] for m in judged]
        assert "채널 msg" in texts
        assert "스레드 A" in texts
        assert "스레드 B (새로 도착)" not in texts

        # 남은 pending
        assert store.load_pending("C001") == []
        # t_b 스레드는 아직 남아있어야 함
        assert len(store.load_thread_buffer("C001", "t_b")) == 1
        # t_a 스레드는 비워져야 함
        assert store.load_thread_buffer("C001", "t_a") == []

    def test_snapshot_appends_to_existing_judged(self, store):
        """기존 judged에 스냅샷 메시지가 추가됨"""
        store.append_judged("C001", [{"ts": "0", "user": "U001", "text": "기존"}])
        store.append_pending("C001", {"ts": "1", "user": "U002", "text": "새 msg"})

        store.move_snapshot_to_judged("C001", snapshot_ts={"1"})

        judged = store.load_judged("C001")
        assert len(judged) == 2
        assert judged[0]["text"] == "기존"
        assert judged[1]["text"] == "새 msg"


class TestThreadBuffer:
    """스레드 메시지 버퍼 테스트"""

    def test_append_and_load_thread_message(self, store):
        msg = {"ts": "1234.9999", "user": "U001", "text": "스레드 메시지", "thread_ts": "1234.5678"}
        store.append_thread_message("C001", "1234.5678", msg)

        loaded = store.load_thread_buffer("C001", "1234.5678")
        assert len(loaded) == 1
        assert loaded[0]["text"] == "스레드 메시지"

    def test_thread_accumulates(self, store):
        store.append_thread_message("C001", "1234.5678", {"ts": "1", "user": "U001", "text": "첫째"})
        store.append_thread_message("C001", "1234.5678", {"ts": "2", "user": "U002", "text": "둘째"})

        loaded = store.load_thread_buffer("C001", "1234.5678")
        assert len(loaded) == 2

    def test_load_empty_thread_buffer(self, store):
        assert store.load_thread_buffer("C001", "NONEXISTENT") == []

    def test_independent_per_thread(self, store):
        store.append_thread_message("C001", "ts_a", {"ts": "1", "user": "U001", "text": "A"})
        store.append_thread_message("C001", "ts_b", {"ts": "2", "user": "U002", "text": "B"})

        assert store.load_thread_buffer("C001", "ts_a")[0]["text"] == "A"
        assert store.load_thread_buffer("C001", "ts_b")[0]["text"] == "B"

    def test_load_all_thread_buffers(self, store):
        store.append_thread_message("C001", "ts_a", {"ts": "1", "user": "U001", "text": "A1"})
        store.append_thread_message("C001", "ts_a", {"ts": "2", "user": "U001", "text": "A2"})
        store.append_thread_message("C001", "ts_b", {"ts": "3", "user": "U002", "text": "B1"})

        all_threads = store.load_all_thread_buffers("C001")
        assert "ts_a" in all_threads
        assert "ts_b" in all_threads
        assert len(all_threads["ts_a"]) == 2
        assert len(all_threads["ts_b"]) == 1

    def test_load_all_thread_buffers_empty(self, store):
        assert store.load_all_thread_buffers("NONEXISTENT") == {}


class TestTokenCounting:
    """토큰 카운팅 테스트"""

    def test_count_pending_tokens_empty(self, store):
        assert store.count_pending_tokens("NONEXISTENT") == 0

    def test_count_pending_tokens_with_data(self, store):
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "이것은 테스트 메시지입니다."})
        store.append_thread_message("C001", "ts_a", {"ts": "2", "user": "U001", "text": "스레드 메시지입니다."})

        token_count = store.count_pending_tokens("C001")
        assert token_count > 0

    def test_count_includes_channel_and_threads(self, store):
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "채널 메시지"})
        channel_only = store.count_pending_tokens("C001")

        store.append_thread_message("C001", "ts_a", {"ts": "2", "user": "U001", "text": "스레드 메시지"})
        with_thread = store.count_pending_tokens("C001")

        assert with_thread > channel_only

    def test_count_buffer_tokens_alias(self, store):
        """count_buffer_tokens는 count_pending_tokens의 하위호환 별칭"""
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "테스트"})
        assert store.count_buffer_tokens("C001") == store.count_pending_tokens("C001")

    def test_count_judged_plus_pending_tokens(self, store):
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "pending 메시지"})
        pending_only = store.count_pending_tokens("C001")

        store.append_judged("C001", [{"ts": "0", "user": "U001", "text": "judged 메시지"}])
        combined = store.count_judged_plus_pending_tokens("C001")

        assert combined > pending_only

    def test_count_judged_plus_pending_tokens_empty(self, store):
        assert store.count_judged_plus_pending_tokens("NONEXISTENT") == 0


class TestClearBuffers:
    """버퍼 비우기 테스트"""

    def test_clear_buffers(self, store):
        store.append_pending("C001", {"ts": "1", "user": "U001", "text": "채널"})
        store.append_thread_message("C001", "ts_a", {"ts": "2", "user": "U001", "text": "스레드"})
        store.append_judged("C001", [{"ts": "3", "user": "U001", "text": "judged"}])

        store.clear_buffers("C001")

        assert store.load_pending("C001") == []
        assert store.load_thread_buffer("C001", "ts_a") == []
        assert store.load_judged("C001") == []
        assert store.count_pending_tokens("C001") == 0

    def test_clear_nonexistent_buffers(self, store):
        """존재하지 않는 채널 버퍼 비우기도 에러 없음"""
        store.clear_buffers("NONEXISTENT")


class TestUpdateReactions:
    """reactions 갱신 테스트"""

    def test_add_reaction_to_pending(self, store):
        """pending 메시지에 리액션 추가"""
        store.append_pending("C001", {"ts": "1.1", "user": "U001", "text": "hello"})
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U002", action="added")

        msgs = store.load_pending("C001")
        assert len(msgs) == 1
        reactions = msgs[0].get("reactions", [])
        assert len(reactions) == 1
        assert reactions[0]["name"] == "thumbsup"
        assert reactions[0]["users"] == ["U002"]
        assert reactions[0]["count"] == 1

    def test_add_reaction_accumulates_users(self, store):
        """같은 이모지에 다른 유저가 추가"""
        store.append_pending("C001", {"ts": "1.1", "user": "U001", "text": "hello"})
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U002", action="added")
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U003", action="added")

        msgs = store.load_pending("C001")
        reactions = msgs[0]["reactions"]
        assert len(reactions) == 1
        assert reactions[0]["count"] == 2
        assert set(reactions[0]["users"]) == {"U002", "U003"}

    def test_add_different_emojis(self, store):
        """다른 이모지들이 각각 추가"""
        store.append_pending("C001", {"ts": "1.1", "user": "U001", "text": "hello"})
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U002", action="added")
        store.update_reactions("C001", ts="1.1", emoji="heart", user="U003", action="added")

        msgs = store.load_pending("C001")
        reactions = msgs[0]["reactions"]
        assert len(reactions) == 2
        names = {r["name"] for r in reactions}
        assert names == {"thumbsup", "heart"}

    def test_remove_reaction(self, store):
        """리액션 제거 시 users에서 삭제되고 count 감소"""
        store.append_pending("C001", {"ts": "1.1", "user": "U001", "text": "hello"})
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U002", action="added")
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U003", action="added")
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U002", action="removed")

        msgs = store.load_pending("C001")
        reactions = msgs[0]["reactions"]
        assert len(reactions) == 1
        assert reactions[0]["count"] == 1
        assert reactions[0]["users"] == ["U003"]

    def test_remove_last_user_deletes_reaction(self, store):
        """마지막 유저가 제거되면 reaction 항목 자체가 삭제"""
        store.append_pending("C001", {"ts": "1.1", "user": "U001", "text": "hello"})
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U002", action="added")
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U002", action="removed")

        msgs = store.load_pending("C001")
        reactions = msgs[0].get("reactions", [])
        assert len(reactions) == 0

    def test_update_reactions_in_judged(self, store):
        """judged 메시지에도 리액션 갱신"""
        store.append_judged("C001", [{"ts": "2.1", "user": "U001", "text": "judged msg"}])
        store.update_reactions("C001", ts="2.1", emoji="fire", user="U002", action="added")

        msgs = store.load_judged("C001")
        reactions = msgs[0].get("reactions", [])
        assert len(reactions) == 1
        assert reactions[0]["name"] == "fire"

    def test_update_reactions_in_thread(self, store):
        """스레드 메시지에도 리액션 갱신"""
        store.append_thread_message("C001", "parent.ts", {"ts": "3.1", "user": "U001", "text": "thread msg"})
        store.update_reactions("C001", ts="3.1", emoji="eyes", user="U002", action="added")

        msgs = store.load_thread_buffer("C001", "parent.ts")
        reactions = msgs[0].get("reactions", [])
        assert len(reactions) == 1
        assert reactions[0]["name"] == "eyes"

    def test_nonexistent_ts_is_noop(self, store):
        """존재하지 않는 ts에 대한 리액션 갱신은 무시 (에러 없음)"""
        store.append_pending("C001", {"ts": "1.1", "user": "U001", "text": "hello"})
        store.update_reactions("C001", ts="9.9", emoji="thumbsup", user="U002", action="added")

        msgs = store.load_pending("C001")
        assert msgs[0].get("reactions") is None

    def test_duplicate_add_is_idempotent(self, store):
        """같은 유저가 같은 이모지를 두 번 추가해도 중복 없음"""
        store.append_pending("C001", {"ts": "1.1", "user": "U001", "text": "hello"})
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U002", action="added")
        store.update_reactions("C001", ts="1.1", emoji="thumbsup", user="U002", action="added")

        msgs = store.load_pending("C001")
        reactions = msgs[0]["reactions"]
        assert reactions[0]["count"] == 1
        assert reactions[0]["users"] == ["U002"]


class TestDigest:
    """digest.md CRUD 테스트"""

    def test_get_digest_empty(self, store):
        assert store.get_digest("NONEXISTENT") is None

    def test_save_and_get_digest(self, store):
        content = "## 채널 관찰 요약\n\n- 오늘은 조용한 하루였다."
        meta = {"last_digested_at": "2026-02-11T10:00:00Z", "total_digests": 1}
        store.save_digest("C001", content, meta)

        result = store.get_digest("C001")
        assert result is not None
        assert result["content"] == content
        assert result["meta"]["total_digests"] == 1

    def test_save_digest_overwrites(self, store):
        store.save_digest("C001", "첫 번째 요약", {"total_digests": 1})
        store.save_digest("C001", "두 번째 요약", {"total_digests": 2})

        result = store.get_digest("C001")
        assert result["content"] == "두 번째 요약"
        assert result["meta"]["total_digests"] == 2

    def test_digest_preserves_unicode(self, store):
        content = "🔥 채널에서 열띤 토론이 벌어졌다"
        store.save_digest("C001", content, {})

        result = store.get_digest("C001")
        assert result["content"] == content

    def test_digest_independent_per_channel(self, store):
        store.save_digest("C001", "채널1 요약", {})
        store.save_digest("C002", "채널2 요약", {})

        assert store.get_digest("C001")["content"] == "채널1 요약"
        assert store.get_digest("C002")["content"] == "채널2 요약"

    def test_creates_directory(self, tmp_path):
        deep_path = tmp_path / "a" / "b" / "c"
        store = ChannelStore(base_dir=deep_path)
        store.save_digest("C001", "test", {})
        assert store.get_digest("C001")["content"] == "test"
