"""JSON 기반 저장소 단위 테스트

.md → .json 자동 마이그레이션, JSON 항목 배열 CRUD,
아카이브 생성, 일괄 마이그레이션 모듈을 검증합니다.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from seosoyoung_plugins.memory.store import (
    MemoryRecord,
    MemoryStore,
    generate_ltm_id,
    generate_obs_id,
    parse_md_observations,
    parse_md_persistent,
)


# ── 헬퍼 ──────────────────────────────────────────────────────


def _make_obs_items(items_data, session_date="2026-02-10"):
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


# ── ID 생성 ──────────────────────────────────────────────────


class TestGenerateObsId:
    def test_first_id_for_date(self):
        obs_id = generate_obs_id([], "2026-02-10")
        assert obs_id == "obs_20260210_000"

    def test_sequential_ids(self):
        existing = [{"id": "obs_20260210_000"}, {"id": "obs_20260210_001"}]
        obs_id = generate_obs_id(existing, "2026-02-10")
        assert obs_id == "obs_20260210_002"

    def test_different_date_resets_seq(self):
        existing = [{"id": "obs_20260210_005"}]
        obs_id = generate_obs_id(existing, "2026-02-11")
        assert obs_id == "obs_20260211_000"

    def test_defaults_to_today(self):
        obs_id = generate_obs_id([])
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert obs_id.startswith(f"obs_{today}_")


class TestGenerateLtmId:
    def test_first_id(self):
        ltm_id = generate_ltm_id([], "2026-02-10")
        assert ltm_id == "ltm_20260210_000"

    def test_sequential_ids(self):
        existing = [{"id": "ltm_20260210_000"}]
        ltm_id = generate_ltm_id(existing, "2026-02-10")
        assert ltm_id == "ltm_20260210_001"


# ── .md 파싱 ─────────────────────────────────────────────────


class TestParseMdObservations:
    def test_basic_parsing(self):
        md = (
            "## [2026-02-10] Session\n"
            "🔴 Critical finding\n"
            "🟡 Medium note\n"
            "🟢 Low priority\n"
        )
        items = parse_md_observations(md)
        assert len(items) == 3
        assert items[0]["priority"] == "🔴"
        assert items[0]["content"] == "Critical finding"
        assert items[0]["session_date"] == "2026-02-10"
        assert items[0]["source"] == "migrated"
        assert items[0]["id"] == "obs_20260210_000"

    def test_strips_priority_labels(self):
        """HIGH/MEDIUM/LOW 레이블이 제거됨"""
        md = (
            "## [2026-02-10] Session\n"
            "🔴 HIGH - 중요한 발견\n"
            "🟡 MEDIUM — 보통 메모\n"
            "🟢 LOW 낮은 우선순위\n"
        )
        items = parse_md_observations(md)
        assert items[0]["content"] == "중요한 발견"
        assert items[1]["content"] == "보통 메모"
        assert items[2]["content"] == "낮은 우선순위"

    def test_multiple_dates(self):
        md = (
            "## [2026-02-09] Day 1\n"
            "🔴 Day1 obs\n\n"
            "## [2026-02-10] Day 2\n"
            "🟡 Day2 obs\n"
        )
        items = parse_md_observations(md)
        assert len(items) == 2
        assert items[0]["session_date"] == "2026-02-09"
        assert items[1]["session_date"] == "2026-02-10"

    def test_empty_input(self):
        assert parse_md_observations("") == []
        assert parse_md_observations("  ") == []
        assert parse_md_observations(None) == []

    def test_no_emoji_lines_skipped(self):
        md = "## [2026-02-10] Session\n그냥 텍스트\n🔴 진짜 항목\n"
        items = parse_md_observations(md)
        assert len(items) == 1
        assert items[0]["content"] == "진짜 항목"


class TestParseMdPersistent:
    def test_basic_parsing(self):
        md = "🔴 장기 기억 1\n🟡 장기 기억 2\n"
        items = parse_md_persistent(md)
        assert len(items) == 2
        assert items[0]["priority"] == "🔴"
        assert items[0]["content"] == "장기 기억 1"
        expected_date = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert items[0]["id"] == f"ltm_{expected_date}_000"
        assert "promoted_at" in items[0]

    def test_plain_text_becomes_medium(self):
        """이모지 없는 줄은 🟡 우선순위로 변환"""
        md = "일반 텍스트 메모\n"
        items = parse_md_persistent(md)
        assert len(items) == 1
        assert items[0]["priority"] == "🟡"

    def test_skips_headers_and_hr(self):
        md = "# Header\n---\n🔴 진짜 항목\n"
        items = parse_md_persistent(md)
        assert len(items) == 1
        assert items[0]["content"] == "진짜 항목"

    def test_empty_input(self):
        assert parse_md_persistent("") == []
        assert parse_md_persistent(None) == []


# ── .md → .json 자동 마이그레이션 ────────────────────────────


class TestAutoMigrationObservations:
    """store.get_record() 호출 시 .md → .json 자동 마이그레이션"""

    def test_md_to_json_migration_on_get(self, store):
        """get_record 시 .md만 존재하면 자동으로 .json 변환"""
        thread_ts = "1234567890.123456"
        store._ensure_dirs()

        # 메타데이터 직접 생성
        meta = {
            "thread_ts": thread_ts,
            "user_id": "U12345",
            "username": "test_user",
            "observation_tokens": 50,
            "last_observed_at": None,
            "total_sessions_observed": 1,
            "reflection_count": 0,
            "created_at": "2026-02-10T00:00:00+00:00",
        }
        store._meta_path(thread_ts).write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )

        # 레거시 .md 파일 생성
        md_content = (
            "## [2026-02-10] Session Observations\n"
            "🔴 사용자는 커밋 메시지를 한글로 작성\n"
            "🟡 트렐로 체크리스트를 먼저 확인\n"
        )
        store._obs_md_path(thread_ts).write_text(md_content, encoding="utf-8")

        # get_record 호출 → 자동 마이그레이션
        record = store.get_record(thread_ts)

        assert record is not None
        assert len(record.observations) == 2
        assert record.observations[0]["priority"] == "🔴"
        assert record.observations[0]["content"] == "사용자는 커밋 메시지를 한글로 작성"
        assert record.observations[0]["source"] == "migrated"
        assert record.observations[0]["id"].startswith("obs_20260210_")

        # .json이 생성되고 .md는 삭제됨
        assert store._obs_path(thread_ts).exists()
        assert not store._obs_md_path(thread_ts).exists()

        # 생성된 .json의 내용 검증
        json_items = json.loads(
            store._obs_path(thread_ts).read_text(encoding="utf-8")
        )
        assert len(json_items) == 2

    def test_json_takes_priority_over_md(self, store):
        """.json과 .md 모두 존재하면 .json 우선"""
        thread_ts = "1234567890.999"
        store._ensure_dirs()

        meta = {"thread_ts": thread_ts}
        store._meta_path(thread_ts).write_text(
            json.dumps(meta), encoding="utf-8"
        )

        json_items = _make_obs_items([("🔴", "JSON 항목")])
        store._obs_path(thread_ts).write_text(
            json.dumps(json_items, ensure_ascii=False), encoding="utf-8"
        )

        md_content = "## [2026-02-10] Session\n🟡 MD 항목 (무시됨)\n"
        store._obs_md_path(thread_ts).write_text(md_content, encoding="utf-8")

        record = store.get_record(thread_ts)
        assert len(record.observations) == 1
        assert record.observations[0]["content"] == "JSON 항목"
        # .md는 그대로 남아 있음 (삭제하지 않음)
        assert store._obs_md_path(thread_ts).exists()

    def test_no_md_no_json_empty_observations(self, store):
        """둘 다 없으면 빈 관찰 리스트"""
        thread_ts = "empty_ts"
        store._ensure_dirs()

        meta = {"thread_ts": thread_ts}
        store._meta_path(thread_ts).write_text(
            json.dumps(meta), encoding="utf-8"
        )

        record = store.get_record(thread_ts)
        assert record is not None
        assert record.observations == []


class TestAutoMigrationPersistent:
    """store.get_persistent() 호출 시 .md → .json 자동 마이그레이션"""

    def test_md_to_json_migration_on_get(self, store):
        """get_persistent 시 .md만 존재하면 자동 변환"""
        store._ensure_dirs()

        md_content = "🔴 장기 기억 1\n🟡 장기 기억 2\n"
        store._persistent_md_path().write_text(md_content, encoding="utf-8")

        result = store.get_persistent()

        assert result is not None
        items = result["content"]
        assert len(items) == 2
        assert items[0]["priority"] == "🔴"
        assert items[0]["content"] == "장기 기억 1"
        assert items[0]["id"].startswith("ltm_")

        # .json이 생성되고 .md는 삭제됨
        assert store._persistent_content_path().exists()
        assert not store._persistent_md_path().exists()

    def test_json_takes_priority_over_md(self, store):
        """.json과 .md 모두 존재하면 .json 우선"""
        store._ensure_dirs()

        json_items = _make_ltm_items([("🔴", "JSON 장기 기억")])
        store._persistent_content_path().write_text(
            json.dumps(json_items, ensure_ascii=False), encoding="utf-8"
        )

        store._persistent_md_path().write_text(
            "🟡 MD 기억 (무시됨)\n", encoding="utf-8"
        )

        result = store.get_persistent()
        assert len(result["content"]) == 1
        assert result["content"][0]["content"] == "JSON 장기 기억"


class TestAutoMigrationNewObservations:
    """store.get_new_observations() 호출 시 .md → .json 자동 마이그레이션"""

    def test_md_to_json_migration(self, store):
        """레거시 .new.md가 있으면 파싱 후 삭제"""
        store._ensure_dirs()
        thread_ts = "new_obs_ts"

        md_content = "## [2026-02-10] Session\n🔴 새 관찰\n"
        store._new_obs_md_path(thread_ts).write_text(md_content, encoding="utf-8")

        items = store.get_new_observations(thread_ts)
        assert len(items) == 1
        assert items[0]["content"] == "새 관찰"

        # .md는 삭제됨
        assert not store._new_obs_md_path(thread_ts).exists()

    def test_json_takes_priority(self, store):
        """.new.json이 있으면 .new.md 무시"""
        store._ensure_dirs()
        thread_ts = "new_obs_ts2"

        json_items = _make_obs_items([("🔴", "JSON 새 관찰")])
        store._new_obs_path(thread_ts).write_text(
            json.dumps(json_items, ensure_ascii=False), encoding="utf-8"
        )
        store._new_obs_md_path(thread_ts).write_text(
            "🟡 MD 새 관찰 (무시됨)\n", encoding="utf-8"
        )

        items = store.get_new_observations(thread_ts)
        assert len(items) == 1
        assert items[0]["content"] == "JSON 새 관찰"


# ── JSON 항목 배열 CRUD ──────────────────────────────────────


class TestSaveAndGetJsonItems:
    """save_record / get_record의 JSON 항목 배열 저장·로드"""

    def test_roundtrip_observation_items(self, store):
        """관찰 항목 리스트가 정확히 직렬화/역직렬화됨"""
        items = _make_obs_items([
            ("🔴", "첫 번째 관찰"),
            ("🟡", "두 번째 관찰"),
            ("🟢", "세 번째 관찰"),
        ])
        record = MemoryRecord(
            thread_ts="ts_crud_001",
            user_id="U123",
            observations=items,
            observation_tokens=100,
        )

        store.save_record(record)
        loaded = store.get_record("ts_crud_001")

        assert loaded is not None
        assert len(loaded.observations) == 3
        for orig, loaded_item in zip(items, loaded.observations):
            assert orig["id"] == loaded_item["id"]
            assert orig["priority"] == loaded_item["priority"]
            assert orig["content"] == loaded_item["content"]
            assert orig["session_date"] == loaded_item["session_date"]
            assert orig["source"] == loaded_item["source"]

    def test_observations_stored_as_json_array(self, store):
        """.json 파일이 실제로 JSON 배열인지 확인"""
        items = _make_obs_items([("🔴", "테스트")])
        record = MemoryRecord(thread_ts="ts_format", observations=items)
        store.save_record(record)

        raw = json.loads(
            store._obs_path("ts_format").read_text(encoding="utf-8")
        )
        assert isinstance(raw, list)
        assert len(raw) == 1
        assert raw[0]["id"] == items[0]["id"]


class TestSaveAndGetPersistentJson:
    """save_persistent / get_persistent의 JSON 항목 배열 저장·로드"""

    def test_roundtrip_persistent_items(self, store):
        items = _make_ltm_items([
            ("🔴", "장기 기억 A"),
            ("🟡", "장기 기억 B"),
        ])
        meta = {"last_promoted_at": "2026-02-10T00:00:00Z", "total_promotions": 5}

        store.save_persistent(items, meta)
        result = store.get_persistent()

        assert result is not None
        assert result["content"] == items
        assert result["meta"]["total_promotions"] == 5

    def test_persistent_stored_as_json_array(self, store):
        """.json 파일이 실제로 JSON 배열인지 확인"""
        items = _make_ltm_items([("🔴", "테스트")])
        store.save_persistent(items, {})

        raw = json.loads(
            store._persistent_content_path().read_text(encoding="utf-8")
        )
        assert isinstance(raw, list)
        assert len(raw) == 1


class TestNewObservationsJson:
    """save_new_observations / get_new_observations의 JSON 항목 배열"""

    def test_roundtrip(self, store):
        items = _make_obs_items([("🔴", "이번 턴 새 관찰")])
        store.save_new_observations("ts_new", items)

        loaded = store.get_new_observations("ts_new")
        assert len(loaded) == 1
        assert loaded[0]["content"] == "이번 턴 새 관찰"

    def test_empty_when_not_exists(self, store):
        assert store.get_new_observations("NONEXISTENT") == []

    def test_clear(self, store):
        items = _make_obs_items([("🔴", "삭제될 관찰")])
        store.save_new_observations("ts_clear", items)
        store.clear_new_observations("ts_clear")
        assert store.get_new_observations("ts_clear") == []


class TestArchivePersistentJson:
    """archive_persistent가 .json 아카이브를 생성하는지 확인"""

    def test_archive_creates_json_file(self, store):
        items = _make_ltm_items([("🔴", "아카이브 대상 기억")])
        store.save_persistent(items, {})

        archive_path = store.archive_persistent()
        assert archive_path is not None
        assert archive_path.suffix == ".json"
        assert archive_path.parent.name == "archive"

        archived = json.loads(archive_path.read_text(encoding="utf-8"))
        assert isinstance(archived, list)
        assert archived[0]["content"] == "아카이브 대상 기억"

    def test_archive_preserves_original(self, store):
        items = _make_ltm_items([("🔴", "원본")])
        store.save_persistent(items, {"key": "value"})
        store.archive_persistent()

        result = store.get_persistent()
        assert result is not None
        assert result["content"][0]["content"] == "원본"


# ── 일괄 마이그레이션 모듈 ────────────────────────────────────


class TestMigrateMemoryDir:
    """migrate_memory_dir 일괄 마이그레이션 테스트"""

    def test_migrate_observations(self, tmp_path):
        from seosoyoung_plugins.memory.migration import migrate_memory_dir

        obs_dir = tmp_path / "observations"
        obs_dir.mkdir()

        (obs_dir / "ts_001.md").write_text(
            "## [2026-02-10] Session\n🔴 관찰 1\n🟡 관찰 2\n",
            encoding="utf-8",
        )
        (obs_dir / "ts_002.md").write_text(
            "## [2026-02-11] Session\n🟢 관찰 3\n",
            encoding="utf-8",
        )
        # .meta.json도 생성 (get_record와 무관하게 migration.py는 .md만 처리)

        report = migrate_memory_dir(tmp_path)

        assert report.total_converted == 2
        assert len(report.observations_converted) == 2
        assert report.errors == []

        # .json 생성 확인
        assert (obs_dir / "ts_001.json").exists()
        assert (obs_dir / "ts_002.json").exists()

        # .md 삭제, .md.bak 생성 확인
        assert not (obs_dir / "ts_001.md").exists()
        assert (obs_dir / "ts_001.md.bak").exists()

        # JSON 내용 검증
        items = json.loads(
            (obs_dir / "ts_001.json").read_text(encoding="utf-8")
        )
        assert len(items) == 2
        assert items[0]["content"] == "관찰 1"

    def test_migrate_persistent(self, tmp_path):
        from seosoyoung_plugins.memory.migration import migrate_memory_dir

        persistent_dir = tmp_path / "persistent"
        persistent_dir.mkdir()

        (persistent_dir / "recent.md").write_text(
            "🔴 장기 기억 1\n🟡 장기 기억 2\n",
            encoding="utf-8",
        )

        report = migrate_memory_dir(tmp_path)

        assert report.persistent_converted is True
        assert (persistent_dir / "recent.json").exists()
        assert not (persistent_dir / "recent.md").exists()
        assert (persistent_dir / "recent.md.bak").exists()

        items = json.loads(
            (persistent_dir / "recent.json").read_text(encoding="utf-8")
        )
        assert len(items) == 2

    def test_dry_run_no_changes(self, tmp_path):
        from seosoyoung_plugins.memory.migration import migrate_memory_dir

        obs_dir = tmp_path / "observations"
        obs_dir.mkdir()
        (obs_dir / "ts_001.md").write_text(
            "## [2026-02-10] Session\n🔴 관찰\n", encoding="utf-8"
        )

        report = migrate_memory_dir(tmp_path, dry_run=True)

        assert report.dry_run is True
        assert len(report.observations_converted) == 1
        # 실제 파일은 변경되지 않음
        assert (obs_dir / "ts_001.md").exists()
        assert not (obs_dir / "ts_001.json").exists()
        assert not (obs_dir / "ts_001.md.bak").exists()

    def test_skip_when_json_exists(self, tmp_path):
        from seosoyoung_plugins.memory.migration import migrate_memory_dir

        obs_dir = tmp_path / "observations"
        obs_dir.mkdir()
        (obs_dir / "ts_001.md").write_text("🔴 MD content\n", encoding="utf-8")
        (obs_dir / "ts_001.json").write_text("[]", encoding="utf-8")

        report = migrate_memory_dir(tmp_path)

        assert len(report.observations_converted) == 0
        assert len(report.skipped) == 1

    def test_empty_directory(self, tmp_path):
        from seosoyoung_plugins.memory.migration import migrate_memory_dir

        report = migrate_memory_dir(tmp_path)
        assert report.total_converted == 0
        assert report.errors == []

    def test_nonexistent_directory(self, tmp_path):
        from seosoyoung_plugins.memory.migration import migrate_memory_dir

        report = migrate_memory_dir(tmp_path / "nonexistent")
        assert report.total_converted == 0
        assert len(report.errors) == 1


class TestMigrationCli:
    """scripts/migrate_om_to_json.py CLI 테스트"""

    def test_cli_dry_run(self, tmp_path):
        import subprocess
        import sys

        obs_dir = tmp_path / "observations"
        obs_dir.mkdir()
        (obs_dir / "ts_001.md").write_text(
            "## [2026-02-10] Session\n🔴 CLI 테스트\n", encoding="utf-8"
        )

        script = Path(__file__).resolve().parent.parent.parent / "scripts" / "migrate_om_to_json.py"
        # sys.executable로 현재 테스트 러너와 동일한 인터프리터 사용 — ``python``
        # 하드코딩 시 Python 3.11이 잡혀 user-local 3.10의 tiktoken을 못 찾는 회귀.
        result = subprocess.run(
            [sys.executable, str(script), "--base-dir", str(tmp_path), "--dry-run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        assert result.returncode == 0
        assert "DRY RUN" in result.stdout
        # 실제 파일 변경 없음
        assert (obs_dir / "ts_001.md").exists()
        assert not (obs_dir / "ts_001.json").exists()

    def test_cli_actual_migration(self, tmp_path):
        import subprocess
        import sys

        obs_dir = tmp_path / "observations"
        obs_dir.mkdir()
        (obs_dir / "ts_001.md").write_text(
            "## [2026-02-10] Session\n🔴 실제 변환\n", encoding="utf-8"
        )

        script = Path(__file__).resolve().parent.parent.parent / "scripts" / "migrate_om_to_json.py"
        result = subprocess.run(
            [sys.executable, str(script), "--base-dir", str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        assert result.returncode == 0
        assert "관찰 로그 변환: 1건" in result.stdout
        assert (obs_dir / "ts_001.json").exists()

    def test_cli_nonexistent_dir(self):
        import subprocess
        import sys

        script = Path(__file__).resolve().parent.parent.parent / "scripts" / "migrate_om_to_json.py"
        result = subprocess.run(
            [sys.executable, str(script), "--base-dir", "/nonexistent/path"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        assert result.returncode == 1
