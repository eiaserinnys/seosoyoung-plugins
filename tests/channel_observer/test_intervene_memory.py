import asyncio
import json
import logging
import sys

import pytest

from seosoyoung_plugins.channel_observer import intervene_memory
from seosoyoung_plugins.channel_observer.intervene_memory import (
    InterveneMemory,
    parse_record_tags,
    render_channel_memory_item,
    render_member_facts_item,
)


class FakeProcess:
    def __init__(
        self,
        stdout: bytes = b"{}",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.hang = hang
        self.killed = False
        self._release = asyncio.Event()

    async def communicate(self):
        if self.hang and not self.killed:
            await self._release.wait()
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True
        self._release.set()


def patch_subprocess(monkeypatch, process):
    calls = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(
        intervene_memory.asyncio, "create_subprocess_exec", fake_create_subprocess_exec,
    )
    return calls


@pytest.fixture
def scripts_dir(tmp_path):
    for name in (
        "load_channel.py",
        "load_member.py",
        "record_utterance.py",
        "load_thread_context.py",
    ):
        (tmp_path / name).write_text("print('{}')\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_load_channel_memory_runs_script_with_configured_python(
    monkeypatch, scripts_dir,
):
    process = FakeProcess(stdout=b'{"previousSummary": "s"}')
    calls = patch_subprocess(monkeypatch, process)

    memory = InterveneMemory(scripts_dir)
    result = await memory.load_channel_memory("C123")

    assert result == {"previousSummary": "s"}
    args, kwargs = calls[0]
    assert args[0] == sys.executable
    assert args[1] == str(scripts_dir / "load_channel.py")
    assert args[2] == "C123"
    assert kwargs["stderr"] is asyncio.subprocess.PIPE


@pytest.mark.asyncio
async def test_script_failure_and_bad_json_return_none_with_warning(
    monkeypatch, scripts_dir, caplog,
):
    memory = InterveneMemory(scripts_dir)

    patch_subprocess(monkeypatch, FakeProcess(stdout=b"", stderr=b"boom", returncode=2))
    with caplog.at_level(logging.WARNING):
        assert await memory.load_channel_memory("C123") is None
    assert any("exit 2" in rec.message and "boom" in rec.message for rec in caplog.records)

    caplog.clear()
    patch_subprocess(monkeypatch, FakeProcess(stdout=b"not json"))
    with caplog.at_level(logging.WARNING):
        assert await memory.load_channel_memory("C123") is None
    assert any("bad JSON" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_script_timeout_kills_process_and_returns_none(
    monkeypatch, scripts_dir, caplog,
):
    monkeypatch.setattr(intervene_memory, "CHANNEL_TIMEOUT", 0.01)
    process = FakeProcess(hang=True)
    patch_subprocess(monkeypatch, process)

    with caplog.at_level(logging.WARNING):
        result = await InterveneMemory(scripts_dir).load_channel_memory("C123")

    assert result is None
    assert process.killed is True
    assert any("timeout" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_missing_script_file_skips_without_spawning(monkeypatch, tmp_path, caplog):
    calls = patch_subprocess(monkeypatch, FakeProcess())

    with caplog.at_level(logging.WARNING):
        result = await InterveneMemory(tmp_path).load_channel_memory("C123")

    assert result is None
    assert calls == []
    assert any("script missing" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_load_member_facts_skips_empty_ids_and_passes_ids(monkeypatch, scripts_dir):
    process = FakeProcess(stdout=b'{"members": []}')
    calls = patch_subprocess(monkeypatch, process)
    memory = InterveneMemory(scripts_dir)

    assert await memory.load_member_facts("C123", []) is None
    assert calls == []

    await memory.load_member_facts("C123", ["U1", "", "U2"])
    args, _ = calls[0]
    assert args[2:] == ("C123", "U1", "U2")


@pytest.mark.asyncio
async def test_record_utterance_writes_file_and_uses_first_line_fallback(
    monkeypatch, scripts_dir, tmp_path,
):
    calls = patch_subprocess(
        monkeypatch, FakeProcess(stdout=b'{"status": "recorded"}'),
    )

    recorded = await InterveneMemory(scripts_dir).record_utterance(
        channel_id="C123",
        body="첫 줄입니다\n둘째 줄",
        slug="20260913-132401",
        topics=[],
        summary_line=None,
        compressed_summary=None,
        channel_memory={"needsCompression": False},
        output_dir=tmp_path,
    )

    assert recorded is True
    utterance_file = tmp_path / "utterance-C123-20260913-132401.txt"
    assert utterance_file.read_text(encoding="utf-8") == "첫 줄입니다\n둘째 줄"
    args, _ = calls[0]
    assert args[1] == str(scripts_dir / "record_utterance.py")
    assert args[2:] == (
        "--channel", "C123",
        "--utterance-file", str(utterance_file.resolve()),
        "--summary-line", "첫 줄입니다",
    )


@pytest.mark.asyncio
async def test_record_utterance_compression_and_deleted_cards(
    monkeypatch, scripts_dir, tmp_path,
):
    calls = patch_subprocess(
        monkeypatch, FakeProcess(stdout=b'{"status": "recorded"}'),
    )

    recorded = await InterveneMemory(scripts_dir).record_utterance(
        channel_id="C123",
        body="본문",
        slug="slug1",
        topics=["주제1", "주제2"],
        summary_line="요약",
        compressed_summary="압축된 줄거리",
        channel_memory={
            "needsCompression": True,
            "oldestTen": [{"cardId": "card-1"}, {"cardId": "card-2"}],
        },
        output_dir=tmp_path,
    )

    assert recorded is True
    compressed_file = tmp_path / "compressed-C123-slug1.txt"
    assert compressed_file.read_text(encoding="utf-8") == "압축된 줄거리"
    args, _ = calls[0]
    assert "--topics" in args and args[args.index("--topics") + 1] == "주제1,주제2"
    assert args[args.index("--summary-line") + 1] == "요약"
    assert args[args.index("--compressed-summary") + 1] == str(
        compressed_file.resolve()
    )
    assert args[args.index("--deleted-cards") + 1] == "card-1,card-2"


@pytest.mark.asyncio
async def test_record_utterance_ignores_compression_without_need(
    monkeypatch, scripts_dir, tmp_path,
):
    calls = patch_subprocess(
        monkeypatch, FakeProcess(stdout=b'{"status": "recorded"}'),
    )

    await InterveneMemory(scripts_dir).record_utterance(
        channel_id="C123",
        body="본문",
        slug="slug1",
        topics=[],
        summary_line="요약",
        compressed_summary="압축된 줄거리",
        channel_memory={"needsCompression": False, "oldestTen": [{"cardId": "c"}]},
        output_dir=tmp_path,
    )

    args, _ = calls[0]
    assert "--compressed-summary" not in args
    assert "--deleted-cards" not in args
    assert not (tmp_path / "compressed-C123-slug1.txt").exists()


@pytest.mark.asyncio
async def test_record_utterance_returns_false_on_script_error(
    monkeypatch, scripts_dir, tmp_path,
):
    patch_subprocess(monkeypatch, FakeProcess(stdout=b'{"status": "error"}'))

    recorded = await InterveneMemory(scripts_dir).record_utterance(
        channel_id="C123",
        body="본문",
        slug="slug1",
        topics=[],
        summary_line="요약",
        compressed_summary=None,
        channel_memory=None,
        output_dir=tmp_path,
    )

    assert recorded is False


@pytest.mark.asyncio
async def test_record_utterance_disabled_without_scripts_dir(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        recorded = await InterveneMemory(None).record_utterance(
            channel_id="C123",
            body="본문",
            slug="slug1",
            topics=[],
            summary_line=None,
            compressed_summary=None,
            channel_memory=None,
            output_dir=tmp_path,
        )

    assert recorded is False
    assert list(tmp_path.iterdir()) == []
    assert caplog.records == []


@pytest.mark.asyncio
async def test_archive_threads_passes_channel_and_file(monkeypatch, scripts_dir):
    calls = patch_subprocess(monkeypatch, FakeProcess(stdout=b'{"archived": 1}'))

    archived = await InterveneMemory(scripts_dir).archive_threads(
        "C123", scripts_dir / "threads.json",
    )

    assert archived is True
    args, _ = calls[0]
    assert args[1] == str(scripts_dir / "load_thread_context.py")
    assert args[2:] == (
        "--channel", "C123",
        "--threads-file", str(scripts_dir / "threads.json"),
    )


def test_from_config_uses_explicit_dir(scripts_dir):
    memory = InterveneMemory.from_config({"intervene_scripts_dir": str(scripts_dir)})
    assert memory.enabled is True


def test_from_config_uses_agent_common_files_dir(monkeypatch, tmp_path):
    scripts = tmp_path / "skills" / "channel-intervene" / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setenv("AGENT_COMMON_FILES_DIR", str(tmp_path))

    memory = InterveneMemory.from_config({})

    assert memory.enabled is True
    assert memory._scripts_dir == scripts


def test_from_config_missing_dir_disables_with_single_warning(monkeypatch, caplog):
    monkeypatch.delenv("AGENT_COMMON_FILES_DIR", raising=False)

    with caplog.at_level(logging.WARNING):
        memory = InterveneMemory.from_config({"intervene_scripts_dir": "/no/such/dir"})

    assert memory.enabled is False
    assert sum("intervene memory disabled" in rec.message for rec in caplog.records) == 1


@pytest.mark.asyncio
async def test_spawn_background_reports_exception(caplog):
    async def boom():
        raise RuntimeError("nope")

    with caplog.at_level(logging.WARNING):
        task = intervene_memory.spawn_background(boom())
        for _ in range(3):
            await asyncio.sleep(0)

    assert any("background task failed" in rec.message for rec in caplog.records)
    assert task not in intervene_memory._background_tasks


def test_render_channel_memory_item_without_compression():
    item = render_channel_memory_item(
        {
            "previousSummary": "지난 줄거리",
            "recentUtterances": [
                {"title": "첫 발화", "topics": ["주제1", "주제2"]},
                {"title": "둘째 발화", "topics": []},
            ],
            "topicsToAvoid": ["민감한 화제"],
            "needsCompression": False,
            "oldestTen": [{"cardId": "c1", "title": "무시", "content": "무시"}],
        }
    )

    assert item == {
        "key": "channel_memory",
        "label": "채널 기억",
        "content": (
            "## 채널 기억\n"
            "### 지난 줄거리\n"
            "지난 줄거리\n"
            "### 최근 내 발화 (최신순)\n"
            "- 첫 발화 [주제1, 주제2]\n"
            "- 둘째 발화 []\n"
            "### 피할 화제\n"
            "민감한 화제"
        ),
    }


def test_render_channel_memory_item_with_compression_and_empty_fields():
    item = render_channel_memory_item(
        {
            "previousSummary": "",
            "recentUtterances": [],
            "topicsToAvoid": [],
            "needsCompression": True,
            "oldestTen": [{"cardId": "c1", "title": "오래된1", "content": "내용1"}],
        }
    )

    assert "(없음)" in item["content"]
    assert "### 압축 필요" in item["content"]
    assert "- 오래된1: 내용1" in item["content"]


def test_render_member_facts_item_limits_and_missing_facts():
    item = render_member_facts_item(
        {
            "members": [
                {
                    "slackId": "U1",
                    "displayName": "주복",
                    "isBot": False,
                    "cacheStatus": "hit",
                    "keyFacts": ["f1", "f2", "f3", "f4", "f5", "x" * 150, "f7"],
                },
                {
                    "slackId": "U2",
                    "displayName": "봇",
                    "isBot": True,
                    "cacheStatus": "miss-refreshed",
                    "keyFacts": [],
                },
                {"slackId": "U3", "cacheStatus": "not_found"},
                {"slackId": "U4", "cacheStatus": "error"},
            ]
        }
    )

    assert item is not None
    content = item["content"]
    assert item["key"] == "member_facts"
    assert "- 주복 (U1): f1 / f2 / f3 / f4 / f5 / " + "x" * 120 in content
    assert "x" * 121 not in content
    assert "f7" not in content
    assert "- 봇 (U2, 봇): " in content
    assert "- U3: (정보 없음)" in content
    assert "- U4: (정보 없음)" in content


def test_render_member_facts_item_without_members_returns_none():
    assert render_member_facts_item(None) is None
    assert render_member_facts_item({"members": []}) is None


def test_parse_record_tags_returns_empty_for_missing_output():
    assert parse_record_tags(None) == ([], None, None)
    assert parse_record_tags("태그 없음") == ([], None, None)


def test_parse_record_tags_reads_first_match_and_limits_topics():
    output = (
        "<utterance>본문</utterance>\n"
        "<topics>a, b, , c, d, e, f</topics>\n"
        "<summary_line> 요약 </summary_line>\n"
        "<compressed_summary>압축</compressed_summary>\n"
        "<topics>무시</topics>"
    )

    assert parse_record_tags(output) == (
        ["a", "b", "c", "d", "e"],
        "요약",
        "압축",
    )
