import asyncio
from unittest.mock import AsyncMock

import pytest

from seosoyoung_plugins.channel_observer.knowledge_context import (
    build_knowledge_context_item,
)


@pytest.mark.asyncio
async def test_keyword_search_dedupes_excludes_slack_and_prioritizes_library():
    results = {
        "GPU": [
            {
                "card_id": "knowledge-1",
                "title": "GPU 일반 메모",
                "snippet": "<b>GPU</b> 공급",
                "node_path": ["지식", "하드웨어"],
                "source_ref": "https://example.test/gpu",
            },
            {
                "card_id": "slack-1",
                "title": "슬랙 원문",
                "snippet": "제외",
                "node_path": ["슬랙 대화", "C1"],
            },
        ],
        "추론": [
            {
                "card_id": "library-1",
                "title": "서재 발행글",
                "snippet": "추론 비용 &amp; 품질",
                "node_path": ["서소영의 서재", "발행 이력"],
            },
            {
                "card_id": "knowledge-1",
                "title": "중복 카드",
                "snippet": "중복",
                "node_path": ["지식"],
            },
        ],
        "비용": [
            {
                "card_id": "knowledge-2",
                "title": "비용 메모",
                "snippet": "비용 비교",
                "node_path": ["지식", "AI"],
            },
            {
                "card_id": "knowledge-3",
                "title": "네 번째",
                "snippet": "상위 3건 밖",
                "node_path": ["지식", "AI"],
            },
        ],
    }

    async def search(keyword):
        return results[keyword]

    item = await build_knowledge_context_item(
        ["GPU", "추론", "비용"], search, timeout=0.2
    )

    assert item is not None
    assert item["key"] == "knowledge_context"
    lines = item["content"].splitlines()
    assert lines[2].startswith("- [서재 발행글]")
    assert "슬랙 원문" not in item["content"]
    assert item["content"].count("GPU 일반 메모") == 1
    assert "<b>" not in item["content"]
    assert "추론 비용 & 품질" in item["content"]
    assert "네 번째" not in item["content"]
    assert len([line for line in lines if line.startswith("- ")]) == 3


@pytest.mark.asyncio
async def test_partial_success_survives_per_keyword_failure_and_timeout():
    async def search(keyword):
        if keyword == "error":
            raise RuntimeError("atom error")
        if keyword == "slow":
            await asyncio.sleep(1)
        return [
            {
                "card_id": "ok-1",
                "title": "즉시 결과",
                "snippet": "살아남음",
                "node_path": ["지식"],
            }
        ]

    item = await build_knowledge_context_item(
        ["ok", "error", "slow"], search, timeout=0.03
    )

    assert item is not None
    assert "즉시 결과" in item["content"]


@pytest.mark.asyncio
async def test_zero_results_omits_context_item():
    item = await build_knowledge_context_item(
        ["없음"], AsyncMock(return_value=[]), timeout=0.1
    )
    assert item is None


@pytest.mark.asyncio
async def test_invalid_search_callback_is_fail_open():
    def invalid_search(_keyword):
        return []

    item = await build_knowledge_context_item(
        ["하나", "둘", "셋"], invalid_search, timeout=0.1
    )
    assert item is None
