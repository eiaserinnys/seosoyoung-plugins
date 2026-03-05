"""번역 모듈

Anthropic 또는 OpenAI API를 호출하여 번역합니다.
backend 파라미터에 따라 분기합니다.

이 모듈은 Config에 의존하지 않습니다.
모든 설정은 호출 시 명시적 파라미터로 전달받습니다.
"""

import logging
import anthropic
import openai

from seosoyoung_plugins.translate.detector import Language
from seosoyoung_plugins.translate.glossary import find_relevant_terms_v2, GlossaryMatchResult
from seosoyoung_plugins.translate.slack_escape import escape_slack_markup, unescape_slack_markup

logger = logging.getLogger(__name__)


def _build_context_text(context_messages: list[dict]) -> str:
    """이전 대화 컨텍스트를 텍스트로 변환

    컨텍스트는 번역 대상이 아니라 참고용이지만,
    슬랙 마크업이 프롬프트를 혼란시킬 수 있으므로 이스케이프합니다.
    복원은 불필요합니다.

    Args:
        context_messages: 이전 메시지 목록 [{"user": "이름", "text": "내용"}, ...]

    Returns:
        컨텍스트 텍스트
    """
    if not context_messages:
        return ""

    lines = ["<previous_messages>"]
    for msg in context_messages:
        escaped_text, _ = escape_slack_markup(msg["text"])
        lines.append(f"[{msg['user']}]: {escaped_text}")
    lines.append("</previous_messages>")
    return "\n".join(lines)


def _build_glossary_section(
    text: str,
    source_lang: Language,
    glossary_path: str,
) -> tuple[str, list[tuple[str, str]], GlossaryMatchResult | None]:
    """텍스트에서 관련 용어를 찾아 용어집 섹션 생성

    Args:
        text: 번역할 텍스트
        source_lang: 원본 언어
        glossary_path: 용어집 파일 경로

    Returns:
        (용어집 섹션 문자열, 참고한 용어 목록, 매칭 결과 객체)
        용어가 없으면 ("", [], None)
    """
    lang_code = "ko" if source_lang == Language.KOREAN else "en"
    match_result = find_relevant_terms_v2(text, lang_code, glossary_path=glossary_path)

    if not match_result.matched_terms:
        return "", [], match_result

    lines = ["<glossary>", "Translate the following proper nouns as specified:"]
    for source_term, target_term in match_result.matched_terms:
        lines.append(f"- {source_term} → {target_term}")
    lines.append("</glossary>")

    return "\n".join(lines), match_result.matched_terms, match_result


def _build_prompt(
    text: str,
    source_lang: Language,
    glossary_path: str,
    context_messages: list[dict] | None = None,
) -> tuple[str, list[tuple[str, str]], GlossaryMatchResult | None, dict[str, str]]:
    """번역 프롬프트 생성

    슬랙 마크업(링크, 멘션, 이모지, 코드 등)을 플레이스홀더로 치환한 뒤
    프롬프트에 삽입합니다. 치환 맵은 번역 후 복원에 사용됩니다.

    Args:
        text: 번역할 텍스트
        source_lang: 원본 언어
        glossary_path: 용어집 파일 경로
        context_messages: 이전 대화 컨텍스트

    Returns:
        (프롬프트 문자열, 참고한 용어 목록, 매칭 결과 객체, 슬랙 마크업 치환 맵)
    """
    target_lang = "English" if source_lang == Language.KOREAN else "Korean"

    # 슬랙 마크업 이스케이프 (용어집 매칭 전에 원본 텍스트 사용)
    escaped_text, slack_replacements = escape_slack_markup(text)

    # 컨텍스트 섹션
    context_text = ""
    if context_messages:
        context_text = _build_context_text(context_messages) + "\n\n"

    # 용어집 섹션 (원본 텍스트로 매칭하여 고유명사를 정확히 찾음)
    glossary_section, glossary_terms, match_result = _build_glossary_section(
        text, source_lang, glossary_path
    )
    glossary_text = glossary_section + "\n\n" if glossary_section else ""

    # 플레이스홀더 보존 지시 (플레이스홀더가 있을 때만 추가)
    placeholder_instruction = ""
    if slack_replacements:
        placeholder_instruction = (
            "Preserve all placeholders like [[LINK1]], [[MENTION1]], "
            "[[BROADCAST1]], [[EMOJI1]], [[CODE1]] exactly as they are. "
            "Do not translate, modify, or remove them.\n"
        )

    prompt = (
        f"{context_text}{glossary_text}"
        f"Translate the following text to {target_lang}.\n"
        f"Output ONLY the translation, nothing else. "
        f"No explanations, no quotes, no prefixes.\n"
        f"{placeholder_instruction}\n"
        f"Text to translate:\n"
        f"{escaped_text}"
    )
    return prompt, glossary_terms, match_result, slack_replacements


# 모델별 가격 (USD per 1M tokens)
MODEL_PRICING = {
    # Anthropic
    "claude-3-5-haiku-latest": {"input": 0.80, "output": 4.00},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-latest": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-opus-20240229": {"input": 15.00, "output": 75.00},
    # OpenAI
    "gpt-5-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}

# 기본 가격 (알 수 없는 모델용)
DEFAULT_PRICING = {"input": 3.00, "output": 15.00}


def _calculate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """토큰 사용량으로 비용을 계산합니다.

    Args:
        input_tokens: 입력 토큰 수
        output_tokens: 출력 토큰 수
        model: 사용한 모델명

    Returns:
        예상 비용 (USD)
    """
    pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost


def _translate_anthropic(prompt: str, model: str, api_key: str) -> tuple[str, int, int]:
    """Anthropic API로 번역

    Returns:
        (번역된 텍스트, 입력 토큰 수, 출력 토큰 수)
    """
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    translated = response.content[0].text.strip()
    return translated, response.usage.input_tokens, response.usage.output_tokens


def _translate_openai(prompt: str, model: str, api_key: str) -> tuple[str, int, int]:
    """OpenAI API로 번역

    Returns:
        (번역된 텍스트, 입력 토큰 수, 출력 토큰 수)
    """
    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=2048,
    )
    translated = response.choices[0].message.content.strip()
    input_tokens = response.usage.prompt_tokens
    output_tokens = response.usage.completion_tokens
    return translated, input_tokens, output_tokens


def translate(
    text: str,
    source_lang: Language,
    *,
    backend: str,
    model: str,
    api_key: str,
    glossary_path: str,
    context_messages: list[dict] | None = None,
) -> tuple[str, float, list[tuple[str, str]], GlossaryMatchResult | None]:
    """텍스트를 번역

    Args:
        text: 번역할 텍스트
        source_lang: 원본 언어
        backend: 번역 백엔드 ("anthropic" | "openai")
        model: 사용할 모델명
        api_key: API 키
        glossary_path: 용어집 파일 경로
        context_messages: 이전 대화 컨텍스트

    Returns:
        (번역된 텍스트, 예상 비용 USD, 참고한 용어 목록, 매칭 결과 객체)

    Raises:
        ValueError: 잘못된 backend
    """
    prompt, glossary_terms, match_result, slack_replacements = _build_prompt(
        text, source_lang, glossary_path, context_messages
    )

    logger.debug(f"번역 요청 [{backend}]: {text[:50]}... -> {source_lang.value}")

    if backend == "openai":
        translated, input_tokens, output_tokens = _translate_openai(prompt, model, api_key)
    elif backend == "anthropic":
        translated, input_tokens, output_tokens = _translate_anthropic(prompt, model, api_key)
    else:
        raise ValueError(f"Unknown backend: {backend}")

    # 슬랙 마크업 복원
    translated = unescape_slack_markup(translated, slack_replacements)

    cost = _calculate_cost(input_tokens, output_tokens, model)

    logger.debug(f"번역 완료 [{backend}]: {translated[:50]}... (비용: ${cost:.6f})")

    return translated, cost, glossary_terms, match_result
