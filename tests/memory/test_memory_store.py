"""관찰 로그 저장소 단위 테스트"""

import json
from datetime import datetime, timezone

import pytest

from seosoyoung_plugins.memory.store import MemoryRecord, MemoryStore


def _make_obs_items(items_data, session_date="2026-02-10"):
    """테스트 헬퍼: 관찰 항목 리스트 생성"""
    result = []
    for i, (priority, content) in enumerate(items_data):
        result.append({
            "id": f"obs_{session_date.replace('-', '')}_{i:03d}",
            "priority": priority,
            "content": content,
            "session_date": session_date,
            "created_at": f"{session_date}T00:00:00+00:00",
            "source": "observer",
        })
    return result


def _make_ltm_items(items_data):
    """테스트 헬퍼: 장기 기억 항목 리스트 생성"""
    result = []
    for i, (priority, content) in enumerate(items_data):
        result.append({
            "id": f"ltm_20260210_{i:03d}",
            "priority": priority,
            "content": content,
            "promoted_at": "2026-02-10T00:00:00+00:00",
        })
    return result


@pytest.fixture
def store(tmp_path):
    return MemoryStore(base_dir=tmp_path)


@pytest.fixture
def sample_record():
    return MemoryRecord(
        thread_ts="1234567890.123456",
        user_id="U08HWT0C6K1",
        username="eias",
        observations=_make_obs_items([("🔴", "사용자는 커밋 메시지를 한글로 작성")]),
        observation_tokens=50,
        last_observed_at=datetime(2026, 2, 10, 9, 30, tzinfo=timezone.utc),
        total_sessions_observed=3,
        reflection_count=0,
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )


class TestMemoryRecord:
    def test_to_meta_dict(self, sample_record):
        meta = sample_record.to_meta_dict()
        assert meta["thread_ts"] == "1234567890.123456"
        assert meta["user_id"] == "U08HWT0C6K1"
        assert meta["username"] == "eias"
        assert meta["observation_tokens"] == 50
        assert meta["total_sessions_observed"] == 3
        assert meta["reflection_count"] == 0
        assert "last_observed_at" in meta
        assert "created_at" in meta

    def test_from_meta_dict_roundtrip(self, sample_record):
        meta = sample_record.to_meta_dict()
        restored = MemoryRecord.from_meta_dict(meta, sample_record.observations)

        assert restored.thread_ts == sample_record.thread_ts
        assert restored.user_id == sample_record.user_id
        assert restored.username == sample_record.username
        assert restored.observations == sample_record.observations
        assert restored.observation_tokens == sample_record.observation_tokens
        assert restored.total_sessions_observed == sample_record.total_sessions_observed
        assert restored.reflection_count == sample_record.reflection_count

    def test_from_meta_dict_missing_optional_fields(self):
        """선택 필드가 없어도 복원 가능"""
        data = {"thread_ts": "1234.5678"}
        record = MemoryRecord.from_meta_dict(data)
        assert record.thread_ts == "1234.5678"
        assert record.user_id == ""
        assert record.username == ""
        assert record.observation_tokens == 0
        assert record.last_observed_at is None

    def test_default_created_at(self):
        """created_at 기본값은 현재 시각"""
        record = MemoryRecord(thread_ts="1234.5678")
        assert record.created_at is not None
        assert isinstance(record.created_at, datetime)


class TestMemoryStoreGetSave:
    def test_get_nonexistent_record(self, store):
        result = store.get_record("NONEXISTENT")
        assert result is None

    def test_save_and_get_record(self, store, sample_record):
        store.save_record(sample_record)
        loaded = store.get_record(sample_record.thread_ts)

        assert loaded is not None
        assert loaded.thread_ts == sample_record.thread_ts
        assert loaded.user_id == sample_record.user_id
        assert loaded.username == sample_record.username
        assert loaded.observations == sample_record.observations
        assert loaded.observation_tokens == sample_record.observation_tokens
        assert loaded.total_sessions_observed == sample_record.total_sessions_observed

    def test_save_creates_directories(self, tmp_path):
        """존재하지 않는 디렉토리도 자동 생성"""
        deep_path = tmp_path / "a" / "b" / "c"
        store = MemoryStore(base_dir=deep_path)
        record = MemoryRecord(thread_ts="1234.5678", observations=[])
        store.save_record(record)

        assert store.observations_dir.exists()
        assert store.conversations_dir.exists()

    def test_overwrite_record(self, store, sample_record):
        store.save_record(sample_record)

        # 관찰 로그 갱신
        sample_record.observations = _make_obs_items([("🟡", "Updated observation")])
        sample_record.observation_tokens = 10
        sample_record.total_sessions_observed = 4
        store.save_record(sample_record)

        loaded = store.get_record(sample_record.thread_ts)
        assert loaded.observations[0]["content"] == "Updated observation"
        assert loaded.observation_tokens == 10
        assert loaded.total_sessions_observed == 4

    def test_multiple_sessions(self, store):
        """여러 세션의 레코드를 독립적으로 저장/로드"""
        record_a = MemoryRecord(
            thread_ts="ts_a", user_id="UA",
            observations=_make_obs_items([("🔴", "Session A observation")]),
        )
        record_b = MemoryRecord(
            thread_ts="ts_b", user_id="UB",
            observations=_make_obs_items([("🟡", "Session B observation")]),
        )

        store.save_record(record_a)
        store.save_record(record_b)

        loaded_a = store.get_record("ts_a")
        loaded_b = store.get_record("ts_b")

        assert loaded_a.observations[0]["content"] == "Session A observation"
        assert loaded_b.observations[0]["content"] == "Session B observation"


class TestMemoryStorePending:
    def test_append_and_load_pending(self, store):
        """pending 메시지 누적 및 로드"""
        messages1 = [{"role": "user", "content": "첫 번째 대화"}]
        messages2 = [{"role": "user", "content": "두 번째 대화"}]

        store.append_pending_messages("ts_1234", messages1)
        store.append_pending_messages("ts_1234", messages2)

        loaded = store.load_pending_messages("ts_1234")
        assert len(loaded) == 2
        assert loaded[0]["content"] == "첫 번째 대화"
        assert loaded[1]["content"] == "두 번째 대화"

    def test_load_empty_pending(self, store):
        """pending이 없으면 빈 리스트"""
        assert store.load_pending_messages("NONEXISTENT") == []

    def test_clear_pending(self, store):
        """pending 비우기"""
        store.append_pending_messages("ts_1234", [{"role": "user", "content": "test"}])
        assert len(store.load_pending_messages("ts_1234")) == 1

        store.clear_pending_messages("ts_1234")
        assert store.load_pending_messages("ts_1234") == []

    def test_clear_nonexistent_pending(self, store):
        """존재하지 않는 pending 비우기는 에러 없음"""
        store.clear_pending_messages("NONEXISTENT")

    def test_pending_preserves_unicode(self, store):
        """한글/이모지가 올바르게 저장/로드"""
        messages = [{"role": "user", "content": "🔴 캐릭터 정보 요청"}]
        store.append_pending_messages("ts_1234", messages)

        loaded = store.load_pending_messages("ts_1234")
        assert loaded[0]["content"] == "🔴 캐릭터 정보 요청"

    def test_pending_independent_per_session(self, store):
        """세션별 pending은 독립적"""
        store.append_pending_messages("ts_a", [{"role": "user", "content": "A"}])
        store.append_pending_messages("ts_b", [{"role": "user", "content": "B"}])

        assert store.load_pending_messages("ts_a")[0]["content"] == "A"
        assert store.load_pending_messages("ts_b")[0]["content"] == "B"

    def test_pending_creates_directory(self, tmp_path):
        """pending 디렉토리 자동 생성"""
        deep_path = tmp_path / "x" / "y"
        store = MemoryStore(base_dir=deep_path)
        store.append_pending_messages("ts_1234", [{"role": "user", "content": "test"}])
        assert store.pending_dir.exists()


class TestMemoryStoreInjectFlag:
    def test_set_and_check_flag(self, store):
        """플래그 설정 후 확인하면 True, 다시 확인하면 False"""
        store.set_inject_flag("ts_1234")
        assert store.check_and_clear_inject_flag("ts_1234") is True
        assert store.check_and_clear_inject_flag("ts_1234") is False

    def test_check_nonexistent_flag(self, store):
        """플래그 없으면 False"""
        assert store.check_and_clear_inject_flag("NONEXISTENT") is False

    def test_flag_independent_per_session(self, store):
        """세션별 플래그는 독립적"""
        store.set_inject_flag("ts_a")
        assert store.check_and_clear_inject_flag("ts_a") is True
        assert store.check_and_clear_inject_flag("ts_b") is False

    def test_flag_creates_directory(self, tmp_path):
        """디렉토리 자동 생성"""
        deep_path = tmp_path / "deep" / "path"
        store = MemoryStore(base_dir=deep_path)
        store.set_inject_flag("ts_1234")
        assert store.check_and_clear_inject_flag("ts_1234") is True

    def test_set_flag_idempotent(self, store):
        """플래그 중복 설정해도 문제 없음"""
        store.set_inject_flag("ts_1234")
        store.set_inject_flag("ts_1234")
        assert store.check_and_clear_inject_flag("ts_1234") is True
        assert store.check_and_clear_inject_flag("ts_1234") is False


class TestMemoryStoreConversation:
    def test_save_and_load_conversation(self, store):
        messages = [
            {"role": "user", "content": "안녕하세요", "timestamp": "2026-02-10T09:00:00Z"},
            {"role": "assistant", "content": "안녕하세요, 서소영입니다.", "timestamp": "2026-02-10T09:00:01Z"},
        ]

        store.save_conversation("1234567890.123456", messages)
        loaded = store.load_conversation("1234567890.123456")

        assert loaded is not None
        assert len(loaded) == 2
        assert loaded[0]["role"] == "user"
        assert loaded[1]["content"] == "안녕하세요, 서소영입니다."

    def test_load_nonexistent_conversation(self, store):
        result = store.load_conversation("NONEXISTENT")
        assert result is None

    def test_conversation_preserves_unicode(self, store):
        """한글/이모지가 올바르게 저장/로드되는지 확인"""
        messages = [
            {"role": "user", "content": "🔴 캐릭터 설정을 수정해줘"},
        ]
        store.save_conversation("ts_unicode", messages)
        loaded = store.load_conversation("ts_unicode")

        assert loaded[0]["content"] == "🔴 캐릭터 설정을 수정해줘"


class TestCandidates:
    """장기 기억 후보(candidates) 저장소 테스트"""

    def test_append_and_load_candidates(self, store):
        """후보 항목을 누적하고 로드"""
        entries = [
            {"ts": "2026-02-10T15:30:00Z", "priority": "🔴", "content": "사용자는 커밋 메시지를 한국어로 작성"},
            {"ts": "2026-02-10T16:00:00Z", "priority": "🟡", "content": "트렐로 체크리스트를 먼저 확인"},
        ]
        store.append_candidates("ts_1234", entries)

        loaded = store.load_candidates("ts_1234")
        assert len(loaded) == 2
        assert loaded[0]["priority"] == "🔴"
        assert loaded[1]["content"] == "트렐로 체크리스트를 먼저 확인"

    def test_append_candidates_accumulates(self, store):
        """여러 번 호출 시 누적"""
        store.append_candidates("ts_1234", [
            {"ts": "t1", "priority": "🔴", "content": "첫 번째"},
        ])
        store.append_candidates("ts_1234", [
            {"ts": "t2", "priority": "🟡", "content": "두 번째"},
        ])

        loaded = store.load_candidates("ts_1234")
        assert len(loaded) == 2

    def test_load_candidates_nonexistent(self, store):
        """존재하지 않는 세션은 빈 리스트"""
        assert store.load_candidates("NONEXISTENT") == []

    def test_load_all_candidates(self, store):
        """전체 세션의 후보를 수집"""
        store.append_candidates("ts_a", [
            {"ts": "t1", "priority": "🔴", "content": "A 세션 후보"},
        ])
        store.append_candidates("ts_b", [
            {"ts": "t2", "priority": "🟡", "content": "B 세션 후보 1"},
            {"ts": "t3", "priority": "🟢", "content": "B 세션 후보 2"},
        ])

        all_candidates = store.load_all_candidates()
        assert len(all_candidates) == 3

    def test_load_all_candidates_empty(self, store):
        """후보가 없으면 빈 리스트"""
        assert store.load_all_candidates() == []

    def test_count_all_candidate_tokens(self, store):
        """전체 후보 토큰 합산"""
        store.append_candidates("ts_a", [
            {"ts": "t1", "priority": "🔴", "content": "사용자는 커밋 메시지를 한국어로 작성하는 것을 선호한다"},
        ])
        store.append_candidates("ts_b", [
            {"ts": "t2", "priority": "🟡", "content": "트렐로 카드 작업 시 체크리스트를 먼저 확인"},
        ])

        token_count = store.count_all_candidate_tokens()
        assert token_count > 0

    def test_count_all_candidate_tokens_empty(self, store):
        """후보가 없으면 0"""
        assert store.count_all_candidate_tokens() == 0

    def test_clear_all_candidates(self, store):
        """모든 후보 파일 삭제"""
        store.append_candidates("ts_a", [
            {"ts": "t1", "priority": "🔴", "content": "A"},
        ])
        store.append_candidates("ts_b", [
            {"ts": "t2", "priority": "🟡", "content": "B"},
        ])

        store.clear_all_candidates()

        assert store.load_all_candidates() == []
        assert store.load_candidates("ts_a") == []
        assert store.load_candidates("ts_b") == []

    def test_clear_all_candidates_empty(self, store):
        """후보가 없어도 에러 없음"""
        store.clear_all_candidates()

    def test_candidates_preserves_unicode(self, store):
        """한글/이모지 보존"""
        entries = [
            {"ts": "t1", "priority": "🔴", "content": "🔴 캐릭터 정보 요청 패턴"},
        ]
        store.append_candidates("ts_1234", entries)
        loaded = store.load_candidates("ts_1234")
        assert loaded[0]["content"] == "🔴 캐릭터 정보 요청 패턴"

    def test_candidates_independent_per_session(self, store):
        """세션별 후보는 독립적"""
        store.append_candidates("ts_a", [{"ts": "t1", "priority": "🔴", "content": "A"}])
        store.append_candidates("ts_b", [{"ts": "t2", "priority": "🟡", "content": "B"}])

        assert len(store.load_candidates("ts_a")) == 1
        assert len(store.load_candidates("ts_b")) == 1
        assert store.load_candidates("ts_a")[0]["content"] == "A"

    def test_candidates_creates_directory(self, tmp_path):
        """디렉토리 자동 생성"""
        deep_path = tmp_path / "deep" / "path"
        store = MemoryStore(base_dir=deep_path)
        store.append_candidates("ts_1234", [{"ts": "t1", "priority": "🔴", "content": "test"}])
        assert store.candidates_dir.exists()


class TestPersistent:
    """장기 기억(persistent) 저장소 테스트"""

    def test_get_persistent_empty(self, store):
        """장기 기억이 없으면 None"""
        assert store.get_persistent() is None

    def test_save_and_get_persistent(self, store):
        """장기 기억 저장 및 로드"""
        content = _make_ltm_items([
            ("🔴", "사용자는 커밋 메시지를 한국어로 작성"),
            ("🟡", "트렐로 체크리스트 먼저 확인"),
        ])
        meta = {"last_promoted_at": "2026-02-10T15:30:00Z", "total_promotions": 1}
        store.save_persistent(content, meta)

        result = store.get_persistent()
        assert result is not None
        assert result["content"] == content
        assert result["meta"]["total_promotions"] == 1

    def test_save_persistent_overwrites(self, store):
        """저장 시 기존 내용 덮어쓰기"""
        store.save_persistent(
            _make_ltm_items([("🔴", "첫 번째 기억")]),
            {"total_promotions": 1},
        )
        second_content = _make_ltm_items([("🟡", "두 번째 기억")])
        store.save_persistent(second_content, {"total_promotions": 2})

        result = store.get_persistent()
        assert result["content"] == second_content
        assert result["meta"]["total_promotions"] == 2

    def test_save_persistent_preserves_unicode(self, store):
        """한글/이모지 보존"""
        content = _make_ltm_items([
            ("🔴", "캐릭터 정보 패턴"),
            ("🟢", "이모지 테스트 ⚡"),
        ])
        store.save_persistent(content, {})

        result = store.get_persistent()
        assert result["content"] == content

    def test_save_persistent_creates_directory(self, tmp_path):
        """디렉토리 자동 생성"""
        deep_path = tmp_path / "deep" / "path"
        store = MemoryStore(base_dir=deep_path)
        store.save_persistent(_make_ltm_items([("🔴", "test")]), {})
        assert store.persistent_dir.exists()


class TestMemoryRecordAnchorTs:
    """MemoryRecord.anchor_ts 필드 직렬화/역직렬화 테스트"""

    def test_anchor_ts_default_empty(self):
        """기본값은 빈 문자열"""
        record = MemoryRecord(thread_ts="ts_1234")
        assert record.anchor_ts == ""

    def test_anchor_ts_to_meta_dict_when_set(self):
        """anchor_ts가 설정되면 to_meta_dict에 포함"""
        record = MemoryRecord(thread_ts="ts_1234", anchor_ts="anchor_abc")
        meta = record.to_meta_dict()
        assert meta["anchor_ts"] == "anchor_abc"

    def test_anchor_ts_to_meta_dict_when_empty(self):
        """anchor_ts가 비었으면 to_meta_dict에 미포함"""
        record = MemoryRecord(thread_ts="ts_1234", anchor_ts="")
        meta = record.to_meta_dict()
        assert "anchor_ts" not in meta

    def test_anchor_ts_from_meta_dict_present(self):
        """anchor_ts가 dict에 있으면 복원"""
        data = {"thread_ts": "ts_1234", "anchor_ts": "anchor_abc"}
        record = MemoryRecord.from_meta_dict(data)
        assert record.anchor_ts == "anchor_abc"

    def test_anchor_ts_from_meta_dict_missing(self):
        """anchor_ts가 dict에 없으면 빈 문자열 기본값"""
        data = {"thread_ts": "ts_1234"}
        record = MemoryRecord.from_meta_dict(data)
        assert record.anchor_ts == ""

    def test_anchor_ts_roundtrip_via_store(self, store):
        """anchor_ts를 store에 저장/로드하면 보존"""
        record = MemoryRecord(
            thread_ts="ts_1234", user_id="U123", anchor_ts="anchor_xyz"
        )
        store.save_record(record)
        loaded = store.get_record("ts_1234")
        assert loaded is not None
        assert loaded.anchor_ts == "anchor_xyz"


class TestArchivePersistent:
    """장기 기억 아카이브 테스트"""

    def test_archive_persistent(self, store):
        """기존 장기 기억을 archive에 백업"""
        content = _make_ltm_items([("🔴", "원본 기억")])
        store.save_persistent(content, {"total_promotions": 1})
        archive_path = store.archive_persistent()

        assert archive_path is not None
        assert archive_path.exists()
        assert archive_path.parent.name == "archive"
        archived = json.loads(archive_path.read_text(encoding="utf-8"))
        assert archived[0]["content"] == "원본 기억"

    def test_archive_persistent_no_existing(self, store):
        """장기 기억이 없으면 None"""
        result = store.archive_persistent()
        assert result is None

    def test_archive_persistent_preserves_original(self, store):
        """아카이브 후 원본도 유지"""
        content = _make_ltm_items([("🔴", "원본 기억")])
        store.save_persistent(content, {"total_promotions": 1})
        store.archive_persistent()

        result = store.get_persistent()
        assert result is not None
        assert result["content"] == content

    def test_archive_multiple_times(self, store):
        """여러 번 아카이브해도 각각 다른 파일로 저장"""
        import time

        store.save_persistent(
            _make_ltm_items([("🔴", "기억 v1")]),
            {"total_promotions": 1},
        )
        path1 = store.archive_persistent()

        time.sleep(0.01)

        store.save_persistent(
            _make_ltm_items([("🟡", "기억 v2")]),
            {"total_promotions": 2},
        )
        path2 = store.archive_persistent()

        assert path1 != path2
        assert path1.exists()
        assert path2.exists()
