"""용어집 로더 모듈

번역 시 고유명사 일관성을 위해 glossary.yaml을 로드하고 파싱합니다.
kiwipiepy를 활용하여 한국어 형태소 분석 기반 용어 매칭을 수행합니다.

이 모듈은 Config에 의존하지 않습니다.
glossary_path는 호출 시 명시적 파라미터로 전달받습니다.
"""

import logging
import re
from pathlib import Path
from dataclasses import dataclass, field

import yaml

# 영어 불용어 (관사, 전치사, 접속사 등) - 모든 영어 단어에 적용
ENGLISH_STOPWORDS = frozenset({
    # 관사
    "a", "an", "the",
    # 전치사
    "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
    "into", "onto", "upon", "about", "above", "below", "between", "among",
    "through", "during", "before", "after", "over", "under", "around",
    # 접속사
    "and", "or", "but", "nor", "yet", "so",
    # 대명사
    "it", "its", "this", "that", "these", "those",
    # 기타 기능어
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "must", "can",
})

# 확장 영어 불용어 (고빈도 일반 단어) - 소문자 시작 단어에만 적용
# 대문자 시작 단어(고유명사 후보)는 이 필터를 우회함
# 용어집(en_index)에 있는 단어는 _get_effective_stopwords()에서 자동 제외됨
EXTENDED_COMMON_WORDS = frozenset({
    # 일반 동사
    "get", "got", "set", "let", "put", "run", "say", "said",
    "see", "saw", "seen", "come", "came", "take", "took", "taken",
    "give", "gave", "given", "make", "made", "know", "knew", "known",
    "think", "thought", "find", "found", "tell", "told", "ask", "asked",
    "work", "worked", "seem", "seemed", "feel", "felt", "try", "tried",
    "leave", "left", "call", "called", "need", "needed", "keep", "kept",
    "start", "started", "show", "showed", "hear", "heard", "play", "played",
    "move", "moved", "live", "lived", "believe", "bring", "brought",
    "happen", "happened", "write", "wrote", "written", "provide", "provided",
    "sit", "sat", "stand", "stood", "lose", "lost", "pay", "paid",
    "meet", "met", "include", "included", "continue", "continued",
    "learn", "learned", "change", "changed", "lead", "led",
    "understand", "understood", "watch", "watched", "follow", "followed",
    "stop", "stopped", "create", "created", "speak", "spoke", "spoken",
    "read", "allow", "allowed", "add", "added", "spend", "spent",
    "grow", "grew", "grown", "open", "opened", "walk", "walked",
    "win", "won", "offer", "offered", "remember", "remembered",
    "appear", "appeared", "buy", "bought", "wait", "waited",
    "serve", "served", "die", "died", "send", "sent",
    "expect", "expected", "build", "built", "stay", "stayed",
    "fall", "fell", "fallen", "cut", "reach", "reached",
    "kill", "killed", "remain", "remained", "suggest", "suggested",
    "raise", "raised", "pass", "passed", "sell", "sold",
    "require", "required", "report", "reported", "decide", "decided",
    "pull", "pulled", "develop", "developed", "use", "used",
    "turn", "turned", "hold", "held", "help", "helped",
    "want", "wanted", "look", "looked", "going", "become", "became",
    "carry", "carried", "pick", "picked", "cause", "caused",
    "support", "supported", "consider", "considered", "cover", "covered",
    "claim", "claimed", "note", "noted", "miss", "missed",
    "present", "presented", "close", "closed", "break", "broke", "broken",
    "drive", "drove", "driven", "eat", "ate", "eaten",
    "describe", "described", "return", "returned", "agree", "agreed",
    "hang", "hung", "check", "checked", "mean", "meant",
    "enjoy", "enjoyed", "handle", "handled", "apply", "applied",
    "receive", "received", "step", "stepped", "form", "formed",
    "state", "stated", "base", "based", "contain", "contained",
    "produce", "produced", "exist", "existed", "matter",
    "deal", "dealt", "fail", "failed", "act", "acted",
    "assume", "assumed", "accept", "accepted", "involve", "involved",
    "suffer", "suffered", "draw", "drew", "drawn", "wish", "wished",
    "save", "saved", "plan", "planned", "demand", "demanded",
    "compare", "compared", "protect", "protected", "drop", "dropped",
    "manage", "managed", "figure", "figured", "press", "pressed",
    "rise", "rose", "risen", "fight", "fought", "push", "pushed",
    # 일반 명사
    "time", "year", "years", "people", "way", "ways", "day", "days",
    "man", "men", "woman", "women", "child", "children",
    "world", "life", "hand", "hands", "part", "parts",
    "place", "places", "case", "cases", "week", "weeks",
    "company", "system", "program", "question", "questions",
    "government", "number", "numbers", "night", "nights",
    "point", "points", "home", "water", "room", "rooms",
    "mother", "area", "areas", "money", "story", "stories",
    "fact", "facts", "month", "months", "lot", "lots",
    "right", "rights", "study", "book", "books", "eye", "eyes",
    "job", "jobs", "word", "words", "business", "issue", "issues",
    "side", "sides", "kind", "kinds", "head", "heads",
    "house", "houses", "friend", "friends",
    "father", "power", "hour", "hours", "game", "games",
    "line", "lines", "end", "ends", "member", "members",
    "law", "laws", "car", "cars", "city", "cities",
    "name", "names", "team", "teams", "minute", "minutes",
    "idea", "ideas", "body", "bodies", "information",
    "parent", "parents", "face", "faces",
    "level", "levels", "office", "door", "doors",
    "health", "person", "persons", "art", "arts",
    "war", "wars", "history", "party", "parties",
    "result", "results", "morning", "mornings",
    "reason", "reasons", "research", "girl", "girls",
    "guy", "guys", "moment", "moments", "air",
    "teacher", "teachers", "education",
    "foot", "feet", "boy", "boys", "age", "ages",
    "process", "music", "market", "markets",
    "sense", "product", "products", "effect", "effects",
    "class", "classes", "piece", "pieces", "ground", "grounds",
    "rule", "rules", "field", "fields", "future",
    "order", "orders", "table", "tables",
    "record", "records", "cost", "costs",
    "practice", "control", "rate", "rates",
    "summer", "center", "centers", "list", "lists",
    "type", "types", "size", "sizes", "group", "groups",
    "risk", "risks", "value", "values", "role", "roles",
    "model", "models", "position", "positions",
    "road", "roads", "sort", "sorts", "view", "views",
    # 일반 형용사
    "new", "old", "good", "bad", "great", "small", "large", "big",
    "long", "high", "low", "young", "little", "early", "late",
    "important", "few", "public", "own", "able",
    "free", "full", "real", "best", "better", "sure", "clear",
    "recent", "certain", "personal", "possible", "common", "current",
    "likely", "natural", "simple", "past", "hard", "strong", "whole",
    "similar", "general", "local", "true", "false",
    "happy", "serious", "ready", "special", "easy", "major",
    "hot", "available", "specific", "short", "single", "wide",
    "various", "different", "final", "main", "poor", "total",
    "popular", "basic", "original", "actual", "primary",
    "related", "modern", "dark", "cold", "nice", "fine", "deep",
    "entire", "former", "red", "blue", "green", "black", "white",
    "brown", "front", "due", "pretty", "extra",
    # 일반 부사
    "also", "very", "often", "however", "too", "usually", "really",
    "already", "always", "ever", "just", "then", "now", "here", "there",
    "still", "even", "again", "never", "away", "once", "quite",
    "enough", "well", "back", "actually", "rather", "almost",
    "perhaps", "maybe", "ago", "far", "together", "only",
    "sometimes", "along", "quickly", "simply", "exactly", "finally",
    "directly", "certainly", "probably", "carefully", "clearly",
    "indeed", "nearly", "recently", "suddenly", "instead",
    "especially", "generally", "alone",
    # 기타 고빈도 일반 단어
    "like", "each", "every", "both", "such", "thing", "things",
    "since", "much", "more", "most", "some", "any", "all", "many",
    "than", "same", "another", "next", "last", "first", "second",
    "third", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "hundred", "thousand", "million",
    "without", "within", "per", "several", "against", "else",
    "across", "either", "though", "less", "least",
    "down", "off", "out", "other", "core",
    "generated", "whether", "whose", "whom",
})

logger = logging.getLogger(__name__)

# kiwipiepy 싱글톤 인스턴스
_kiwi = None
_kiwi_initialized = False

# 캐시: glossary_path -> raw data
_glossary_cache: dict[str, dict] = {}
_entries_cache: dict[str, tuple[tuple[str, str], ...]] = {}
_word_index_cache: dict[str, tuple[dict, dict]] = {}
_effective_stopwords_cache: dict[str, frozenset[str]] = {}


@dataclass
class GlossaryMatchResult:
    """용어 매칭 결과"""
    matched_terms: list[tuple[str, str]]  # [(원본, 번역), ...]
    extracted_words: list[str] = field(default_factory=list)  # 추출된 단어들
    debug_info: dict = field(default_factory=dict)  # 디버그 정보


def _load_glossary_raw(glossary_path: str) -> dict:
    """glossary.yaml 파일을 로드 (캐싱)

    Args:
        glossary_path: 용어집 파일 경로

    Returns:
        파싱된 YAML 딕셔너리
    """
    if glossary_path in _glossary_cache:
        return _glossary_cache[glossary_path]

    path = Path(glossary_path)

    if not path.exists():
        logger.warning(f"용어집 파일을 찾을 수 없습니다: {path}")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _glossary_cache[glossary_path] = data
        return data
    except Exception as e:
        logger.error(f"용어집 로드 실패: {e}")
        return {}


def _extract_name_pair(item: dict) -> tuple[str, str] | None:
    """아이템에서 한국어-영어 이름 쌍 추출

    glossary.yaml 구조: item.name.kr / item.name.en

    Args:
        item: glossary 항목 (name: {kr, en} 포함)

    Returns:
        (한국어명, 영어명) 튜플 또는 None
    """
    name = item.get("name", {})
    if not isinstance(name, dict):
        return None

    kr_name = name.get("kr")
    en_name = name.get("en")

    if kr_name and en_name:
        return (str(kr_name), str(en_name))
    return None


def _extract_short_names(full_name: str) -> list[str]:
    """전체 이름에서 짧은 이름들을 추출 (사용자 사전 등록용)

    Args:
        full_name: 전체 이름 문자열

    Returns:
        추출된 짧은 이름 리스트 (전체 이름 포함)
    """
    names = [full_name]

    # 쉼표로 분리
    if "," in full_name:
        parts = [p.strip() for p in full_name.split(",")]
        names.extend(parts)

    # 괄호 제거 후 핵심 이름 추출
    name_without_paren = re.sub(r"\([^)]*\)", "", full_name).strip()
    if name_without_paren and name_without_paren != full_name:
        if "," in name_without_paren:
            parts = [p.strip() for p in name_without_paren.split(",") if p.strip()]
            names.extend(parts)
        elif name_without_paren.strip():
            names.append(name_without_paren.strip())

    # "이름 성" 패턴에서 첫 단어만 추출
    for name in list(names):
        words = name.split()
        if len(words) == 2 and len(words[0]) >= 2:
            names.append(words[0])

    # 중복 제거
    seen = set()
    result = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            result.append(name)

    return result


def get_glossary_entries(glossary_path: str) -> tuple[tuple[str, str], ...]:
    """용어집 항목들을 (한국어, 영어) 쌍으로 반환 (캐싱)

    Args:
        glossary_path: 용어집 파일 경로

    Returns:
        ((kr_name, en_name), ...) 튜플
    """
    if glossary_path in _entries_cache:
        return _entries_cache[glossary_path]

    raw_data = _load_glossary_raw(glossary_path)
    entries = []

    categories_with_items = [
        "main_characters", "ariella_variants", "bosses", "sub_bosses",
        "boss_human_era", "blessing_angels", "blessings", "npcs", "golems",
        "system_characters", "main_places", "sanctuary_places",
        "seal_structure", "concepts", "items_resources", "terminology",
    ]

    for category in categories_with_items:
        category_data = raw_data.get(category, {})
        items = category_data.get("items", [])

        for item in items:
            pair = _extract_name_pair(item)
            if pair:
                entries.append(pair)

    logger.debug(f"용어집 항목 {len(entries)}개 로드")
    result = tuple(entries)
    _entries_cache[glossary_path] = result
    return result


def _get_kiwi(glossary_path: str):
    """Kiwi 인스턴스 반환 (싱글톤, 사용자 사전 포함)"""
    global _kiwi, _kiwi_initialized

    if _kiwi_initialized:
        return _kiwi

    try:
        from kiwipiepy import Kiwi
        _kiwi = Kiwi()

        # 용어집에서 한국어 고유명사 추출하여 사용자 사전에 등록
        entries = get_glossary_entries(glossary_path)
        registered = set()

        for kr_name, _ in entries:
            # 전체 이름과 짧은 이름 모두 등록
            for name in _extract_short_names(kr_name):
                if len(name) >= 2 and name not in registered:
                    try:
                        _kiwi.add_user_word(name, 'NNP')
                        registered.add(name)
                    except Exception:
                        pass  # 이미 등록된 경우 무시

        logger.info(f"kiwipiepy 사용자 사전에 {len(registered)}개 한국어 고유명사 등록")
        _kiwi_initialized = True

    except ImportError:
        logger.warning("kiwipiepy 미설치, 단순 공백 분리 사용")
        _kiwi = None
        _kiwi_initialized = True

    return _kiwi


def _extract_korean_words(text: str, glossary_path: str) -> list[str]:
    """한국어 텍스트에서 명사 추출 (kiwipiepy 사용)

    Args:
        text: 한국어 텍스트
        glossary_path: 용어집 파일 경로

    Returns:
        추출된 명사 리스트 (2글자 이상)
    """
    kiwi = _get_kiwi(glossary_path)

    if kiwi is None:
        # kiwipiepy가 없으면 단순 공백 분리
        words = text.split()
        return [w for w in words if len(w) >= 2]

    tokens = kiwi.tokenize(text)
    nouns = []

    for token in tokens:
        # NNG: 일반명사, NNP: 고유명사, NNB: 의존명사
        if token.tag in ('NNG', 'NNP') and len(token.form) >= 2:
            nouns.append(token.form)

    return nouns


def _extract_english_words(text: str, glossary_path: str | None = None) -> list[str]:
    """영어 텍스트에서 단어 추출

    대문자 시작 단어(고유명사 후보)는 기본 불용어만 적용하고,
    소문자 시작 단어는 확장 불용어를 추가 적용하여 일반 단어 노이즈를 줄인다.

    Args:
        text: 영어 텍스트
        glossary_path: 용어집 파일 경로 (확장 불용어 교차 검증에 사용)

    Returns:
        추출된 단어 리스트 (3글자 이상, 불용어 제외)
    """
    words = re.findall(r'[A-Za-z]+', text)

    if glossary_path:
        effective_ext = _get_effective_stopwords(glossary_path)
    else:
        effective_ext = EXTENDED_COMMON_WORDS

    result = []
    for w in words:
        if len(w) < 3:
            continue
        lower = w.lower()
        # 기본 불용어(관사, 전치사, 접속사 등)는 모든 단어에 적용
        if lower in ENGLISH_STOPWORDS:
            continue
        # 확장 불용어는 소문자 시작 단어에만 적용 (대문자 시작 = 고유명사 후보)
        if w[0].islower() and lower in effective_ext:
            continue
        result.append(w)
    return result


def _build_word_index(glossary_path: str) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """용어집 역색인 구축 (단어 -> 항목 인덱스)

    영어 역색인 키는 .lower()로 정규화하여 대소문자 무관 매칭을 지원한다.

    Args:
        glossary_path: 용어집 파일 경로

    Returns:
        (한국어 역색인, 영어 역색인)
    """
    if glossary_path in _word_index_cache:
        return _word_index_cache[glossary_path]

    entries = get_glossary_entries(glossary_path)
    kr_index: dict[str, list[int]] = {}
    en_index: dict[str, list[int]] = {}

    for idx, (kr_name, en_name) in enumerate(entries):
        # 한국어: 짧은 이름들로 역색인
        for name in _extract_short_names(kr_name):
            if len(name) >= 2:
                kr_index.setdefault(name, []).append(idx)

        # 영어: 짧은 이름들로 역색인 (.lower() 정규화)
        for name in _extract_short_names(en_name):
            if len(name) >= 3:
                en_index.setdefault(name.lower(), []).append(idx)

    logger.debug(f"역색인 구축: 한국어 {len(kr_index)}개, 영어 {len(en_index)}개")
    result = (kr_index, en_index)
    _word_index_cache[glossary_path] = result
    return result


def _get_effective_stopwords(glossary_path: str) -> frozenset[str]:
    """용어집과 교차 검증된 유효 확장 불용어를 반환 (캐싱)

    EXTENDED_COMMON_WORDS에서 용어집의 영어 항목(en_index)과
    겹치는 단어를 제외한다. 예: "Haven", "Grace" 등은 일반 단어이면서
    게임 용어이므로 불용어에서 자동 제외된다.

    Args:
        glossary_path: 용어집 파일 경로

    Returns:
        유효 확장 불용어 frozenset
    """
    if glossary_path in _effective_stopwords_cache:
        return _effective_stopwords_cache[glossary_path]

    if not glossary_path:
        return EXTENDED_COMMON_WORDS

    _, en_index = _build_word_index(glossary_path)
    # en_index 키는 이미 .lower() 정규화됨
    glossary_words = set(en_index.keys())

    effective = EXTENDED_COMMON_WORDS - glossary_words

    excluded = EXTENDED_COMMON_WORDS & glossary_words
    if excluded:
        logger.debug(f"용어집 보호로 불용어에서 제외된 단어: {sorted(excluded)}")

    result = frozenset(effective)
    _effective_stopwords_cache[glossary_path] = result
    return result


def find_relevant_terms(
    text: str,
    source_lang: str,
    fuzzy_threshold: int = 80,
    *,
    glossary_path: str,
) -> list[tuple[str, str]]:
    """텍스트에서 관련 용어 추출 (하위 호환성 유지)

    Args:
        text: 검색할 텍스트
        source_lang: 원본 언어 ("ko" 또는 "en")
        fuzzy_threshold: 퍼지 매칭 임계값 (기본 80)
        glossary_path: 용어집 파일 경로

    Returns:
        [(원본 용어, 번역된 용어), ...] 리스트
    """
    result = find_relevant_terms_v2(text, source_lang, fuzzy_threshold, glossary_path=glossary_path)
    return result.matched_terms


def find_relevant_terms_v2(
    text: str,
    source_lang: str,
    fuzzy_threshold: int = 80,
    *,
    glossary_path: str,
) -> GlossaryMatchResult:
    """텍스트에서 관련 용어 추출 (개선된 버전, 디버그 정보 포함)

    알고리즘:
    1. 텍스트를 형태소 분석하여 명사 추출 (한국어) 또는 단어 분리 (영어)
    2. 추출된 단어가 용어집 항목에 포함되는지 검색
    3. 퍼지 매칭으로 유사 용어 추가 검색

    영어의 경우 역색인 키와 매칭 비교를 모두 .lower()로 정규화하여
    대소문자 무관 매칭을 수행한다.

    Args:
        text: 검색할 텍스트
        source_lang: 원본 언어 ("ko" 또는 "en")
        fuzzy_threshold: 퍼지 매칭 임계값 (기본 80)
        glossary_path: 용어집 파일 경로

    Returns:
        GlossaryMatchResult (매칭 결과, 추출된 단어, 디버그 정보)
    """
    entries = get_glossary_entries(glossary_path)
    kr_index, en_index = _build_word_index(glossary_path)

    # 언어별 단어 추출
    if source_lang == "ko":
        words = _extract_korean_words(text, glossary_path)
        word_index = kr_index
    else:
        words = _extract_english_words(text, glossary_path)
        word_index = en_index

    matched: list[tuple[str, str]] = []
    matched_indices: set[int] = set()
    exact_matches: list[str] = []
    substring_matches: list[str] = []
    fuzzy_matches: list[str] = []

    # 1단계: 역색인을 통한 정확한 단어 매칭
    for word in words:
        # 영어는 .lower()로 정규화하여 대소문자 무관 매칭
        lookup_word = word.lower() if source_lang == "en" else word
        if lookup_word in word_index:
            for idx in word_index[lookup_word]:
                if idx not in matched_indices:
                    kr_name, en_name = entries[idx]
                    source_name = kr_name if source_lang == "ko" else en_name
                    target_name = en_name if source_lang == "ko" else kr_name
                    matched.append((source_name, target_name))
                    matched_indices.add(idx)
                    exact_matches.append(f"{word} → {source_name}")

    # 2단계: 단어가 용어집 항목에 부분 포함되는지 검색
    for word in words:
        if len(word) < 2:
            continue

        for idx, (kr_name, en_name) in enumerate(entries):
            if idx in matched_indices:
                continue

            source_name = kr_name if source_lang == "ko" else en_name
            target_name = en_name if source_lang == "ko" else kr_name

            # 영어는 대소문자 무시 비교
            if source_lang == "en":
                match = word.lower() in source_name.lower()
            else:
                match = word in source_name

            if match:
                matched.append((source_name, target_name))
                matched_indices.add(idx)
                substring_matches.append(f"{word} ⊂ {source_name}")

    # 3단계: 퍼지 매칭
    try:
        from rapidfuzz import fuzz

        for word in words:
            if len(word) < 3:  # 퍼지 매칭은 3글자 이상만
                continue

            for idx, (kr_name, en_name) in enumerate(entries):
                if idx in matched_indices:
                    continue

                source_name = kr_name if source_lang == "ko" else en_name
                target_name = en_name if source_lang == "ko" else kr_name

                # 짧은 이름들과 퍼지 매칭
                for short_name in _extract_short_names(source_name):
                    if len(short_name) < 3:
                        continue

                    # 영어는 대소문자 무시 퍼지 비교
                    if source_lang == "en":
                        ratio = fuzz.ratio(word.lower(), short_name.lower())
                    else:
                        ratio = fuzz.ratio(word, short_name)

                    if ratio >= fuzzy_threshold:
                        matched.append((source_name, target_name))
                        matched_indices.add(idx)
                        fuzzy_matches.append(f"{word} ≈ {short_name} ({ratio}%)")
                        break

    except ImportError:
        logger.debug("rapidfuzz 미설치, 퍼지 매칭 건너뜀")

    debug_info = {
        "extracted_words": words,
        "exact_matches": exact_matches,
        "substring_matches": substring_matches,
        "fuzzy_matches": fuzzy_matches,
        "total_matched": len(matched),
    }

    logger.debug(f"용어 매칭: {len(words)}개 단어 → {len(matched)}개 매칭")

    return GlossaryMatchResult(
        matched_terms=matched,
        extracted_words=words,
        debug_info=debug_info
    )


def clear_cache() -> None:
    """캐시 초기화 (테스트 또는 용어집 갱신 시 사용)"""
    global _kiwi, _kiwi_initialized
    _glossary_cache.clear()
    _entries_cache.clear()
    _word_index_cache.clear()
    _effective_stopwords_cache.clear()
    _kiwi = None
    _kiwi_initialized = False
