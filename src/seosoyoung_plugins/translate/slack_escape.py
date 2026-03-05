"""Slack 마크업 이스케이프/언이스케이프 모듈

번역 프롬프트에 슬랙 마크업이 그대로 들어가면 LLM이 혼란스러워하므로,
마크업 요소를 번호 매긴 플레이스홀더로 치환한 뒤 번역 후 복원합니다.

처리 대상:
- 코드 블록 (```) 및 인라인 코드 (`)
- 링크 (<URL|text>, <URL>)
- 유저 멘션 (<@U...>)
- 브로드캐스트 (<!here>, <!channel>, <!everyone>, <!subteam^...>)
- 채널 링크 (<#C...>)
- 커스텀 이모지 (:name:)

코드 블록을 가장 먼저 처리하여 내부 마크업이 이중 이스케이프되지 않도록 합니다.
"""

import re


# 코드 블록 (```...```) — 가장 먼저 처리
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")

# 인라인 코드 (`...`) — 코드 블록 이후에 처리
_INLINE_CODE_RE = re.compile(r"`[^`]+`")

# 슬랙 유저 멘션: <@U...>
_USER_MENTION_RE = re.compile(r"<@U[A-Z0-9]+>")

# 슬랙 브로드캐스트 멘션: <!here>, <!channel>, <!everyone>, <!subteam^...|...>
_BROADCAST_RE = re.compile(r"<!(?:here|channel|everyone|subteam\^[^>]+)>")

# 슬랙 링크: <URL|text> 또는 <URL> 또는 <#C...|name>
# URL은 http/https/mailto 로 시작, 채널은 #C로 시작
_LINK_RE = re.compile(r"<(?:https?://|mailto:|#C)[^>]+>")

# 커스텀 이모지: :name: (영문, 숫자, 하이픈, 언더스코어)
_EMOJI_RE = re.compile(r":([a-z0-9][a-z0-9_+-]*(?:-[a-z0-9_+-]*)*):")


def escape_slack_markup(text: str) -> tuple[str, dict[str, str]]:
    """슬랙 마크업을 플레이스홀더로 치환합니다.

    Args:
        text: 슬랙 메시지 원본 텍스트

    Returns:
        (이스케이프된 텍스트, 플레이스홀더 -> 원본 매핑)
    """
    if not text:
        return text, {}

    replacements: dict[str, str] = {}
    counters: dict[str, int] = {}

    def _make_placeholder(type_name: str, original: str) -> str:
        count = counters.get(type_name, 0) + 1
        counters[type_name] = count
        placeholder = f"[[{type_name}{count}]]"
        replacements[placeholder] = original
        return placeholder

    # 1. 코드 블록 (```) — 내부 마크업 보호를 위해 가장 먼저 처리
    text = _CODE_BLOCK_RE.sub(
        lambda m: _make_placeholder("CODE", m.group(0)), text
    )

    # 2. 인라인 코드 (`) — 코드 블록 이후
    text = _INLINE_CODE_RE.sub(
        lambda m: _make_placeholder("CODE", m.group(0)), text
    )

    # 3. 유저 멘션 (<@U...>)
    text = _USER_MENTION_RE.sub(
        lambda m: _make_placeholder("MENTION", m.group(0)), text
    )

    # 4. 브로드캐스트 (<!here> 등)
    text = _BROADCAST_RE.sub(
        lambda m: _make_placeholder("BROADCAST", m.group(0)), text
    )

    # 5. 링크 (<URL|text>, <URL>, <#C...|name>)
    text = _LINK_RE.sub(
        lambda m: _make_placeholder("LINK", m.group(0)), text
    )

    # 6. 커스텀 이모지 (:name:)
    text = _EMOJI_RE.sub(
        lambda m: _make_placeholder("EMOJI", m.group(0)), text
    )

    return text, replacements


def unescape_slack_markup(text: str, replacements: dict[str, str]) -> str:
    """플레이스홀더를 원본 슬랙 마크업으로 복원합니다.

    Args:
        text: 플레이스홀더가 포함된 텍스트 (번역 결과)
        replacements: escape_slack_markup()이 반환한 매핑

    Returns:
        원본 마크업이 복원된 텍스트
    """
    for placeholder, original in replacements.items():
        text = text.replace(placeholder, original)
    return text
