"""T-G12-C: cross-import 회귀 안전망 — plugin_sdk와 soul_common helper 시그니처 정합.

R-4 fix(2026-05-11, atom G-12): plugin_sdk가 soul_common을 *직접 import하지 않고*
build_bot_caller_info를 *동등 구현*으로 노출 (plugin이 host 내부 모듈 모름, §1 정합).

본 회귀 테스트가 두 정본의 *시그니처 + 반환 dict + SYSTEM_PORTRAIT_BASE 상수*가 정합한지
확인한다. 두 정본 중 한쪽이 drift하면 *명시적으로* 실패한다 (§4 명시적 실패).

cross-import는 test-only — plugin prod 코드는 plugin_sdk만 사용. seosoyoung-plugins
pyproject.toml `pythonpath`에 `"../soulstream/packages/soul-common/src"` test-only 추가
(prod plugin import 영향 0).
"""

import inspect

from seosoyoung.plugin_sdk.caller_info import (
    SYSTEM_PORTRAIT_BASE as PLUGIN_SDK_PORTRAIT_BASE,
    build_bot_caller_info as plugin_sdk_build_bot_caller_info,
)
from soul_common.auth.caller_info import (
    SYSTEM_PORTRAIT_BASE as SOUL_COMMON_PORTRAIT_BASE,
    build_bot_caller_info as soul_common_build_bot_caller_info,
)


class TestPortraitBaseConstantParity:
    """SYSTEM_PORTRAIT_BASE 상수가 양 정본에서 정합."""

    def test_same_value(self):
        assert PLUGIN_SDK_PORTRAIT_BASE == SOUL_COMMON_PORTRAIT_BASE

    def test_value_is_server_relative(self):
        """`/api/system/portraits` — orch-server 라우트 prefix."""
        assert PLUGIN_SDK_PORTRAIT_BASE == "/api/system/portraits"


class TestBuildBotCallerInfoSignatureParity:
    """build_bot_caller_info 시그니처 정합 — keyword-only 인자·기본값·반환 타입."""

    def test_keyword_only_args_match(self):
        """source/display_name keyword-only, agent_node default None — 양쪽 정합."""
        plugin_sig = inspect.signature(plugin_sdk_build_bot_caller_info)
        soul_sig = inspect.signature(soul_common_build_bot_caller_info)

        # 파라미터 이름 + kind + default 비교
        plugin_params = {
            name: (p.kind, p.default)
            for name, p in plugin_sig.parameters.items()
        }
        soul_params = {
            name: (p.kind, p.default)
            for name, p in soul_sig.parameters.items()
        }
        assert plugin_params == soul_params

    def test_required_kwargs(self):
        """source/display_name 필수 — positional 호출 TypeError 양쪽 동일."""
        for fn in (
            plugin_sdk_build_bot_caller_info,
            soul_common_build_bot_caller_info,
        ):
            try:
                fn("channel_observer", "채널 관찰자")  # type: ignore[misc]
            except TypeError:
                continue
            raise AssertionError(
                f"{fn.__module__}.{fn.__name__}이 positional TypeError를 일으켜야 한다"
            )


class TestBuildBotCallerInfoReturnDictParity:
    """build_bot_caller_info 반환 dict가 양 정본에서 *바이트 단위 동일* (R-4 G-12)."""

    def test_minimal_args_dict_equal(self):
        """source + display_name만 — 반환 dict 정합 (agent_node 키 부재)."""
        plugin_result = plugin_sdk_build_bot_caller_info(
            source="channel_observer",
            display_name="채널 관찰자",
        )
        soul_result = soul_common_build_bot_caller_info(
            source="channel_observer",
            display_name="채널 관찰자",
        )
        assert plugin_result == soul_result

    def test_with_agent_node_dict_equal(self):
        """agent_node 포함 — 반환 dict 정합."""
        plugin_result = plugin_sdk_build_bot_caller_info(
            source="trello_watcher",
            display_name="트렐로 워처",
            agent_node="eias-shopping",
        )
        soul_result = soul_common_build_bot_caller_info(
            source="trello_watcher",
            display_name="트렐로 워처",
            agent_node="eias-shopping",
        )
        assert plugin_result == soul_result

    def test_agent_node_falsy_omitted_both(self):
        """agent_node=None / "" → 양쪽 키 부재 (graceful, §9 대칭)."""
        for falsy in (None, ""):
            plugin_result = plugin_sdk_build_bot_caller_info(
                source="channel_observer",
                display_name="채널 관찰자",
                agent_node=falsy,
            )
            soul_result = soul_common_build_bot_caller_info(
                source="channel_observer",
                display_name="채널 관찰자",
                agent_node=falsy,
            )
            assert "agent_node" not in plugin_result
            assert "agent_node" not in soul_result
            assert plugin_result == soul_result
