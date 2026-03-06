"""TrelloWatcher 테스트"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta
import threading
from pathlib import Path

from seosoyoung_plugins.trello.watcher import TrelloWatcher, TrackedCard


def _make_watcher(tmp_path, **overrides):
    """TrelloWatcher 인스턴스를 생성하는 헬퍼.

    새 생성자 시그니처에 맞춰 기본 Mock 값을 제공하고,
    overrides 로 개별 파라미터를 덮어쓸 수 있다.
    """
    trello_client = overrides.pop("trello_client", MagicMock())
    prompt_builder = overrides.pop("prompt_builder", MagicMock())
    get_session_lock = overrides.pop("get_session_lock", None)
    list_runner_ref = overrides.pop("list_runner_ref", None)
    data_dir = overrides.pop("data_dir", tmp_path)

    config = overrides.pop("config", {})
    # 기본 config 값 (테스트용)
    default_config = {
        "notify_channel": "C12345",
        "poll_interval": 5,
        "watch_lists": {},
        "dm_target_user_id": "",
        "polling_debug": False,
        "list_ids": {
            "to_go": None,
            "in_progress": None,
            "review": None,
            "done": None,
            "blocked": None,
            "backlog": None,
            "draft": None,
        },
    }
    # config 오버라이드 머지
    for k, v in config.items():
        if k == "list_ids" and isinstance(v, dict):
            default_config["list_ids"].update(v)
        else:
            default_config[k] = v

    watcher = TrelloWatcher(
        trello_client=trello_client,
        prompt_builder=prompt_builder,
        config=default_config,
        get_session_lock=get_session_lock,
        data_dir=data_dir,
        list_runner_ref=list_runner_ref,
    )

    # Set event loop for tests (normally set in _run() when thread starts)
    try:
        watcher._loop = asyncio.get_event_loop()
    except RuntimeError:
        watcher._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(watcher._loop)

    return watcher


class TestTrelloWatcherPauseResume:
    """TrelloWatcher pause/resume 기능 테스트"""

    def test_initial_not_paused(self, tmp_path):
        """초기 상태는 일시 중단 아님"""
        watcher = _make_watcher(tmp_path)
        assert watcher.is_paused is False

    def test_pause(self, tmp_path):
        """일시 중단"""
        watcher = _make_watcher(tmp_path)
        watcher.pause()
        assert watcher.is_paused is True

    def test_resume(self, tmp_path):
        """재개"""
        watcher = _make_watcher(tmp_path)
        watcher.pause()
        assert watcher.is_paused is True
        watcher.resume()
        assert watcher.is_paused is False

    def test_poll_skipped_when_paused(self, tmp_path):
        """일시 중단 상태면 폴링 스킵"""
        mock_trello = MagicMock()
        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"watch_lists": {"to_plan": "list123"}},
        )
        watcher.pause()
        watcher._poll()
        mock_trello.get_cards_in_list.assert_not_called()

    def test_poll_works_when_not_paused(self, tmp_path):
        """일시 중단 아니면 정상 폴링"""
        mock_trello = MagicMock()
        mock_trello.get_cards_in_list.return_value = []
        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"watch_lists": {"to_plan": "list123"}},
        )
        watcher._poll()
        mock_trello.get_cards_in_list.assert_called()


class TestTrelloWatcherTrackedCardLookup:
    """TrackedCard 조회 기능 테스트"""

    def test_get_tracked_by_thread_ts_found(self, tmp_path):
        """thread_ts로 TrackedCard 조회 - 찾음"""
        from seosoyoung_plugins.trello.watcher import ThreadCardInfo

        watcher = _make_watcher(tmp_path)

        tracked = TrackedCard(
            card_id="card123",
            card_name="테스트 카드",
            card_url="https://trello.com/c/abc123",
            list_id="list123",
            list_key="to_go",
            thread_ts="1234567890.123456",
            channel_id="C12345",
            detected_at="2024-01-01T00:00:00"
        )
        watcher._tracked["card123"] = tracked
        watcher._register_thread_card(tracked)

        result = watcher.get_tracked_by_thread_ts("1234567890.123456")
        assert result is not None
        assert result.card_id == "card123"
        assert result.card_name == "테스트 카드"

    def test_get_tracked_by_thread_ts_not_found(self, tmp_path):
        """thread_ts로 TrackedCard 조회 - 못 찾음"""
        watcher = _make_watcher(tmp_path)
        result = watcher.get_tracked_by_thread_ts("nonexistent_ts")
        assert result is None

    def test_build_reaction_execute_prompt(self, tmp_path):
        """리액션 기반 실행 프롬프트 생성"""
        from seosoyoung_plugins.trello.watcher import ThreadCardInfo
        from seosoyoung_plugins.trello.prompt_builder import PromptBuilder

        mock_trello = MagicMock()
        mock_trello.get_card.return_value = MagicMock(desc="")
        mock_trello.get_card_checklists.return_value = []
        mock_trello.get_card_comments.return_value = []

        prompt_builder = PromptBuilder(mock_trello, list_ids={})
        watcher = _make_watcher(tmp_path, prompt_builder=prompt_builder)

        info = ThreadCardInfo(
            thread_ts="1234567890.123456",
            channel_id="C12345",
            card_id="card123",
            card_name="기능 구현 작업",
            card_url="https://trello.com/c/abc123",
            created_at="2024-01-01T00:00:00"
        )

        prompt = watcher.build_reaction_execute_prompt(info)

        assert "🚀 리액션으로 실행이 요청된" in prompt
        assert "기능 구현 작업" in prompt
        assert "card123" in prompt
        assert "https://trello.com/c/abc123" in prompt
        assert "이미 워처에 의해 🔨 In Progress로 이동되었습니다" in prompt


class TestAutoMoveNoticeInPrompts:
    """프롬프트에 카드 자동 이동 안내가 포함되는지 테스트"""

    def test_to_go_execute_prompt_has_auto_move_notice(self, tmp_path):
        """실행 모드 프롬프트에 자동 이동 안내 포함"""
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung_plugins.trello.prompt_builder import PromptBuilder

        mock_trello = MagicMock()
        mock_trello.get_card_checklists.return_value = []
        mock_trello.get_card_comments.return_value = []
        prompt_builder = PromptBuilder(mock_trello, list_ids={})

        watcher = _make_watcher(tmp_path, prompt_builder=prompt_builder, trello_client=mock_trello)

        card = TrelloCard(
            id="card123",
            name="테스트 태스크",
            desc="태스크 본문",
            url="https://trello.com/c/abc123",
            list_id="list123",
            labels=[],
        )

        prompt = watcher.prompt_builder.build_to_go(card, has_execute=True)
        assert "이미 워처에 의해 🔨 In Progress로 이동되었습니다" in prompt
        assert "In Progress로 이동하지 마세요" in prompt

    def test_to_go_plan_prompt_has_auto_move_notice(self, tmp_path):
        """계획 모드 프롬프트에 자동 이동 안내 포함"""
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung_plugins.trello.prompt_builder import PromptBuilder

        mock_trello = MagicMock()
        mock_trello.get_card_checklists.return_value = []
        mock_trello.get_card_comments.return_value = []
        prompt_builder = PromptBuilder(mock_trello, list_ids={})

        watcher = _make_watcher(tmp_path, prompt_builder=prompt_builder, trello_client=mock_trello)

        card = TrelloCard(
            id="card456",
            name="계획 태스크",
            desc="태스크 본문",
            url="https://trello.com/c/def456",
            list_id="list123",
            labels=[],
        )

        prompt = watcher.prompt_builder.build_to_go(card, has_execute=False)
        assert "이미 워처에 의해 🔨 In Progress로 이동되었습니다" in prompt
        assert "In Progress로 이동하지 마세요" in prompt
        assert "📦 Backlog로 이동하세요" in prompt


class TestListRunSaySignature:
    """정주행 say() 함수가 send_long_message와 호환되는 시그니처를 갖는지 테스트

    Note: 이 테스트는 원본 seosoyoung에서 중요했지만, 플러그인 아키텍처에서는
    PresentationContext가 soulstream에 의해 제공되므로 플러그인 레벨에서는 테스트할 수 없음.
    대신 host (seosoyoung) 레벨에서 테스트되어야 함.
    """

    @pytest.mark.skip(reason="Plugin architecture: PresentationContext는 host가 제공하므로 host에서 테스트됨")
    def test_list_run_say_accepts_thread_ts_keyword(self, tmp_path):
        """정주행 say()가 thread_ts= 키워드 인자를 받을 수 있어야 함

        원본 seosoyoung 회귀 테스트: send_long_message가 say(text=..., thread_ts=thread_ts)로
        호출하므로 say()가 thread_ts 키워드를 받아야 TypeError가 발생하지 않음.

        플러그인 버전에서는 PresentationContext가 host에서 제공되므로 이 테스트는 skip됨.
        """
        pass


class TestStaleTrackedCardCleanup:
    """방안 A: _poll() 시 만료된 _tracked 항목 자동 정리 테스트"""

    def test_stale_card_auto_untracked_after_timeout(self, tmp_path):
        """2시간 이상 경과 + To Go에 없는 카드는 자동 untrack"""
        mock_trello = MagicMock()
        mock_trello.get_cards_in_list.return_value = []
        mock_trello.get_lists.return_value = []

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"watch_lists": {"to_go": "list_togo"}},
        )

        # 3시간 전에 추적 시작된 카드 (만료 기준 초과)
        stale_time = (datetime.now() - timedelta(hours=3)).isoformat()
        tracked = TrackedCard(
            card_id="stale_card",
            card_name="Stuck Card",
            card_url="https://trello.com/c/stale",
            list_id="list_togo",
            list_key="to_go",
            thread_ts="1111.2222",
            channel_id="C12345",
            detected_at=stale_time,
            session_id=None,  # 세션 없음
        )
        watcher._tracked["stale_card"] = tracked

        # 폴링 실행
        watcher._poll()

        # stale 카드가 untrack 되었어야 함
        assert "stale_card" not in watcher._tracked

    def test_recent_card_not_untracked(self, tmp_path):
        """30분 전 추적 시작된 카드는 아직 만료되지 않아 유지"""
        mock_trello = MagicMock()
        mock_trello.get_cards_in_list.return_value = []
        mock_trello.get_lists.return_value = []

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"watch_lists": {"to_go": "list_togo"}},
        )

        # 30분 전 추적 시작 (만료 기준 미달)
        recent_time = (datetime.now() - timedelta(minutes=30)).isoformat()
        tracked = TrackedCard(
            card_id="recent_card",
            card_name="Recent Card",
            card_url="https://trello.com/c/recent",
            list_id="list_togo",
            list_key="to_go",
            thread_ts="3333.4444",
            channel_id="C12345",
            detected_at=recent_time,
        )
        watcher._tracked["recent_card"] = tracked

        watcher._poll()

        # 아직 유지되어야 함
        assert "recent_card" in watcher._tracked


class TestHandleNewCardFailureUntrack:
    """방안 B: _handle_new_card 실패 시 untrack 테스트"""

    def test_untrack_on_slack_message_failure(self, tmp_path, mock_plugin_sdk):
        """Slack 메시지 전송 실패 시 카드가 _tracked에 남지 않아야 함"""
        from seosoyoung_plugins.trello.client import TrelloCard

        # Mock slack.send_message to raise an exception
        mock_plugin_sdk["slack"].send_message.side_effect = Exception("Slack API error")

        mock_trello = MagicMock()
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"list_ids": {"in_progress": "list_inprogress"}},
        )

        card = TrelloCard(
            id="fail_card",
            name="Fail Card",
            desc="",
            url="https://trello.com/c/fail",
            list_id="list_togo",
            labels=[],
        )

        watcher._handle_new_card(card, "to_go")

        # Slack 메시지 실패 시 _tracked에 카드가 남지 않아야 함
        assert "fail_card" not in watcher._tracked


class TestToGoReturnRetrack:
    """방안 C: 카드가 To Go로 다시 돌아왔을 때 re-track 테스트"""

    def test_card_returned_to_togo_is_retracked(self, tmp_path):
        """이미 _tracked에 있는 카드가 다시 To Go에 나타나면 re-track"""
        from seosoyoung_plugins.trello.client import TrelloCard

        mock_trello = MagicMock()
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True
        mock_trello.get_lists.return_value = []

        mock_slack = MagicMock()
        mock_slack.chat_postMessage.return_value = {"ts": "9999.0000"}

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            
            session_manager=MagicMock(create=MagicMock()),
            config={
                "watch_lists": {"to_go": "list_togo"},
                "list_ids": {"in_progress": "list_inprogress"},
            },
        )

        # stale tracked card (3시간 전)
        stale_time = (datetime.now() - timedelta(hours=3)).isoformat()
        old_tracked = TrackedCard(
            card_id="return_card",
            card_name="Return Card",
            card_url="https://trello.com/c/return",
            list_id="list_togo",
            list_key="to_go",
            thread_ts="old_thread",
            channel_id="C12345",
            detected_at=stale_time,
            session_id=None,
        )
        watcher._tracked["return_card"] = old_tracked

        # 이 카드가 다시 To Go에 있음
        card = TrelloCard(
            id="return_card",
            name="Return Card",
            desc="",
            url="https://trello.com/c/return",
            list_id="list_togo",
            labels=[],
        )
        mock_trello.get_cards_in_list.return_value = [card]

        watcher._poll()

        # stale 카드가 제거된 후 _handle_new_card로 다시 처리되어야 함
        # 또는 detected_at이 갱신되었어야 함
        # 핵심: 카드가 stuck 상태로 남지 않고 재처리됨
        assert "return_card" not in watcher._tracked or \
            watcher._tracked["return_card"].detected_at != stale_time


class TestPreemptiveCompact:
    """정주행 카드 완료 시 선제적 컨텍스트 컴팩트 테스트"""

    def test_compact_success_with_session_id(self, tmp_path, mock_plugin_sdk):
        """세션 ID가 있을 때 soulstream.compact() 호출 성공"""
        # Mock soulstream.get_session_id to return a session
        mock_plugin_sdk["soulstream"].get_session_id.return_value = "test-session-abc123"

        # Mock soulstream.compact to return success
        mock_compact_result = MagicMock()
        mock_compact_result.success = True
        mock_compact_result.session_id = "test-session-abc123"
        mock_plugin_sdk["soulstream"].compact.return_value = mock_compact_result

        watcher = _make_watcher(tmp_path)

        watcher._preemptive_compact("1234.5678", "C12345", "Test Card")

        # Verify soulstream.get_session_id was called
        mock_plugin_sdk["soulstream"].get_session_id.assert_called_once_with("1234.5678")

        # Verify soulstream.compact was called with correct session_id
        mock_plugin_sdk["soulstream"].compact.assert_called_once_with("test-session-abc123")

    def test_compact_skipped_without_session_id(self, tmp_path, mock_plugin_sdk):
        """세션 ID가 없으면 compact를 스킵"""
        # Mock soulstream.get_session_id to return None
        mock_plugin_sdk["soulstream"].get_session_id.return_value = None

        watcher = _make_watcher(tmp_path)

        watcher._preemptive_compact("1234.5678", "C12345", "Test Card")

        # Verify compact was NOT called when session_id is None
        mock_plugin_sdk["soulstream"].compact.assert_not_called()

    def test_compact_failure_does_not_block_next_card(self, tmp_path, mock_plugin_sdk):
        """compact 실패해도 예외가 전파되지 않아 다음 카드 처리를 막지 않음"""
        # Mock soulstream.get_session_id to return a session
        mock_plugin_sdk["soulstream"].get_session_id.return_value = "test-session-abc123"

        # Mock soulstream.compact to raise an exception
        mock_plugin_sdk["soulstream"].compact.side_effect = RuntimeError("Connection failed")

        watcher = _make_watcher(tmp_path)

        # Exception should not propagate
        watcher._preemptive_compact("1234.5678", "C12345", "Test Card")

    def test_compact_updates_session_id_when_changed(self, tmp_path, mock_plugin_sdk):
        """compact 후 세션 ID가 변경되어도 정상 처리됨

        Note: plugin_sdk architecture에서는 session_id 업데이트가 host에서 자동으로 처리되므로,
        플러그인은 단지 compact를 호출하기만 하면 됨. 원본 seosoyoung에서는 session_manager를
        통해 명시적으로 update_session_id를 호출했지만, 플러그인 버전에서는 이 책임이
        host에 위임되어 있음.
        """
        # Mock soulstream.get_session_id to return old session
        mock_plugin_sdk["soulstream"].get_session_id.return_value = "old-session-id"

        # Mock soulstream.compact to return success with new session_id
        mock_compact_result = MagicMock()
        mock_compact_result.ok = True
        mock_compact_result.session_id = "new-session-id"  # 변경된 session_id

        async def async_compact(session_id):
            return mock_compact_result
        mock_plugin_sdk["soulstream"].compact = AsyncMock(side_effect=async_compact)

        watcher = _make_watcher(tmp_path)

        watcher._preemptive_compact("1234.5678", "C12345", "Test Card")

        # Verify compact was called with old session_id
        mock_plugin_sdk["soulstream"].compact.assert_called_once()

        # session_id 업데이트는 host가 내부적으로 처리하므로 플러그인에서는 검증 불필요

class TestCheckRunListLabelsFiltering:
    """_check_run_list_labels 운영 리스트 필터링 및 가드 테스트"""

    def test_operational_lists_excluded(self, tmp_path):
        """운영 리스트(In Progress, Review, Done 등)는 정주행 대상에서 제외"""
        from seosoyoung_plugins.trello.client import TrelloCard

        mock_trello = MagicMock()

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={
                "watch_lists": {"to_go": "list_togo"},
                "list_ids": {
                    "review": "list_review",
                    "done": "list_done",
                    "in_progress": "list_inprogress",
                    "backlog": "list_backlog",
                    "blocked": "list_blocked",
                    "draft": "list_draft",
                },
            },
        )

        # 운영 리스트에 Run List 레이블이 있는 카드를 배치
        run_list_label = {"id": "label_run", "name": "🏃 Run List"}
        card_in_progress = TrelloCard(
            id="card_ip", name="Card In Progress", desc="",
            url="", list_id="list_inprogress",
            labels=[run_list_label],
        )

        mock_trello.get_lists.return_value = [
            {"id": "list_inprogress", "name": "🔨 In Progress"},
            {"id": "list_review", "name": "👀 Review"},
            {"id": "list_togo", "name": "🚀 To Go"},
            {"id": "list_plan", "name": "📌 PLAN: Test"},
        ]
        mock_trello.get_cards_in_list.return_value = [card_in_progress]
        mock_trello.remove_label_from_card.return_value = True

        watcher._check_run_list_labels()

        # 운영 리스트가 아닌 list_plan만 카드 조회 대상이어야 함
        # get_cards_in_list는 list_plan에 대해서만 호출되어야 함
        call_args = [c[0][0] for c in mock_trello.get_cards_in_list.call_args_list]
        assert "list_inprogress" not in call_args
        assert "list_review" not in call_args
        assert "list_togo" not in call_args

    def test_label_removal_failure_skips_list_run(self, tmp_path):
        """레이블 제거 실패 시 정주행을 시작하지 않아야 함"""
        from seosoyoung_plugins.trello.client import TrelloCard

        mock_trello = MagicMock()

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            list_runner_ref=MagicMock(return_value=MagicMock()),
        )

        run_list_label = {"id": "label_run", "name": "🏃 Run List"}
        card = TrelloCard(
            id="card_plan", name="Plan Card", desc="",
            url="", list_id="list_plan",
            labels=[run_list_label],
        )

        mock_trello.get_lists.return_value = [
            {"id": "list_plan", "name": "📌 PLAN: Test"},
        ]
        mock_trello.get_cards_in_list.return_value = [card]
        # 레이블 제거 실패
        mock_trello.remove_label_from_card.return_value = False

        with patch.object(watcher, "_start_list_run") as mock_start:
            watcher._check_run_list_labels()
            # _start_list_run이 호출되지 않아야 함
            mock_start.assert_not_called()

    def test_active_session_guard_prevents_duplicate(self, tmp_path):
        """동일 리스트에 활성 세션이 있으면 정주행 시작 안 함"""
        from seosoyoung_plugins.trello.client import TrelloCard

        mock_trello = MagicMock()

        mock_list_runner = MagicMock()
        active_session = MagicMock()
        active_session.list_id = "list_plan"
        mock_list_runner.get_active_sessions.return_value = [active_session]

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            list_runner_ref=lambda: mock_list_runner,
        )

        run_list_label = {"id": "label_run", "name": "🏃 Run List"}
        card = TrelloCard(
            id="card_plan", name="Plan Card", desc="",
            url="", list_id="list_plan",
            labels=[run_list_label],
        )

        mock_trello.get_lists.return_value = [
            {"id": "list_plan", "name": "📌 PLAN: Test"},
        ]
        mock_trello.get_cards_in_list.return_value = [card]
        mock_trello.remove_label_from_card.return_value = True

        with patch.object(watcher, "_start_list_run") as mock_start:
            watcher._check_run_list_labels()
            mock_start.assert_not_called()


class TestProcessListRunCardTracked:
    """_process_list_run_card가 _tracked에 등록하는지 테스트"""

    def test_list_run_card_registered_in_tracked(self, tmp_path):
        """정주행 카드가 _tracked에 등록되어 To Go 감지와 중복 방지"""
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung_plugins.trello.list_runner import ListRunner, SessionStatus

        mock_trello = MagicMock()
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.return_value = {"ts": "1234567890.123456"}

        list_runner = ListRunner(data_dir=tmp_path)

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            
            list_runner_ref=lambda: list_runner,
        )

        # 세션 생성
        session = list_runner.create_session(
            list_id="list_123",
            list_name="Plan List",
            card_ids=["card_a"],
        )
        list_runner.update_session_status(session.session_id, SessionStatus.RUNNING)

        card = TrelloCard(
            id="card_a", name="Test Card", desc="",
            url="https://trello.com/c/abc", list_id="list_123",
            labels=[],
        )
        mock_trello.get_card.return_value = card

        # _process_list_run_card 호출 전 _tracked 확인
        assert "card_a" not in watcher._tracked

        # 세션 락 없이 실행
        watcher.get_session_lock = None
        watcher._process_list_run_card(session.session_id, "1234567890.123456")

        # 정주행 카드가 _tracked에 등록되어야 함
        assert "card_a" in watcher._tracked
        assert watcher._tracked["card_a"].list_key == "list_run"

    def test_list_run_first_card_not_redetected_by_poll(self, tmp_path):
        """정주행 첫 카드가 _tracked에 있으면 _poll에서 재감지되지 않음"""
        from seosoyoung_plugins.trello.client import TrelloCard

        mock_trello = MagicMock()
        mock_trello.get_lists.return_value = []

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"watch_lists": {"to_go": "list_togo"}},
        )

        # 정주행으로 이미 _tracked에 등록된 카드
        tracked = TrackedCard(
            card_id="card_run_1",
            card_name="Run Card",
            card_url="https://trello.com/c/run1",
            list_id="list_plan",
            list_key="list_run",
            thread_ts="thread_123",
            channel_id="C12345",
            detected_at=datetime.now().isoformat(),
            has_execute=True,
        )
        watcher._tracked["card_run_1"] = tracked

        # 같은 카드가 To Go에도 나타남 (이론적으로 불가능하지만 방어적으로 테스트)
        card = TrelloCard(
            id="card_run_1", name="Run Card", desc="",
            url="https://trello.com/c/run1", list_id="list_togo",
            labels=[],
        )
        mock_trello.get_cards_in_list.return_value = [card]

        with patch.object(watcher, "_handle_new_card") as mock_handle:
            watcher._poll()
            # _tracked에 이미 있으므로 _handle_new_card가 호출되지 않아야 함
            mock_handle.assert_not_called()


class TestListRunDuplicatePrevention:
    """리스트 정주행 동시 실행 시 중복 방지 테스트"""

    def test_list_run_lock_serializes_concurrent_starts(self, tmp_path):
        """_list_run_lock이 동시 _check_run_list_labels 호출을 직렬화

        두 스레드가 동시에 _check_run_list_labels를 호출하면,
        첫 번째가 세션을 생성한 후 두 번째는 활성 세션을 발견하여 스킵해야 함.
        """
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung_plugins.trello.list_runner import ListRunner

        mock_trello = MagicMock()
        list_runner = ListRunner(data_dir=tmp_path)

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            list_runner_ref=lambda: list_runner,
        )

        run_list_label = {"id": "label_run", "name": "🏃 Run List"}
        card = TrelloCard(
            id="card_plan", name="Plan Card", desc="",
            url="", list_id="list_plan",
            labels=[run_list_label],
        )

        mock_trello.get_lists.return_value = [
            {"id": "list_plan", "name": "📌 PLAN: Test"},
        ]
        mock_trello.get_cards_in_list.return_value = [card]
        mock_trello.remove_label_from_card.return_value = True

        start_call_count = 0

        def counting_start(*args, **kwargs):
            nonlocal start_call_count
            start_call_count += 1
            list_runner.create_session(args[0], args[1], [c.id for c in args[2]])

        with patch.object(watcher, "_start_list_run", side_effect=counting_start):
            barrier = threading.Barrier(2)
            results = []

            def run_check():
                barrier.wait()
                watcher._check_run_list_labels()
                results.append(True)

            t1 = threading.Thread(target=run_check)
            t2 = threading.Thread(target=run_check)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert start_call_count <= 1, (
            f"_start_list_run이 {start_call_count}번 호출됨 (기대: ≤1)"
        )

    def test_tracked_card_skipped_in_list_run(self, tmp_path, mock_plugin_sdk):
        """다른 세션에서 처리 중인 카드(다른 thread_ts)는 skipped_duplicate로 처리"""
        from seosoyoung_plugins.trello.list_runner import ListRunner, SessionStatus

        mock_trello = MagicMock()
        list_runner = ListRunner(data_dir=tmp_path)

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            list_runner_ref=lambda: list_runner,
        )

        session = list_runner.create_session(
            list_id="list_123",
            list_name="Plan List",
            card_ids=["card_a"],
        )
        list_runner.update_session_status(session.session_id, SessionStatus.RUNNING)

        tracked = TrackedCard(
            card_id="card_a",
            card_name="Card A",
            card_url="https://trello.com/c/a",
            list_id="list_123",
            list_key="list_run",
            thread_ts="other_thread",
            channel_id="C12345",
            detected_at=datetime.now().isoformat(),
            has_execute=True,
        )
        watcher._tracked["card_a"] = tracked

        watcher._process_list_run_card(session.session_id, "ts_123")

        updated_session = list_runner.get_session(session.session_id)
        assert "card_a" in updated_session.processed_cards
        assert updated_session.processed_cards["card_a"] == "skipped_duplicate"
        assert updated_session.status == SessionStatus.COMPLETED

    def test_watcher_has_list_run_lock(self, tmp_path):
        """TrelloWatcher가 _list_run_lock 속성을 가지고 있어야 함"""
        watcher = _make_watcher(tmp_path)
        assert hasattr(watcher, "_list_run_lock")
        assert isinstance(watcher._list_run_lock, type(threading.Lock()))


class TestGetOperationalListIds:
    """_get_operational_list_ids 테스트"""

    def test_collects_all_operational_ids(self, tmp_path):
        """모든 운영 리스트 ID가 수집됨"""
        watcher = _make_watcher(
            tmp_path,
            config={
                "watch_lists": {"to_go": "list_togo"},
                "list_ids": {
                    "review": "list_review",
                    "done": "list_done",
                    "in_progress": "list_ip",
                    "backlog": "list_bl",
                    "blocked": "list_blocked",
                    "draft": "list_draft",
                },
            },
        )

        ids = watcher._get_operational_list_ids()
        assert "list_togo" in ids
        assert "list_review" in ids
        assert "list_done" in ids
        assert "list_ip" in ids
        assert "list_bl" in ids
        assert "list_blocked" in ids
        assert "list_draft" in ids

    def test_empty_ids_excluded(self, tmp_path):
        """빈 문자열 ID는 제외됨"""
        watcher = _make_watcher(
            tmp_path,
            config={
                "watch_lists": {"to_go": "list_togo"},
                "list_ids": {
                    "review": "",
                    "done": None,
                    "in_progress": "list_ip",
                    "backlog": "",
                    "blocked": None,
                    "draft": "",
                },
            },
        )

        ids = watcher._get_operational_list_ids()
        assert "" not in ids
        assert None not in ids
        assert "list_togo" in ids
        assert "list_ip" in ids


class _SyncThread:
    """테스트용: threading.Thread를 동기적으로 실행하는 대체 클래스"""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **extra):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        if self.target:
            self.target(*self.args, **self.kwargs)


class TestMultiCardChainingIntegration:
    """멀티 카드 체이닝 통합 테스트 (card1→card2→card3→COMPLETED)

    _spawn_claude_thread가 별도 스레드를 생성하므로, claude_runner_factory를
    동기적으로 완료하도록 모킹하여 체이닝 흐름을 검증합니다.
    on_success 내부의 threading.Thread도 동기화하여 전체 체인을 검증합니다.
    """

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_three_card_chaining_completes(self, tmp_path, mock_plugin_sdk):
        """3장의 카드가 순차적으로 처리되고 세션이 COMPLETED 상태가 됨"""
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung_plugins.trello.list_runner import ListRunner, SessionStatus

        mock_trello = MagicMock()
        list_runner = ListRunner(data_dir=tmp_path)

        def sync_spawn(*, prompt, thread_ts, channel,
                       tracked, dm_channel_id=None, dm_thread_ts=None,
                       on_success=None, on_error=None, on_finally=None):
            if on_success:
                on_success()
            if on_finally:
                on_finally()

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            list_runner_ref=lambda: list_runner,
            config={"list_ids": {"in_progress": "list_inprogress"}},
        )
        watcher._spawn_claude_thread = sync_spawn
        watcher._preemptive_compact = MagicMock()

        cards_data = {
            "card_a": TrelloCard(
                id="card_a", name="Card A", desc="",
                url="https://trello.com/c/a", list_id="list_plan", labels=[],
            ),
            "card_b": TrelloCard(
                id="card_b", name="Card B", desc="",
                url="https://trello.com/c/b", list_id="list_plan", labels=[],
            ),
            "card_c": TrelloCard(
                id="card_c", name="Card C", desc="",
                url="https://trello.com/c/c", list_id="list_plan", labels=[],
            ),
        }
        mock_trello.get_card.side_effect = lambda cid: cards_data.get(cid)
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True

        session = list_runner.create_session(
            list_id="list_plan",
            list_name="Plan List",
            card_ids=["card_a", "card_b", "card_c"],
        )

        watcher._process_list_run_card(session.session_id, "thread_123")

        updated = list_runner.get_session(session.session_id)
        assert updated.status == SessionStatus.COMPLETED
        assert updated.current_index == 3
        assert updated.processed_cards == {
            "card_a": "completed",
            "card_b": "completed",
            "card_c": "completed",
        }

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_chaining_continues_after_compact_failure(self, tmp_path, mock_plugin_sdk):
        """_preemptive_compact 실패해도 체인이 끊기지 않음"""
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung_plugins.trello.list_runner import ListRunner, SessionStatus

        mock_trello = MagicMock()
        list_runner = ListRunner(data_dir=tmp_path)

        def sync_spawn(*, prompt, thread_ts, channel,
                       tracked, dm_channel_id=None, dm_thread_ts=None,
                       on_success=None, on_error=None, on_finally=None):
            if on_success:
                on_success()
            if on_finally:
                on_finally()

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            list_runner_ref=lambda: list_runner,
            config={"list_ids": {"in_progress": "list_inprogress"}},
        )
        watcher._spawn_claude_thread = sync_spawn
        watcher._preemptive_compact = MagicMock(
            side_effect=RuntimeError("compact hang")
        )

        cards_data = {
            "card_a": TrelloCard(
                id="card_a", name="Card A", desc="",
                url="https://trello.com/c/a", list_id="list_plan", labels=[],
            ),
            "card_b": TrelloCard(
                id="card_b", name="Card B", desc="",
                url="https://trello.com/c/b", list_id="list_plan", labels=[],
            ),
        }
        mock_trello.get_card.side_effect = lambda cid: cards_data.get(cid)
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True

        session = list_runner.create_session(
            list_id="list_plan",
            list_name="Plan List",
            card_ids=["card_a", "card_b"],
        )

        watcher._process_list_run_card(session.session_id, "thread_123")

        updated = list_runner.get_session(session.session_id)
        assert updated.status == SessionStatus.COMPLETED
        assert updated.current_index == 2

    def test_on_success_exception_does_not_trigger_on_error(self, tmp_path, mock_plugin_sdk):
        """on_success 예외가 on_error를 트리거하지 않음 (_spawn_claude_thread 격리 검증)"""
        watcher = _make_watcher(tmp_path)

        tracked = TrackedCard(
            card_id="card_test",
            card_name="Test Card",
            card_url="",
            list_id="list_test",
            list_key="test",
            thread_ts="thread_123",
            channel_id="C12345",
            detected_at="2026-01-01T00:00:00",
        )

        on_error_called = []

        def failing_on_success():
            raise RuntimeError("on_success exploded")

        def tracking_on_error(e):
            on_error_called.append(e)

        # _spawn_claude_thread 직접 호출 후 스레드 완료 대기
        watcher.get_session_lock = None
        watcher._spawn_claude_thread(
            prompt="test",
            thread_ts="thread_123",
            channel="C12345",
            tracked=tracked,
            on_success=failing_on_success,
            on_error=tracking_on_error,
        )

        # 스레드 완료 대기
        import time
        time.sleep(0.5)

        # on_error가 호출되지 않아야 함 (Claude 실행 자체는 성공)
        assert len(on_error_called) == 0

    def test_process_list_run_card_handles_trello_api_error(self, tmp_path):
        """_process_list_run_card에서 Trello API 오류 시 전역 try-except가 잡음"""
        from seosoyoung_plugins.trello.list_runner import ListRunner, SessionStatus

        mock_trello = MagicMock()
        mock_trello.get_card.side_effect = ConnectionError("Trello API down")

        list_runner = ListRunner(data_dir=tmp_path)
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.return_value = {"ts": "thread_123"}

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            
            list_runner_ref=lambda: list_runner,
            config={"list_ids": {"in_progress": "list_ip"}},
        )

        session = list_runner.create_session(
            list_id="list_plan",
            list_name="Plan",
            card_ids=["card_a"],
        )
        list_runner.update_session_status(session.session_id, SessionStatus.RUNNING)

        watcher._process_list_run_card(session.session_id, "thread_123")

        updated = list_runner.get_session(session.session_id)
        assert updated.status == SessionStatus.PAUSED

    def test_compact_timeout_does_not_block_chain(self, tmp_path, mock_plugin_sdk):
        """_preemptive_compact 타임아웃 시 체인이 계속됨 (plugin_sdk 사용)"""
        import concurrent.futures

        watcher = _make_watcher(tmp_path)

        # plugin_sdk.soulstream.get_session_id가 session_id를 반환하도록 설정
        mock_plugin_sdk["soulstream"].get_session_id = AsyncMock(return_value="test-session")
        # plugin_sdk.soulstream.compact가 TimeoutError를 raise하도록 설정
        mock_plugin_sdk["soulstream"].compact = AsyncMock(side_effect=concurrent.futures.TimeoutError())

        # TimeoutError가 발생해도 정상 반환 (예외 전파 없음)
        # _preemptive_compact는 내부에서 plugin_sdk.soulstream.compact를 호출함
        watcher._preemptive_compact("1234.5678", "C12345", "Test Card")

        # soulstream.compact가 호출되었는지 확인
        mock_plugin_sdk["soulstream"].compact.assert_called_once()


class TestSpawnClaudeThreadLockHandling:
    """_spawn_claude_thread 락 처리 버그 테스트

    버그 1·2: watcher가 직접 락을 acquire한 뒤 claude_runner_factory(executor.run)도
    같은 락을 acquire하여 이중 관리가 발생하는 문제.

    버그 3: on_success()가 락 해제 전에 호출되어 다음 스레드가 락 획득에 실패하는 문제.
    """

    def test_lock_released_before_on_success_next_thread_can_acquire(self, tmp_path, mock_plugin_sdk):
        """on_success에서 시작한 새 스레드가 같은 thread_ts 락을 즉시 획득할 수 있어야 함

        버그 3 검증:
        - on_success()가 락 해제 전에 호출되면, on_success 내부의 새 스레드가
          락 획득을 시도할 때 여전히 watcher 스레드가 잡고 있어 blocking됨.
        - 수정 후: 락 해제 후에 on_success()가 호출되어야 함.
        - RLock은 스레드 소유권 기반이므로 다른 스레드에서 획득 시도해야 버그가 드러남.
        """
        lock = threading.RLock()
        other_thread_lock_try_result = []

        def get_session_lock(ts):
            return lock

        watcher = _make_watcher(tmp_path, get_session_lock=get_session_lock)

        tracked = TrackedCard(
            card_id="card_test",
            card_name="Test Card",
            card_url="",
            list_id="list_test",
            list_key="test",
            thread_ts="thread_lock_test",
            channel_id="C12345",
            detected_at="2026-01-01T00:00:00",
        )

        event = threading.Event()

        def on_success():
            # 이 시점에서 lock은 이미 해제되어 있어야 함 (수정 후)
            # 다른 스레드에서 non-blocking 획득 시도 → lock이 free해야 True
            result_holder = []
            def try_from_other_thread():
                acquired = lock.acquire(blocking=False)
                result_holder.append(acquired)
                if acquired:
                    lock.release()
            t = threading.Thread(target=try_from_other_thread)
            t.start()
            t.join(timeout=1.0)
            if result_holder:
                other_thread_lock_try_result.append(result_holder[0])
            event.set()

        watcher._spawn_claude_thread(
            prompt="test",
            thread_ts="thread_lock_test",
            channel="C12345",
            tracked=tracked,
            on_success=on_success,
        )

        # 스레드 완료 대기
        event.wait(timeout=5.0)

        # on_success 호출 확인
        assert other_thread_lock_try_result, "on_success가 호출되지 않았습니다"
        # 수정 후: on_success 호출 시점에 다른 스레드가 락을 획득할 수 있어야 함
        assert other_thread_lock_try_result[0] is True, (
            "on_success 호출 시점에 다른 스레드가 lock을 획득할 수 없습니다 (버그 3)\n"
            "락 해제 후에 on_success()를 호출해야 합니다."
        )

    def test_watcher_does_not_double_manage_lock(self, tmp_path, mock_plugin_sdk):
        """watcher가 락을 직접 관리하지 않아도 executor가 올바르게 처리함을 검증

        버그 1·2 검증:
        - watcher가 lock.acquire()를 하고 executor.run()도 acquire()를 하면
          같은 스레드에서 RLock count가 2가 됨.
          watcher finally + executor finally 두 번 release → count=0이지만,
          on_success 호출 시점(693~702행 사이)에는 count=1이 남아 있어
          다른 스레드가 lock을 획득할 수 없음.
        - 수정 후: watcher는 락을 직접 acquire/release하지 않음.
          executor.run()이 락의 단독 owner → on_finally 전에 executor가 release.
        """
        lock = threading.RLock()

        def get_session_lock(ts):
            return lock

        lock_free_in_on_finally = []

        watcher = _make_watcher(tmp_path, get_session_lock=get_session_lock)

        tracked = TrackedCard(
            card_id="card_double_lock",
            card_name="Double Lock Card",
            card_url="",
            list_id="list_test",
            list_key="test",
            thread_ts="thread_double_lock",
            channel_id="C12345",
            detected_at="2026-01-01T00:00:00",
        )

        done = threading.Event()

        def on_finally():
            # on_finally 호출 시점에 다른 스레드에서 락 획득 가능한지 확인
            # 수정 후에는 watcher가 락을 직접 잡지 않으므로
            # on_finally 이전에 이미 executor가 release했어야 함
            result_holder = []
            def try_acquire():
                acquired = lock.acquire(blocking=False)
                result_holder.append(acquired)
                if acquired:
                    lock.release()
            t = threading.Thread(target=try_acquire)
            t.start()
            t.join(timeout=1.0)
            if result_holder:
                lock_free_in_on_finally.append(result_holder[0])
            done.set()

        watcher._spawn_claude_thread(
            prompt="test",
            thread_ts="thread_double_lock",
            channel="C12345",
            tracked=tracked,
            on_finally=on_finally,
        )

        done.wait(timeout=5.0)

        assert lock_free_in_on_finally, "on_finally가 호출되지 않았습니다"
        # 수정 후: on_finally 호출 전(혹은 시점)에 watcher가 락을 잡지 않아야 함
        # → 다른 스레드에서 락을 획득할 수 있어야 함
        assert lock_free_in_on_finally[0] is True, (
            "on_finally 호출 시점에 다른 스레드가 락을 획득할 수 없습니다 (버그 1·2)\n"
            "watcher가 락을 직접 acquire/release하면 executor와 이중 관리됩니다."
        )


class TestListRunOnSuccessLockOrder:
    """리스트 정주행에서 on_success 콜백과 락 해제 순서 테스트

    버그 3의 실제 발현 시나리오:
    - remote 모드에서 executor.run()이 락을 보유한 채로 완료
    - watcher도 락을 보유한 상태에서 on_success() 호출
    - on_success 내부의 새 스레드가 같은 락 획득 시도 → 블로킹
    """

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_lock_state_after_on_success_with_real_lock(self, tmp_path, mock_plugin_sdk):
        """실제 락과 _spawn_claude_thread를 사용할 때 on_success 시 락이 해제되어 있어야 함

        버그 3의 전체 시나리오:
        - _process_list_run_card → _spawn_claude_thread(on_success=...) 호출
        - _spawn_claude_thread 내부의 run_claude()가 lock.acquire()
        - on_success()가 lock.release() 전에 호출 → 버그
        - on_success 내부에서 새 스레드가 같은 thread_ts lock 획득 시도 → 실패
        """
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung_plugins.trello.list_runner import ListRunner, SessionStatus

        mock_trello = MagicMock()
        list_runner = ListRunner(data_dir=tmp_path)
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.return_value = {"ts": "thread_123"}

        real_lock = threading.RLock()
        other_thread_result_in_on_success = []

        def get_session_lock(ts):
            return real_lock

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            
            list_runner_ref=lambda: list_runner,
            get_session_lock=get_session_lock,
        )
        watcher._preemptive_compact = MagicMock()

        card = TrelloCard(
            id="card_a", name="Card A", desc="",
            url="https://trello.com/c/a", list_id="list_plan", labels=[],
        )
        mock_trello.get_card.return_value = card
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True

        session = list_runner.create_session(
            list_id="list_plan",
            list_name="Plan List",
            card_ids=["card_a"],
        )

        original_spawn = watcher._spawn_claude_thread

        def intercepting_spawn(*, prompt, thread_ts, channel,
                               tracked, dm_channel_id=None, dm_thread_ts=None,
                               on_success=None, on_error=None, on_finally=None):
            def wrapped_on_success():
                acquired_flag = [None]
                lock_checked = threading.Event()
                def check_from_real_thread():
                    acquired_flag[0] = real_lock.acquire(blocking=False)
                    if acquired_flag[0]:
                        real_lock.release()
                    lock_checked.set()
                t = threading.Thread(target=check_from_real_thread)
                t.start()
                lock_checked.wait(timeout=1.0)
                other_thread_result_in_on_success.append(acquired_flag[0])
                if on_success:
                    on_success()
            original_spawn(
                prompt=prompt,
                thread_ts=thread_ts,
                channel=channel,
                tracked=tracked,
                dm_channel_id=dm_channel_id,
                dm_thread_ts=dm_thread_ts,
                on_success=wrapped_on_success,
                on_error=on_error,
                on_finally=on_finally,
            )

        watcher._spawn_claude_thread = intercepting_spawn

        watcher._process_list_run_card(session.session_id, "thread_123")

        assert other_thread_result_in_on_success, \
            "_spawn_claude_thread에 on_success가 전달되지 않았습니다"
        assert other_thread_result_in_on_success[0] is True, (
            "on_success 호출 시점에 다른 스레드가 lock을 획득할 수 없습니다 (버그 3)\n"
            "락 해제 후에 on_success()를 호출해야 합니다."
        )


class TestSpawnClaudeThreadDmInfo:
    """_spawn_claude_thread가 DM 정보를 soulstream.run()에 전달하는지 검증"""

    def test_dm_info_passed_to_soulstream_run(self, tmp_path, mock_plugin_sdk):
        """dm_channel_id, dm_thread_ts가 soulstream.run()에 전달됨"""
        import time

        watcher = _make_watcher(tmp_path)

        tracked = TrackedCard(
            card_id="card_dm",
            card_name="DM Card",
            card_url="",
            list_id="list_test",
            list_key="test",
            thread_ts="thread_dm",
            channel_id="C12345",
            detected_at="2026-01-01T00:00:00",
        )

        watcher._spawn_claude_thread(
            prompt="test prompt",
            thread_ts="thread_dm",
            channel="C12345",
            tracked=tracked,
            dm_channel_id="D999",
            dm_thread_ts="8888.0001",
        )

        # 스레드 완료 대기
        time.sleep(0.5)

        # soulstream.run()이 호출되었는지 확인
        mock_soulstream = mock_plugin_sdk["soulstream"]
        mock_soulstream.run.assert_called_once()

        call_kwargs = mock_soulstream.run.call_args
        # kwargs로 전달되었는지 확인
        assert call_kwargs.kwargs.get("dm_channel_id") == "D999"
        assert call_kwargs.kwargs.get("dm_thread_ts") == "8888.0001"

    def test_no_dm_info_when_not_provided(self, tmp_path, mock_plugin_sdk):
        """dm_channel_id, dm_thread_ts가 None이면 None으로 전달"""
        import time

        watcher = _make_watcher(tmp_path)

        tracked = TrackedCard(
            card_id="card_no_dm",
            card_name="No DM Card",
            card_url="",
            list_id="list_test",
            list_key="test",
            thread_ts="thread_no_dm",
            channel_id="C12345",
            detected_at="2026-01-01T00:00:00",
        )

        watcher._spawn_claude_thread(
            prompt="test",
            thread_ts="thread_no_dm",
            channel="C12345",
            tracked=tracked,
        )

        time.sleep(0.5)

        mock_soulstream = mock_plugin_sdk["soulstream"]
        mock_soulstream.run.assert_called_once()

        call_kwargs = mock_soulstream.run.call_args
        assert call_kwargs.kwargs.get("dm_channel_id") is None
        assert call_kwargs.kwargs.get("dm_thread_ts") is None


class TestAdaptivePolling:
    """_run() 메인 루프의 adaptive polling 로직 테스트"""

    def test_poll_returns_false_when_no_new_cards(self, tmp_path):
        """새 카드가 없으면 _poll()이 False를 반환"""
        mock_trello = MagicMock()
        mock_trello.get_cards_in_list.return_value = []
        mock_trello.get_lists.return_value = []

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"watch_lists": {"to_go": "list_togo"}},
        )
        assert watcher._poll() is False

    def test_poll_returns_true_when_new_cards_found(self, tmp_path, mock_plugin_sdk):
        """새 카드가 있으면 _poll()이 True를 반환"""
        from seosoyoung_plugins.trello.client import TrelloCard

        mock_trello = MagicMock()
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True
        mock_trello.get_lists.return_value = []

        card = TrelloCard(
            id="new_card_1",
            name="New Card",
            desc="",
            url="https://trello.com/c/new1",
            list_id="list_togo",
            labels=[],
        )
        mock_trello.get_cards_in_list.return_value = [card]

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={
                "watch_lists": {"to_go": "list_togo"},
                "list_ids": {"in_progress": "list_ip"},
            },
        )
        assert watcher._poll() is True

    def test_poll_returns_false_when_paused(self, tmp_path):
        """일시 중단 상태면 _poll()이 False를 반환"""
        watcher = _make_watcher(tmp_path)
        watcher.pause()
        assert watcher._poll() is False

    def test_burst_config_defaults(self, tmp_path):
        """burst 설정이 config에 없으면 기본값 사용"""
        watcher = _make_watcher(tmp_path)
        assert watcher.burst_interval == 3
        assert watcher.max_burst_count == 5

    def test_burst_config_custom(self, tmp_path):
        """config에서 burst 설정을 커스텀할 수 있음"""
        watcher = _make_watcher(
            tmp_path,
            config={
                "burst_interval": 2,
                "max_burst_count": 10,
            },
        )
        assert watcher.burst_interval == 2
        assert watcher.max_burst_count == 10

    def test_run_uses_burst_interval_when_cards_found(self, tmp_path):
        """새 카드 감지 시 burst_interval로 대기

        _run()의 adaptive polling 로직을 직접 검증:
        - 첫 폴링: 새 카드 발견 → burst 모드 진입
        - 두 번째 폴링: 새 카드 없음 → burst 모드 종료
        - 세 번째 이후: 정상 간격
        """
        poll_results = [True, False]
        poll_index = [0]
        wait_times = []

        watcher = _make_watcher(
            tmp_path,
            config={
                "burst_interval": 2,
                "max_burst_count": 5,
                "poll_interval": 15,
            },
        )

        # _poll을 모킹하여 새 카드 감지를 시뮬레이션
        def mock_poll():
            idx = min(poll_index[0], len(poll_results) - 1)
            poll_index[0] += 1
            return poll_results[idx]

        watcher._poll = mock_poll

        # _stop_event.wait를 모킹하여 대기 시간 기록
        original_wait = watcher._stop_event.wait

        def recording_wait(timeout=None):
            wait_times.append(timeout)
            watcher._stop_event.set()  # 루프 종료
            return True

        watcher._stop_event.wait = recording_wait
        watcher._loop = asyncio.new_event_loop()

        try:
            watcher._run()
        finally:
            if watcher._loop and not watcher._loop.is_closed():
                watcher._loop.close()

        # 첫 폴링에서 새 카드 발견 → burst_interval(2초)로 대기
        assert len(wait_times) >= 1
        assert wait_times[0] == 2

    def test_run_uses_normal_interval_when_no_cards(self, tmp_path):
        """새 카드가 없으면 정상 간격으로 대기"""
        wait_times = []

        watcher = _make_watcher(
            tmp_path,
            config={
                "burst_interval": 2,
                "max_burst_count": 5,
                "poll_interval": 15,
            },
        )

        watcher._poll = lambda: False

        def recording_wait(timeout=None):
            wait_times.append(timeout)
            watcher._stop_event.set()
            return True

        watcher._stop_event.wait = recording_wait
        watcher._loop = asyncio.new_event_loop()

        try:
            watcher._run()
        finally:
            if watcher._loop and not watcher._loop.is_closed():
                watcher._loop.close()

        assert len(wait_times) >= 1
        assert wait_times[0] == 15

    def test_burst_mode_exits_after_max_count(self, tmp_path):
        """burst 최대 횟수에 도달하면 정상 간격으로 복귀"""
        wait_times = []
        poll_count = [0]

        watcher = _make_watcher(
            tmp_path,
            config={
                "burst_interval": 1,
                "max_burst_count": 3,
                "poll_interval": 15,
            },
        )

        # 매번 새 카드가 있는 것처럼 반환
        def always_found():
            poll_count[0] += 1
            return True

        watcher._poll = always_found

        def recording_wait(timeout=None):
            wait_times.append(timeout)
            # burst 3회 + 정상 1회 후 종료
            if len(wait_times) >= 4:
                watcher._stop_event.set()
            return len(wait_times) >= 4

        watcher._stop_event.wait = recording_wait
        watcher._loop = asyncio.new_event_loop()

        try:
            watcher._run()
        finally:
            if watcher._loop and not watcher._loop.is_closed():
                watcher._loop.close()

        # burst 3회: [1, 1, 1], 그 후 burst 소진 → burst_remaining 리셋 → 다시 진입
        # 최소 3회는 burst_interval이어야 함
        burst_waits = [t for t in wait_times[:3]]
        assert all(t == 1 for t in burst_waits), f"Expected burst intervals of 1, got {burst_waits}"


class TestParallelCardHandling:
    """_handle_new_cards 병렬 처리 테스트"""

    def test_single_card_handled_directly(self, tmp_path, mock_plugin_sdk):
        """카드가 1개면 executor 없이 직접 처리"""
        from seosoyoung_plugins.trello.client import TrelloCard

        mock_trello = MagicMock()
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"list_ids": {"in_progress": "list_ip"}},
        )

        card = TrelloCard(
            id="single_card",
            name="Single Card",
            desc="",
            url="https://trello.com/c/single",
            list_id="list_togo",
            labels=[],
        )

        with patch.object(watcher, "_handle_new_card") as mock_handle:
            watcher._handle_new_cards([(card, "to_go")])
            mock_handle.assert_called_once_with(card, "to_go")

    def test_multiple_cards_all_handled(self, tmp_path, mock_plugin_sdk):
        """여러 카드가 모두 처리됨"""
        from seosoyoung_plugins.trello.client import TrelloCard
        import concurrent.futures

        mock_trello = MagicMock()
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"list_ids": {"in_progress": "list_ip"}},
        )
        # executor를 설정하여 병렬 처리 활성화
        watcher._card_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="test-card"
        )

        cards = [
            TrelloCard(
                id=f"card_{i}", name=f"Card {i}", desc="",
                url=f"https://trello.com/c/{i}", list_id="list_togo",
                labels=[],
            )
            for i in range(3)
        ]

        handled_ids = []
        original_handle = watcher._handle_new_card

        def tracking_handle(card, list_key):
            handled_ids.append(card.id)

        watcher._handle_new_card = tracking_handle

        try:
            watcher._handle_new_cards([(c, "to_go") for c in cards])
        finally:
            watcher._card_executor.shutdown(wait=True)

        assert sorted(handled_ids) == ["card_0", "card_1", "card_2"]

    def test_executor_handles_individual_card_failure(self, tmp_path, mock_plugin_sdk):
        """한 카드 처리가 실패해도 다른 카드는 정상 처리"""
        from seosoyoung_plugins.trello.client import TrelloCard
        import concurrent.futures

        watcher = _make_watcher(tmp_path)
        watcher._card_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="test-card"
        )

        handled_ids = []
        call_count = [0]

        def failing_handle(card, list_key):
            call_count[0] += 1
            if card.id == "card_fail":
                raise RuntimeError("Simulated failure")
            handled_ids.append(card.id)

        watcher._handle_new_card = failing_handle

        cards = [
            TrelloCard(id="card_ok_1", name="OK 1", desc="", url="", list_id="l", labels=[]),
            TrelloCard(id="card_fail", name="Fail", desc="", url="", list_id="l", labels=[]),
            TrelloCard(id="card_ok_2", name="OK 2", desc="", url="", list_id="l", labels=[]),
        ]

        try:
            # 예외가 전파되지 않아야 함
            watcher._handle_new_cards([(c, "to_go") for c in cards])
        finally:
            watcher._card_executor.shutdown(wait=True)

        # 실패한 카드 외 나머지는 처리됨
        assert sorted(handled_ids) == ["card_ok_1", "card_ok_2"]
        assert call_count[0] == 3

    def test_no_executor_falls_back_to_sequential(self, tmp_path, mock_plugin_sdk):
        """executor가 없으면 순차 처리 (테스트 환경)"""
        from seosoyoung_plugins.trello.client import TrelloCard

        watcher = _make_watcher(tmp_path)
        watcher._card_executor = None  # executor 없음

        handled = []

        def tracking_handle(card, list_key):
            handled.append(card.id)

        watcher._handle_new_card = tracking_handle

        cards = [
            TrelloCard(id=f"card_{i}", name=f"Card {i}", desc="", url="", list_id="l", labels=[])
            for i in range(3)
        ]

        watcher._handle_new_cards([(c, "to_go") for c in cards])

        assert handled == ["card_0", "card_1", "card_2"]


class TestStateLockProtection:
    """_state_lock을 통한 공유 상태 보호 테스트"""

    def test_watcher_has_state_lock(self, tmp_path):
        """TrelloWatcher에 _state_lock이 존재함"""
        watcher = _make_watcher(tmp_path)
        assert hasattr(watcher, "_state_lock")
        assert isinstance(watcher._state_lock, type(threading.Lock()))

    def test_untrack_card_is_thread_safe(self, tmp_path):
        """_untrack_card가 동시 호출에서 안전함"""
        watcher = _make_watcher(tmp_path)

        tracked = TrackedCard(
            card_id="safe_card",
            card_name="Safe Card",
            card_url="",
            list_id="l",
            list_key="to_go",
            thread_ts="ts",
            channel_id="C",
            detected_at=datetime.now().isoformat(),
        )
        watcher._tracked["safe_card"] = tracked

        results = []

        def try_untrack():
            try:
                watcher._untrack_card("safe_card")
                results.append("success")
            except Exception as e:
                results.append(f"error: {e}")

        threads = [threading.Thread(target=try_untrack) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # 모두 성공해야 하고 (이미 없는 경우도 성공), 카드는 반드시 제거
        assert "safe_card" not in watcher._tracked
        assert all(r == "success" for r in results)

    def test_concurrent_poll_and_handle_safe(self, tmp_path, mock_plugin_sdk):
        """_poll과 _handle_new_card가 동시에 실행되어도 _tracked 상태가 일관됨"""
        from seosoyoung_plugins.trello.client import TrelloCard

        mock_trello = MagicMock()
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True
        mock_trello.get_lists.return_value = []

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={
                "watch_lists": {"to_go": "list_togo"},
                "list_ids": {"in_progress": "list_ip"},
            },
        )

        # 폴링할 때마다 새 카드 1개씩 반환
        poll_counter = [0]

        def dynamic_cards(list_id):
            poll_counter[0] += 1
            return [TrelloCard(
                id=f"card_{poll_counter[0]}",
                name=f"Card {poll_counter[0]}",
                desc="",
                url=f"https://trello.com/c/{poll_counter[0]}",
                list_id=list_id,
                labels=[],
            )]

        mock_trello.get_cards_in_list.side_effect = dynamic_cards

        errors = []

        def safe_poll():
            try:
                watcher._poll()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=safe_poll) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"동시 폴링에서 오류 발생: {errors}"


class TestWatcherStartStop:
    """start/stop 시 executor 라이프사이클 테스트"""

    def test_start_creates_executor(self, tmp_path):
        """start()가 _card_executor를 생성"""
        mock_trello = MagicMock()
        mock_trello.is_configured.return_value = True

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
        )
        assert watcher._card_executor is None

        watcher.start()
        assert watcher._card_executor is not None

        watcher.stop()
        assert watcher._card_executor is None

    def test_max_card_workers_config(self, tmp_path):
        """max_card_workers 설정이 적용됨"""
        watcher = _make_watcher(
            tmp_path,
            config={"max_card_workers": 5},
        )
        assert watcher._max_card_workers == 5

    def test_max_card_workers_default(self, tmp_path):
        """max_card_workers 기본값은 3"""
        watcher = _make_watcher(tmp_path)
        assert watcher._max_card_workers == 3


class TestUntrackCardCleansThreadCards:
    """_untrack_card가 _thread_cards도 함께 정리하는지 테스트"""

    def test_untrack_removes_thread_card_mapping(self, tmp_path):
        """_untrack_card가 _tracked와 _thread_cards 양쪽에서 카드를 제거"""
        from seosoyoung_plugins.trello.watcher import ThreadCardInfo

        watcher = _make_watcher(tmp_path)

        tracked = TrackedCard(
            card_id="card_cleanup",
            card_name="Cleanup Card",
            card_url="https://trello.com/c/cleanup",
            list_id="list_test",
            list_key="to_go",
            thread_ts="ts_cleanup",
            channel_id="C12345",
            detected_at="2026-01-01T00:00:00",
        )
        watcher._tracked["card_cleanup"] = tracked
        watcher._register_thread_card(tracked)

        # 사전 조건: 양쪽 모두 등록되어 있어야 함
        assert "card_cleanup" in watcher._tracked
        assert "ts_cleanup" in watcher._thread_cards

        watcher._untrack_card("card_cleanup")

        # 양쪽 모두 제거되어야 함
        assert "card_cleanup" not in watcher._tracked
        assert "ts_cleanup" not in watcher._thread_cards

    def test_untrack_persists_thread_cards_removal(self, tmp_path):
        """_untrack_card가 _thread_cards 변경을 파일에 저장"""
        watcher = _make_watcher(tmp_path)

        tracked = TrackedCard(
            card_id="card_persist",
            card_name="Persist Card",
            card_url="https://trello.com/c/persist",
            list_id="list_test",
            list_key="to_go",
            thread_ts="ts_persist",
            channel_id="C12345",
            detected_at="2026-01-01T00:00:00",
        )
        watcher._tracked["card_persist"] = tracked
        watcher._register_thread_card(tracked)

        watcher._untrack_card("card_persist")

        # 새 인스턴스로 로드해도 _thread_cards에 남아있지 않아야 함
        watcher2 = _make_watcher(tmp_path)
        assert "ts_persist" not in watcher2._thread_cards

    def test_untrack_nonexistent_card_is_safe(self, tmp_path):
        """존재하지 않는 카드를 untrack해도 에러 없음"""
        watcher = _make_watcher(tmp_path)

        # _thread_cards에 직접 항목을 넣어두고
        from seosoyoung_plugins.trello.watcher import ThreadCardInfo
        watcher._thread_cards["ts_orphan"] = ThreadCardInfo(
            thread_ts="ts_orphan",
            channel_id="C12345",
            card_id="card_orphan",
            card_name="Orphan",
            card_url="",
        )

        # 존재하지 않는 card_id로 untrack — 에러 없이 통과해야 함
        watcher._untrack_card("nonexistent_card")

        # orphan은 그대로 (다른 card_id이므로)
        assert "ts_orphan" in watcher._thread_cards

    def test_untrack_does_not_remove_other_thread_cards(self, tmp_path):
        """한 카드를 untrack해도 다른 카드의 thread_card 매핑은 유지"""
        watcher = _make_watcher(tmp_path)

        tracked_a = TrackedCard(
            card_id="card_a", card_name="Card A",
            card_url="", list_id="l", list_key="to_go",
            thread_ts="ts_a", channel_id="C12345",
            detected_at="2026-01-01T00:00:00",
        )
        tracked_b = TrackedCard(
            card_id="card_b", card_name="Card B",
            card_url="", list_id="l", list_key="to_go",
            thread_ts="ts_b", channel_id="C12345",
            detected_at="2026-01-01T00:00:00",
        )
        watcher._tracked["card_a"] = tracked_a
        watcher._tracked["card_b"] = tracked_b
        watcher._register_thread_card(tracked_a)
        watcher._register_thread_card(tracked_b)

        watcher._untrack_card("card_a")

        assert "card_a" not in watcher._tracked
        assert "ts_a" not in watcher._thread_cards
        # card_b는 그대로 유지
        assert "card_b" in watcher._tracked
        assert "ts_b" in watcher._thread_cards


class TestLoadThreadCardsFieldValidation:
    """_load_thread_cards 필드 검증 테스트"""

    def test_missing_card_url_gets_default(self, tmp_path):
        """card_url 필드가 없는 데이터 로드 시 빈 문자열로 보완"""
        import json

        thread_cards_file = tmp_path / "thread_cards.json"
        thread_cards_file.write_text(json.dumps({
            "ts_1": {
                "thread_ts": "ts_1",
                "channel_id": "C12345",
                "card_id": "card_1",
                "card_name": "Card 1",
                # card_url 누락
            }
        }), encoding="utf-8")

        watcher = _make_watcher(tmp_path)

        assert "ts_1" in watcher._thread_cards
        assert watcher._thread_cards["ts_1"].card_url == ""

    def test_missing_multiple_fields_get_defaults(self, tmp_path):
        """여러 필드가 누락된 데이터도 정상 로드"""
        import json

        thread_cards_file = tmp_path / "thread_cards.json"
        thread_cards_file.write_text(json.dumps({
            "ts_2": {
                "thread_ts": "ts_2",
                "channel_id": "C12345",
                "card_id": "card_2",
                # card_name, card_url, session_id, has_execute, created_at 누락
            }
        }), encoding="utf-8")

        watcher = _make_watcher(tmp_path)

        assert "ts_2" in watcher._thread_cards
        info = watcher._thread_cards["ts_2"]
        assert info.card_name == ""
        assert info.card_url == ""
        assert info.session_id is None
        assert info.has_execute is False
        assert info.created_at == ""

    def test_complete_data_loads_normally(self, tmp_path):
        """모든 필드가 있는 데이터는 정상 로드"""
        import json

        thread_cards_file = tmp_path / "thread_cards.json"
        thread_cards_file.write_text(json.dumps({
            "ts_3": {
                "thread_ts": "ts_3",
                "channel_id": "C12345",
                "card_id": "card_3",
                "card_name": "Complete Card",
                "card_url": "https://trello.com/c/complete",
                "session_id": "session_abc",
                "has_execute": True,
                "created_at": "2026-01-01T00:00:00",
            }
        }), encoding="utf-8")

        watcher = _make_watcher(tmp_path)

        info = watcher._thread_cards["ts_3"]
        assert info.card_name == "Complete Card"
        assert info.card_url == "https://trello.com/c/complete"
        assert info.session_id == "session_abc"
        assert info.has_execute is True


class TestHandleNewCardRollback:
    """_handle_new_card 실패 시 카드 롤백 테스트"""

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_claude_failure_rolls_back_card(self, tmp_path, mock_plugin_sdk):
        """Claude 실행 실패 시 카드가 원래 리스트로 롤백됨"""
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus

        mock_plugin_sdk["soulstream"].run = AsyncMock(
            return_value=RunResult(
                ok=False,
                status=RunStatus.FAILED,
                error="Rate limit exceeded",
            )
        )

        mock_trello = MagicMock()
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"list_ids": {"in_progress": "list_inprogress"}},
        )
        # _loop를 새 이벤트 루프로 설정 (closed 방지)
        watcher._loop = asyncio.new_event_loop()

        card = TrelloCard(
            id="card_rollback",
            name="Rollback Card",
            desc="",
            url="https://trello.com/c/rollback",
            list_id="list_togo",
            labels=[],
        )

        try:
            watcher._handle_new_card(card, "to_go")
        finally:
            watcher._loop.close()

        # move_card 호출 내역 확인:
        # 1회: In Progress로 이동
        # 2회: 원래 리스트(list_togo)로 롤백
        move_calls = mock_trello.move_card.call_args_list
        assert len(move_calls) >= 2
        # 마지막 move_card는 원래 리스트로의 롤백이어야 함
        rollback_call = move_calls[-1]
        assert rollback_call[0] == ("card_rollback", "list_togo")

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_claude_success_does_not_rollback(self, tmp_path, mock_plugin_sdk):
        """Claude 실행 성공 시 롤백이 발생하지 않음"""
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus

        mock_plugin_sdk["soulstream"].run = AsyncMock(
            return_value=RunResult(
                ok=True,
                status=RunStatus.COMPLETED,
                session_id="session_abc",
            )
        )

        mock_trello = MagicMock()
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"list_ids": {"in_progress": "list_inprogress"}},
        )
        watcher._loop = asyncio.new_event_loop()

        card = TrelloCard(
            id="card_success",
            name="Success Card",
            desc="",
            url="https://trello.com/c/success",
            list_id="list_togo",
            labels=[],
        )

        try:
            watcher._handle_new_card(card, "to_go")
        finally:
            watcher._loop.close()

        # move_card는 In Progress 이동 1회만 호출되어야 함
        move_calls = mock_trello.move_card.call_args_list
        assert len(move_calls) == 1
        assert move_calls[0][0] == ("card_success", "list_inprogress")

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_claude_exception_rolls_back_card(self, tmp_path, mock_plugin_sdk):
        """Claude 실행 중 예외 발생 시에도 카드가 원래 리스트로 롤백됨"""
        from seosoyoung_plugins.trello.client import TrelloCard

        mock_plugin_sdk["soulstream"].run = AsyncMock(
            side_effect=RuntimeError("Connection lost")
        )

        mock_trello = MagicMock()
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"list_ids": {"in_progress": "list_inprogress"}},
        )
        watcher._loop = asyncio.new_event_loop()

        card = TrelloCard(
            id="card_exception",
            name="Exception Card",
            desc="",
            url="https://trello.com/c/exception",
            list_id="list_togo",
            labels=[],
        )

        try:
            watcher._handle_new_card(card, "to_go")
        finally:
            watcher._loop.close()

        # 롤백 호출 확인
        move_calls = mock_trello.move_card.call_args_list
        assert len(move_calls) >= 2
        rollback_call = move_calls[-1]
        assert rollback_call[0] == ("card_exception", "list_togo")


class TestDeferredCardMove:
    """버그 1: 카드 이동이 Claude 실행 직전까지 지연되는지 검증"""

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_card_not_moved_before_spawn(self, tmp_path, mock_plugin_sdk):
        """_handle_new_card가 _spawn_claude_thread 전에 move_card를 호출하지 않음

        on_start 콜백이 soulstream.run() 직전에만 카드를 이동해야 한다.
        _handle_new_card 본문에서 직접 move_card를 호출하면 안 된다.
        """
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus

        move_call_order = []

        mock_trello = MagicMock()
        mock_trello.update_card_name.return_value = True

        def track_move_card(card_id, list_id):
            move_call_order.append(("move_card", card_id, list_id))
            return True
        mock_trello.move_card.side_effect = track_move_card

        original_run = mock_plugin_sdk["soulstream"].run

        async def tracking_run(*args, **kwargs):
            move_call_order.append(("soulstream.run",))
            return RunResult(ok=True, status=RunStatus.COMPLETED, session_id="s1")
        mock_plugin_sdk["soulstream"].run = AsyncMock(side_effect=tracking_run)

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"list_ids": {"in_progress": "list_inprogress"}},
        )
        watcher._loop = asyncio.new_event_loop()

        card = TrelloCard(
            id="card_deferred",
            name="Deferred Move Card",
            desc="",
            url="https://trello.com/c/deferred",
            list_id="list_togo",
            labels=[],
        )

        try:
            watcher._handle_new_card(card, "to_go")
        finally:
            watcher._loop.close()

        # move_card(In Progress)가 soulstream.run() 직전에 호출되었는지 확인
        # 순서: ("move_card", ..., "list_inprogress") → ("soulstream.run",)
        move_indices = [
            i for i, entry in enumerate(move_call_order)
            if entry[0] == "move_card" and entry[2] == "list_inprogress"
        ]
        run_indices = [
            i for i, entry in enumerate(move_call_order)
            if entry[0] == "soulstream.run"
        ]

        assert len(move_indices) == 1, f"In Progress 이동이 정확히 1회여야 함, 실제: {move_indices}"
        assert len(run_indices) == 1, f"soulstream.run이 정확히 1회여야 함, 실제: {run_indices}"
        assert move_indices[0] < run_indices[0], (
            f"move_card가 soulstream.run 전에 호출되어야 함: "
            f"move={move_indices[0]}, run={run_indices[0]}"
        )

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_card_tracked_before_spawn(self, tmp_path, mock_plugin_sdk):
        """카드가 _tracked에 등록된 후에 _spawn_claude_thread가 호출됨

        다음 폴링에서 같은 카드가 중복 처리되지 않도록,
        _tracked 등록은 _spawn_claude_thread 이전에 완료되어야 한다.
        """
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus

        tracked_at_run_time = {}

        async def check_tracked_run(*args, **kwargs):
            tracked_at_run_time["snapshot"] = dict(watcher._tracked)
            return RunResult(ok=True, status=RunStatus.COMPLETED, session_id="s2")
        mock_plugin_sdk["soulstream"].run = AsyncMock(side_effect=check_tracked_run)

        mock_trello = MagicMock()
        mock_trello.move_card.return_value = True
        mock_trello.update_card_name.return_value = True

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"list_ids": {"in_progress": "list_inprogress"}},
        )
        watcher._loop = asyncio.new_event_loop()

        card = TrelloCard(
            id="card_tracked_order",
            name="Track Order Card",
            desc="",
            url="https://trello.com/c/trackorder",
            list_id="list_togo",
            labels=[],
        )

        try:
            watcher._handle_new_card(card, "to_go")
        finally:
            watcher._loop.close()

        # soulstream.run 호출 시점에 카드가 이미 _tracked에 있어야 함
        assert "card_tracked_order" in tracked_at_run_time["snapshot"]

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_no_move_when_in_progress_list_not_configured(self, tmp_path, mock_plugin_sdk):
        """in_progress 리스트가 설정되지 않으면 move_card가 호출되지 않음"""
        from seosoyoung_plugins.trello.client import TrelloCard
        from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus

        mock_plugin_sdk["soulstream"].run = AsyncMock(
            return_value=RunResult(ok=True, status=RunStatus.COMPLETED, session_id="s3")
        )

        mock_trello = MagicMock()
        mock_trello.update_card_name.return_value = True

        watcher = _make_watcher(
            tmp_path,
            trello_client=mock_trello,
            config={"list_ids": {"in_progress": None}},
        )
        watcher._loop = asyncio.new_event_loop()

        card = TrelloCard(
            id="card_no_move",
            name="No Move Card",
            desc="",
            url="https://trello.com/c/nomove",
            list_id="list_togo",
            labels=[],
        )

        try:
            watcher._handle_new_card(card, "to_go")
        finally:
            watcher._loop.close()

        mock_trello.move_card.assert_not_called()


class TestOnStartCallback:
    """_spawn_claude_thread의 on_start 콜백 테스트"""

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_on_start_called_before_soulstream_run(self, tmp_path, mock_plugin_sdk):
        """on_start가 soulstream.run() 직전에 호출됨"""
        from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus

        call_order = []

        def on_start():
            call_order.append("on_start")

        async def tracking_run(*args, **kwargs):
            call_order.append("soulstream.run")
            return RunResult(ok=True, status=RunStatus.COMPLETED, session_id="s")
        mock_plugin_sdk["soulstream"].run = AsyncMock(side_effect=tracking_run)

        watcher = _make_watcher(tmp_path)
        watcher._loop = asyncio.new_event_loop()

        tracked = TrackedCard(
            card_id="c1", card_name="Test", card_url="https://trello.com/c/t",
            list_id="l1", list_key="to_go", thread_ts="1234.5678",
            channel_id="C123", detected_at="2026-01-01T00:00:00",
        )

        try:
            watcher._spawn_claude_thread(
                prompt="test", thread_ts="1234.5678", channel="C123",
                tracked=tracked, on_start=on_start,
            )
        finally:
            watcher._loop.close()

        assert call_order == ["on_start", "soulstream.run"]

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_on_start_exception_does_not_block_run(self, tmp_path, mock_plugin_sdk):
        """on_start에서 예외가 발생해도 soulstream.run()은 실행됨"""
        from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus

        mock_plugin_sdk["soulstream"].run = AsyncMock(
            return_value=RunResult(ok=True, status=RunStatus.COMPLETED, session_id="s")
        )

        def on_start():
            raise RuntimeError("on_start error")

        watcher = _make_watcher(tmp_path)
        watcher._loop = asyncio.new_event_loop()

        tracked = TrackedCard(
            card_id="c1", card_name="Test", card_url="https://trello.com/c/t",
            list_id="l1", list_key="to_go", thread_ts="1234.5678",
            channel_id="C123", detected_at="2026-01-01T00:00:00",
        )

        try:
            watcher._spawn_claude_thread(
                prompt="test", thread_ts="1234.5678", channel="C123",
                tracked=tracked, on_start=on_start,
            )
        finally:
            watcher._loop.close()

        # on_start 실패에도 불구하고 soulstream.run이 호출되어야 함
        mock_plugin_sdk["soulstream"].run.assert_called_once()

    @patch("seosoyoung_plugins.trello.watcher.threading.Thread", _SyncThread)
    def test_no_on_start_is_fine(self, tmp_path, mock_plugin_sdk):
        """on_start가 None이면 건너뛰고 정상 실행"""
        from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus

        mock_plugin_sdk["soulstream"].run = AsyncMock(
            return_value=RunResult(ok=True, status=RunStatus.COMPLETED, session_id="s")
        )

        watcher = _make_watcher(tmp_path)
        watcher._loop = asyncio.new_event_loop()

        tracked = TrackedCard(
            card_id="c1", card_name="Test", card_url="https://trello.com/c/t",
            list_id="l1", list_key="to_go", thread_ts="1234.5678",
            channel_id="C123", detected_at="2026-01-01T00:00:00",
        )

        try:
            watcher._spawn_claude_thread(
                prompt="test", thread_ts="1234.5678", channel="C123",
                tracked=tracked,
            )
        finally:
            watcher._loop.close()

        mock_plugin_sdk["soulstream"].run.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
