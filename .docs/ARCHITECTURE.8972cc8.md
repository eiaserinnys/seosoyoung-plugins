<!-- docgen: commit=8972cc8 -->
# seosoyoung_plugins Architecture Map

> Auto-generated. `python .claude/skills/docgen/scripts/generate.py <project_path>`

## Module Groups

### seosoyoung_plugins (root)
> seosoyoung-plugins: Plugin implementations for seosoyoung slackbot.

- `src/seosoyoung_plugins/soulstream_client.py`: 소울스트림 LLM 프록시 클라이언트

### channel_observer/
> Channel Observer plugin.

- `src/seosoyoung_plugins/channel_observer/collector.py`: 채널 메시지 수집기
- `src/seosoyoung_plugins/channel_observer/intervention.py`: 채널 개입(intervention) 모듈
- `src/seosoyoung_plugins/channel_observer/observer.py`: 채널 관찰 엔진
- `src/seosoyoung_plugins/channel_observer/pipeline.py`: 채널 소화/판단 파이프라인
- `src/seosoyoung_plugins/channel_observer/pipeline_lock.py`: 채널별 파이프라인 실행 잠금
- `src/seosoyoung_plugins/channel_observer/plugin.py`: Channel Observer plugin.
- `src/seosoyoung_plugins/channel_observer/prompts.py`: 채널 관찰 프롬프트
- `src/seosoyoung_plugins/channel_observer/scheduler.py`: 채널 소화 주기적 스케줄러
- `src/seosoyoung_plugins/channel_observer/store.py`: 채널 관찰 데이터 저장소

### memory/
> Memory 플러그인

- `src/seosoyoung_plugins/memory/context_builder.py`: 컨텍스트 빌더
- `src/seosoyoung_plugins/memory/intervention.py`: 채널 개입(intervention) 모듈
- `src/seosoyoung_plugins/memory/migration.py`: OM 마크다운 → JSON 마이그레이션
- `src/seosoyoung_plugins/memory/observation_pipeline.py`: 관찰 파이프라인
- `src/seosoyoung_plugins/memory/observer.py`: Observer 모듈
- `src/seosoyoung_plugins/memory/plugin.py`: Memory plugin.
- `src/seosoyoung_plugins/memory/promoter.py`: Promoter / Compactor 모듈
- `src/seosoyoung_plugins/memory/prompt_loader.py`: 프롬프트 파일 로더
- `src/seosoyoung_plugins/memory/prompts.py`: Observer/Reflector 프롬프트
- `src/seosoyoung_plugins/memory/reflector.py`: Reflector 모듈
- `src/seosoyoung_plugins/memory/store.py`: 관찰 로그 저장소
- `src/seosoyoung_plugins/memory/token_counter.py`: 토큰 카운터

### translate/
> Translate plugin package.

- `src/seosoyoung_plugins/translate/detector.py`: 언어 감지 모듈
- `src/seosoyoung_plugins/translate/glossary.py`: 용어집 로더 모듈
- `src/seosoyoung_plugins/translate/plugin.py`: Translate plugin.
- `src/seosoyoung_plugins/translate/slack_escape.py`: Slack 마크업 이스케이프/언이스케이프 모듈
- `src/seosoyoung_plugins/translate/translator.py`: 번역 모듈

### trello/
> Trello plugin package.

- `src/seosoyoung_plugins/trello/client.py`: Trello API 클라이언트
- `src/seosoyoung_plugins/trello/formatting.py`: 트렐로 카드 포맷팅 유틸리티
- `src/seosoyoung_plugins/trello/list_runner.py`: ListRunner - 리스트 정주행 기능
- `src/seosoyoung_plugins/trello/plugin.py`: Trello plugin.
- `src/seosoyoung_plugins/trello/prompt_builder.py`: 트렐로 카드 프롬프트 빌더
- `src/seosoyoung_plugins/trello/watcher.py`: Trello 워처 - To Go 리스트 감시 및 처리

### utils/
> Utility modules for seosoyoung plugins.

- `src/seosoyoung_plugins/utils/async_runner.py`: 스레드 안전한 async 실행 헬퍼.
- `src/seosoyoung_plugins/utils/message_formatter.py`: 슬랙 메시지 -> 프롬프트 주입 포맷터
- `src/seosoyoung_plugins/utils/prompt_loader.py`: 프롬프트 파일 로더
- `src/seosoyoung_plugins/utils/token_counter.py`: 토큰 카운터

## Entry Points & Flows

(no entry points detected)


## Dependency Graph

### Hub Modules (highest connectivity)

- `soulstream_client` — 8 importers, 0 dependencies
- `memory.store` — 7 importers, 0 dependencies
- `channel_observer.observer` — 4 importers, 3 dependencies
- `memory.promoter` — 2 importers, 4 dependencies
- `memory.observation_pipeline` — 1 importers, 5 dependencies
- `memory` — 0 importers, 6 dependencies
- `translate.translator` — 2 importers, 4 dependencies
- `memory.reflector` — 2 importers, 4 dependencies
- `utils.token_counter` — 5 importers, 0 dependencies
- `channel_observer.pipeline` — 0 importers, 5 dependencies

### Module Dependencies

- `channel_observer.collector` → channel_observer.store
- `channel_observer.intervention` → channel_observer.observer, channel_observer.prompts
- `channel_observer.observer` → channel_observer.prompts, memory.token_counter, soulstream_client
- `channel_observer.pipeline` → channel_observer.intervention, channel_observer.observer, channel_observer.prompts, channel_observer.store, memory.token_counter
- `channel_observer.plugin` → channel_observer, soulstream_client
- `channel_observer.prompts` → memory.prompt_loader
- `channel_observer.scheduler` → channel_observer.intervention, channel_observer.observer, channel_observer.store
- `memory` → memory.migration, memory.observation_pipeline, memory.observer, memory.promoter, memory.reflector, memory.store
- `memory.context_builder` → memory.store, utils.message_formatter, utils.token_counter
- `memory.intervention` → channel_observer.observer, channel_observer.prompts
- `memory.migration` → memory.store
- `memory.observation_pipeline` → memory.observer, memory.promoter, memory.reflector, memory.store, utils.token_counter
- `memory.observer` → memory.prompts, memory.store, soulstream_client
- `memory.plugin` → soulstream_client
- `memory.promoter` → memory.prompts, memory.store, soulstream_client, utils.token_counter
- `memory.prompts` → utils.prompt_loader
- `memory.reflector` → memory.prompts, memory.store, soulstream_client, utils.token_counter
- `translate` → translate.detector, translate.glossary, translate.translator
- `translate.plugin` → soulstream_client, translate.detector, translate.glossary, translate.translator
- `translate.translator` → soulstream_client, translate.detector, translate.glossary, translate.slack_escape
- `trello` → trello.client, trello.list_runner, trello.watcher
- `trello.plugin` → trello.client, trello.prompt_builder
- `trello.prompt_builder` → trello.client, trello.formatting
- `trello.watcher` → trello.client, trello.prompt_builder
- `utils` → utils.message_formatter, utils.prompt_loader, utils.token_counter

## Impact Map (Reverse Dependencies)

*If you change X, check Y.*

- `channel_observer` ← channel_observer.plugin
- `channel_observer.intervention` ← channel_observer.pipeline, channel_observer.scheduler
- `channel_observer.observer` ← channel_observer.intervention, channel_observer.pipeline, channel_observer.scheduler, memory.intervention
- `channel_observer.prompts` ← channel_observer.intervention, channel_observer.observer, channel_observer.pipeline, memory.intervention
- `channel_observer.store` ← channel_observer.collector, channel_observer.pipeline, channel_observer.scheduler
- `memory.migration` ← memory
- `memory.observation_pipeline` ← memory
- `memory.observer` ← memory, memory.observation_pipeline
- `memory.promoter` ← memory, memory.observation_pipeline
- `memory.prompt_loader` ← channel_observer.prompts
- `memory.prompts` ← memory.observer, memory.promoter, memory.reflector
- `memory.reflector` ← memory, memory.observation_pipeline
- `memory.store` ← memory, memory.context_builder, memory.migration, memory.observation_pipeline, memory.observer, memory.promoter, memory.reflector
- `memory.token_counter` ← channel_observer.observer, channel_observer.pipeline
- `soulstream_client` ← channel_observer.observer, channel_observer.plugin, memory.observer, memory.plugin, memory.promoter, memory.reflector, translate.plugin, translate.translator
- `translate.detector` ← translate, translate.plugin, translate.translator
- `translate.glossary` ← translate, translate.plugin, translate.translator
- `translate.slack_escape` ← translate.translator
- `translate.translator` ← translate, translate.plugin
- `trello.client` ← trello, trello.plugin, trello.prompt_builder, trello.watcher
- `trello.formatting` ← trello.prompt_builder
- `trello.list_runner` ← trello
- `trello.prompt_builder` ← trello.plugin, trello.watcher
- `trello.watcher` ← trello
- `utils.message_formatter` ← memory.context_builder, utils
- `utils.prompt_loader` ← memory.prompts, utils
- `utils.token_counter` ← memory.context_builder, memory.observation_pipeline, memory.promoter, memory.reflector, utils

## Module Details

### `src/seosoyoung_plugins/channel_observer/collector.py`
> 채널 메시지 수집기

**class `ChannelMessageCollector`** (L16)
: 관찰 대상 채널의 메시지를 수집하여 버퍼에 저장

- `__init__(store: ChannelStore, target_channels: list[str], bot_user_id: str | None)` (L73)
- `bot_user_id() -> str | None` (L84): 봇 사용자 ID.
- `collect(event: dict) -> bool` (L114): 이벤트에서 메시지를 추출하여 버퍼에 저장.
- `collect_reaction(event: dict, action: str) -> bool` (L203): 리액션 이벤트에서 reactions 필드를 갱신합니다.

### `src/seosoyoung_plugins/channel_observer/intervention.py`
> 채널 개입(intervention) 모듈

**class `InterventionAction`** (L31)
: 개입 액션

- Fields: `type: str`, `target: str`, `content: str`


**class `InterventionHistory`** (L222)
: 개입 이력 관리

- `__init__(base_dir: str | Path)` (L238)
- `record(channel_id: str, entry_type: str) -> None` (L267): 개입 이력을 기록합니다.
- `minutes_since_last(channel_id: str) -> float` (L279): 마지막 개입으로부터 경과 시간(분)을 반환합니다.
- `recent_count(channel_id: str, window_minutes: int) -> int` (L293): 최근 window_minutes 내 개입 횟수를 반환합니다.
- `burst_probability(channel_id: str, importance: int) -> float` (L304): 버스트 인식 개입 확률을 반환합니다.
- `can_react(channel_id: str) -> bool` (L318): 이모지 리액션은 항상 허용
- `filter_actions(channel_id: str, actions: list[InterventionAction]) -> list[InterventionAction]` (L322): 액션을 필터링합니다.

- `parse_intervention_markup(result: ChannelObserverResult) -> list[InterventionAction]` (L39): ChannelObserverResult를 InterventionAction 리스트로 변환합니다.
- `async execute_interventions(channel_id: str, actions: list[InterventionAction]) -> list[Optional[dict]]` (L80): InterventionAction 리스트를 슬랙 API로 발송합니다.
- `intervention_probability(minutes_since_last: float, recent_count: int) -> float` (L130): 시간 감쇠와 빈도 감쇠를 기반으로 개입 확률을 계산합니다.
- `burst_intervention_probability(history_entries: list[dict], importance: int, now: float | None) -> float` (L152): 버스트 인식 개입 확률을 계산합니다.
- `async send_debug_log(debug_channel: str, source_channel: str, observer_result: ChannelObserverResult, actions: list[InterventionAction], actions_filtered: list[InterventionAction], reasoning: Optional[str], emotion: Optional[str], pending_count: int, reaction_detail: Optional[str]) -> None` (L359): 디버그 채널에 관찰 결과 로그를 전송합니다 (Block Kit 형식).
- `async send_collect_debug_log(debug_channel: str, source_channel: str, buffer_tokens: int, threshold: int, message_text: str, user: str, is_thread: bool) -> None` (L420): 메시지 수집 시 디버그 채널에 로그를 전송합니다 (Block Kit 형식).
- `async send_digest_skip_debug_log(debug_channel: str, source_channel: str, buffer_tokens: int, threshold: int) -> None` (L464): 소화 스킵(임계치 미달) 시 디버그 채널에 로그를 전송합니다 (Block Kit 형식).
- `async send_intervention_probability_debug_log(debug_channel: str, source_channel: str, importance: int, time_factor: float, freq_factor: float, probability: float, final_score: float, threshold: float, passed: bool) -> None` (L494): 확률 기반 개입 판단 결과를 디버그 채널에 기록합니다 (Block Kit 형식).
- `async send_multi_judge_debug_log(debug_channel: str, source_channel: str, items: list[JudgeItem], react_actions: list[InterventionAction], message_actions_executed: list[InterventionAction], pending_count: int, pending_messages: list[dict] | None, slack_client) -> None` (L538): 복수 판단 결과를 메시지별 독립 블록으로 디버그 채널에 전송합니다.

### `src/seosoyoung_plugins/channel_observer/observer.py`
> 채널 관찰 엔진

**class `ChannelObserverResult`** (L34)
: 채널 관찰 결과 (하위호환 유지)

- Fields: `digest: str = ''`, `importance: int = 0`, `reaction_type: str = 'none'`, `reaction_target: Optional[str] = None`, `reaction_content: Optional[str] = None`


**class `DigestResult`** (L45)
: 소화 전용 결과

- Fields: `digest: str`, `token_count: int`


**class `JudgeItem`** (L53)
: 개별 메시지에 대한 리액션 판단 결과

- Fields: `ts: str = ''`, `importance: int = 0`, `reaction_type: str = 'none'`, `reaction_target: Optional[str] = None`, `reaction_content: Optional[str] = None`, `reasoning: Optional[str] = None`, `emotion: Optional[str] = None`, `addressed_to_me: bool = False`, `addressed_to_me_reason: Optional[str] = None`, `related_to_me: bool = False`, `related_to_me_reason: Optional[str] = None`, `is_instruction: bool = False`, `is_instruction_reason: Optional[str] = None`, `context_meaning: Optional[str] = None`, `linked_message_ts: Optional[str] = None`, `link_reason: Optional[str] = None`


**class `JudgeResult`** (L75)
: 복수 메시지에 대한 리액션 판단 결과

- Fields: `items: list[JudgeItem] = field(default_factory=list)`, `importance: int = 0`, `reaction_type: str = 'none'`, `reaction_target: Optional[str] = None`, `reaction_content: Optional[str] = None`, `reasoning: Optional[str] = None`, `emotion: Optional[str] = None`, `addressed_to_me: bool = False`, `addressed_to_me_reason: Optional[str] = None`, `related_to_me: bool = False`, `related_to_me_reason: Optional[str] = None`, `is_instruction: bool = False`, `is_instruction_reason: Optional[str] = None`, `context_meaning: Optional[str] = None`


**class `DigestCompressorResult`** (L101)
: digest 압축 결과

- Fields: `digest: str`, `token_count: int`


**class `ChannelObserver`** (L281)
: 채널 대화를 관찰하여 digest를 갱신하고 반응을 판단

- `__init__(soulstream_client: SoulstreamClient, model: str)` (L284)
- `async observe(channel_id: str, existing_digest: str | None, channel_messages: list[dict], thread_buffers: dict[str, list[dict]]) -> ChannelObserverResult | None` (L288): 채널 버퍼를 분석하여 관찰 결과를 반환합니다 (하위호환).
- `async digest(channel_id: str, existing_digest: str | None, judged_messages: list[dict]) -> DigestResult | None` (L332): judged 메시지를 digest에 편입합니다 (소화 전용).
- `async judge(channel_id: str, digest: str | None, judged_messages: list[dict], pending_messages: list[dict], thread_buffers: dict[str, list[dict]] | None, bot_user_id: str | None, slack_client) -> JudgeResult | None` (L380): pending 메시지에 대해 리액션을 판단합니다 (판단 전용).

**class `DigestCompressor`** (L434)
: digest가 임계치를 초과할 때 압축

- `__init__(soulstream_client: SoulstreamClient, model: str)` (L437)
- `async compress(digest: str, target_tokens: int) -> DigestCompressorResult | None` (L442): digest를 압축합니다.

- `parse_channel_observer_output(text: str) -> ChannelObserverResult` (L108): Observer 응답에서 XML 태그를 파싱합니다.
- `parse_judge_output(text: str) -> JudgeResult` (L134): Judge 응답에서 XML 태그를 파싱합니다.

### `src/seosoyoung_plugins/channel_observer/pipeline.py`
> 채널 소화/판단 파이프라인

- `async run_channel_pipeline(store: ChannelStore, observer: ChannelObserver, channel_id: str, cooldown: InterventionHistory, threshold_a: int, threshold_b: int, compressor: Optional[DigestCompressor], digest_max_tokens: int, digest_target_tokens: int, debug_channel: str, intervention_threshold: float, react_probability: float, llm_call: Optional[Callable], bot_user_id: str | None, recent_messages_count: int, intervene_model: str | None, folder_id: str | None) -> None` (L270): 소화/판단 분리 파이프라인을 실행합니다.

### `src/seosoyoung_plugins/channel_observer/pipeline_lock.py`
> 채널별 파이프라인 실행 잠금

- `try_acquire(channel_id: str) -> bool` (L13): 채널의 파이프라인 lock 획득을 시도한다.
- `release(channel_id: str) -> None` (L25): 채널의 파이프라인 lock을 해제한다.

### `src/seosoyoung_plugins/channel_observer/plugin.py`
> Channel Observer plugin.

**class `ChannelObserverPlugin`(Plugin)** (L23)
: Channel observation and digest management plugin.

- `async on_load(config: dict[str, Any]) -> None` (L40)
- `async on_unload() -> None` (L99)
- `register_hooks() -> dict` (L106)
- `collect_reaction(event: dict, action: str) -> bool` (L247): Collect reaction events for channel observation.
- `store() -> Any` (L259): ChannelStore instance (for session_context hybrid mode).
- `channels() -> list[str]` (L264): Monitored channel IDs.

### `src/seosoyoung_plugins/channel_observer/prompts.py`
> 채널 관찰 프롬프트

**class `DisplayNameResolver`** (L18)
: Slack user ID → 디스플레이네임 캐시 기반 변환기.

- `__init__(slack_client)` (L24)
- `resolve(user_id: str) -> str` (L28): user_id를 '이름 / [UID]' 형식으로 변환합니다.

- `build_channel_observer_system_prompt() -> str` (L70): 채널 관찰 시스템 프롬프트를 반환합니다.
- `build_channel_observer_user_prompt(channel_id: str, existing_digest: str | None, channel_messages: list[dict], thread_buffers: dict[str, list[dict]], current_time: datetime | None) -> str` (L75): 채널 관찰 사용자 프롬프트를 구성합니다.
- `build_digest_compressor_system_prompt(target_tokens: int) -> str` (L109): digest 압축 시스템 프롬프트를 반환합니다.
- `build_digest_compressor_retry_prompt(token_count: int, target_tokens: int) -> str` (L114): digest 압축 재시도 프롬프트를 반환합니다.
- `get_channel_intervene_system_prompt() -> str` (L123): 채널 개입 응답 생성 시스템 프롬프트를 반환합니다.
- `build_digest_only_system_prompt() -> str` (L128): 소화 전용 시스템 프롬프트를 반환합니다.
- `build_digest_only_user_prompt(channel_id: str, existing_digest: str | None, judged_messages: list[dict], current_time: datetime | None) -> str` (L133): 소화 전용 사용자 프롬프트를 구성합니다.
- `build_judge_system_prompt() -> str` (L164): 리액션 판단 전용 시스템 프롬프트를 반환합니다.
- `build_judge_user_prompt(channel_id: str, digest: str | None, judged_messages: list[dict], pending_messages: list[dict], thread_buffers: dict[str, list[dict]] | None, bot_user_id: str | None, slack_client) -> str` (L169): 리액션 판단 전용 사용자 프롬프트를 구성합니다.

### `src/seosoyoung_plugins/channel_observer/scheduler.py`
> 채널 소화 주기적 스케줄러

**class `ChannelDigestScheduler`** (L19)
: 주기적으로 채널 버퍼를 체크하여 소화를 트리거하는 스케줄러

- `__init__()` (L27)
- `start() -> None` (L70): 스케줄러를 시작합니다.
- `stop() -> None` (L81): 스케줄러를 중지합니다.

### `src/seosoyoung_plugins/channel_observer/store.py`
> 채널 관찰 데이터 저장소

**class `ChannelStore`** (L24)
: 파일 기반 채널 관찰 데이터 저장소

- `__init__(base_dir: str | Path)` (L30)
- `append_pending(channel_id: str, message: dict) -> None` (L57): 채널 루트 메시지를 pending 버퍼에 추가
- `upsert_pending(channel_id: str, message: dict) -> None` (L65): 같은 ts의 메시지가 있으면 교체, 없으면 추가.
- `load_pending(channel_id: str) -> list[dict]` (L90): pending 버퍼를 로드. 없으면 빈 리스트.
- `clear_pending(channel_id: str) -> None` (L100): pending 버퍼만 비운다.
- `append_channel_message(channel_id: str, message: dict) -> None` (L108): append_pending의 하위호환 별칭
- `load_channel_buffer(channel_id: str) -> list[dict]` (L112): load_pending의 하위호환 별칭
- `append_judged(channel_id: str, messages: list[dict]) -> None` (L124): judged 버퍼에 메시지들을 추가
- `load_judged(channel_id: str) -> list[dict]` (L133): judged 버퍼를 로드. 없으면 빈 리스트.
- `clear_judged(channel_id: str) -> None` (L143): judged 버퍼만 비운다.
- `move_pending_to_judged(channel_id: str) -> None` (L151): pending + 스레드 버퍼를 judged에 append 후 클리어
- `move_snapshot_to_judged(channel_id: str, snapshot_ts: set[str], snapshot_thread_ts: set[str] | None) -> None` (L166): 스냅샷에 포함된 메시지만 judged로 이동하고 나머지는 pending에 남깁니다.
- `append_thread_message(channel_id: str, thread_ts: str, message: dict) -> None` (L224): 스레드 메시지를 버퍼에 추가
- `upsert_thread_message(channel_id: str, thread_ts: str, message: dict) -> None` (L232): 같은 ts의 스레드 메시지가 있으면 교체, 없으면 추가.
- `load_thread_buffer(channel_id: str, thread_ts: str) -> list[dict]` (L254): 스레드 메시지 버퍼를 로드. 없으면 빈 리스트.
- `load_all_thread_buffers(channel_id: str) -> dict[str, list[dict]]` (L264): 채널의 전체 스레드 버퍼를 로드. {thread_ts: [messages]} 형태.
- `count_pending_tokens(channel_id: str) -> int` (L290): pending 버퍼 총 토큰 수 (채널 + 스레드 합산)
- `count_judged_plus_pending_tokens(channel_id: str) -> int` (L299): judged + pending 합산 토큰 수
- `count_buffer_tokens(channel_id: str) -> int` (L305): count_pending_tokens의 하위호환 별칭
- `clear_buffers(channel_id: str) -> None` (L320): pending + judged + 스레드 버퍼를 모두 비운다.
- `get_digest(channel_id: str) -> dict | None` (L337): digest.md를 로드. 없으면 None.
- `save_digest(channel_id: str, content: str, meta: dict) -> None` (L356): digest.md를 저장
- `update_reactions(channel_id: str) -> None` (L369): pending/judged/thread 버퍼에서 ts가 일치하는 메시지의 reactions를 갱신합니다.

### `src/seosoyoung_plugins/memory/context_builder.py`
> 컨텍스트 빌더

**class `InjectionResult`** (L31)
: 주입 결과 -- 디버그 로그용 정보를 포함

- Fields: `prompt: str | None`, `persistent_tokens: int = 0`, `session_tokens: int = 0`, `persistent_content: str = ''`, `session_content: str = ''`, `channel_digest_tokens: int = 0`, `channel_buffer_tokens: int = 0`, `new_observation_tokens: int = 0`, `new_observation_content: str = ''`


**class `ContextBuilder`** (L221)
: 장기 기억 + 세션 관찰 로그 + 채널 관찰을 시스템 프롬프트로 변환

- `__init__(store: MemoryStore, channel_store: Optional['ChannelStore'])` (L224)
- `build_memory_prompt(thread_ts: str, max_tokens: int, include_persistent: bool, include_session: bool, include_channel_observation: bool, channel_id: Optional[str], include_new_observations: bool) -> InjectionResult` (L292): 장기 기억, 세션 관찰, 채널 관찰, 새 관찰을 합쳐서 시스템 프롬프트로 변환합니다.

- `render_observation_items(items: list[dict], now: datetime | None) -> str` (L48): 관찰 항목 리스트를 사람이 읽을 수 있는 텍스트로 렌더링합니다.
- `render_persistent_items(items: list[dict]) -> str` (L81): 장기 기억 항목 리스트를 텍스트로 렌더링합니다.
- `optimize_items_for_context(items: list[dict], max_tokens: int) -> list[dict]` (L121): 관찰 항목을 컨텍스트 주입에 최적화합니다.
- `add_relative_time(observations: str, now: datetime | None) -> str` (L161): [하위 호환] 텍스트 관찰 로그의 날짜 헤더에 상대 시간 주석을 추가합니다.
- `optimize_for_context(observations: str, max_tokens: int) -> str` (L181): [하위 호환] 텍스트 관찰 로그를 컨텍스트 주입에 최적화합니다.

### `src/seosoyoung_plugins/memory/intervention.py`
> 채널 개입(intervention) 모듈

**class `InterventionAction`** (L31)
: 개입 액션

- Fields: `type: str`, `target: str`, `content: str`


**class `InterventionHistory`** (L222)
: 개입 이력 관리

- `__init__(base_dir: str | Path)` (L238)
- `record(channel_id: str, entry_type: str) -> None` (L267): 개입 이력을 기록합니다.
- `minutes_since_last(channel_id: str) -> float` (L279): 마지막 개입으로부터 경과 시간(분)을 반환합니다.
- `recent_count(channel_id: str, window_minutes: int) -> int` (L293): 최근 window_minutes 내 개입 횟수를 반환합니다.
- `burst_probability(channel_id: str, importance: int) -> float` (L304): 버스트 인식 개입 확률을 반환합니다.
- `can_react(channel_id: str) -> bool` (L318): 이모지 리액션은 항상 허용
- `filter_actions(channel_id: str, actions: list[InterventionAction]) -> list[InterventionAction]` (L322): 액션을 필터링합니다.

- `parse_intervention_markup(result: ChannelObserverResult) -> list[InterventionAction]` (L39): ChannelObserverResult를 InterventionAction 리스트로 변환합니다.
- `async execute_interventions(channel_id: str, actions: list[InterventionAction]) -> list[Optional[dict]]` (L80): InterventionAction 리스트를 슬랙 API로 발송합니다. (plugin_sdk 사용)
- `intervention_probability(minutes_since_last: float, recent_count: int) -> float` (L130): 시간 감쇠와 빈도 감쇠를 기반으로 개입 확률을 계산합니다.
- `burst_intervention_probability(history_entries: list[dict], importance: int, now: float | None) -> float` (L152): 버스트 인식 개입 확률을 계산합니다.
- `async send_debug_log(debug_channel: str, source_channel: str, observer_result: ChannelObserverResult, actions: list[InterventionAction], actions_filtered: list[InterventionAction], reasoning: Optional[str], emotion: Optional[str], pending_count: int, reaction_detail: Optional[str]) -> None` (L359): 디버그 채널에 관찰 결과 로그를 전송합니다 (Block Kit 형식, plugin_sdk 사용).
- `async send_collect_debug_log(debug_channel: str, source_channel: str, buffer_tokens: int, threshold: int, message_text: str, user: str, is_thread: bool) -> None` (L420): 메시지 수집 시 디버그 채널에 로그를 전송합니다 (Block Kit 형식, plugin_sdk 사용).
- `async send_digest_skip_debug_log(debug_channel: str, source_channel: str, buffer_tokens: int, threshold: int) -> None` (L464): 소화 스킵(임계치 미달) 시 디버그 채널에 로그를 전송합니다 (Block Kit 형식, plugin_sdk 사용).
- `async send_intervention_probability_debug_log(debug_channel: str, source_channel: str, importance: int, time_factor: float, freq_factor: float, probability: float, final_score: float, threshold: float, passed: bool) -> None` (L494): 확률 기반 개입 판단 결과를 디버그 채널에 기록합니다 (Block Kit 형식, plugin_sdk 사용).
- `async send_multi_judge_debug_log(debug_channel: str, source_channel: str, items: list[JudgeItem], react_actions: list[InterventionAction], message_actions_executed: list[InterventionAction], pending_count: int, pending_messages: list[dict] | None, slack_client) -> None` (L538): 복수 판단 결과를 메시지별 독립 블록으로 디버그 채널에 전송합니다 (plugin_sdk 사용).

### `src/seosoyoung_plugins/memory/migration.py`
> OM 마크다운 → JSON 마이그레이션

**class `MigrationReport`** (L21)
: 마이그레이션 결과 보고서

- Fields: `observations_converted: list[str] = field(default_factory=list)`, `persistent_converted: bool = False`, `skipped: list[str] = field(default_factory=list)`, `errors: list[str] = field(default_factory=list)`, `dry_run: bool = False`

- `total_converted() -> int` (L31)
- `summary() -> str` (L34)

- `migrate_observations(observations_dir: Path, dry_run: bool) -> MigrationReport` (L53): observations/ 디렉토리의 .md 파일을 .json으로 변환합니다.
- `migrate_persistent(persistent_dir: Path, dry_run: bool) -> bool` (L105): persistent/recent.md → recent.json 변환.
- `migrate_memory_dir(base_dir: str | Path, dry_run: bool) -> MigrationReport` (L145): memory/ 디렉토리 전체를 마이그레이션합니다.

### `src/seosoyoung_plugins/memory/observation_pipeline.py`
> 관찰 파이프라인

- `render_observation_items(items: list[dict], now: datetime | None) -> str` (L53): 관찰 항목 리스트를 사람이 읽을 수 있는 텍스트로 렌더링합니다.
- `render_persistent_items(items: list[dict]) -> str` (L88): 장기 기억 항목 리스트를 텍스트로 렌더링합니다.
- `async observe_conversation(store: MemoryStore, observer: Observer, thread_ts: str, user_id: str, messages: list[dict], min_turn_tokens: int, reflector: Optional[Reflector], reflection_threshold: int, promoter: Optional[Promoter], promotion_threshold: int, compactor: Optional[Compactor], compaction_threshold: int, compaction_target: int, debug_channel: str, anchor_ts: str, slack_bot_token: str, emoji_obs_complete: str) -> bool` (L171): 매턴 Observer를 호출하여 세션 관찰 로그를 갱신하고 후보를 수집합니다.

### `src/seosoyoung_plugins/memory/observer.py`
> Observer 모듈

**class `ObserverResult`** (L26)
: Observer 출력 결과

- Fields: `observations: list[dict] = field(default_factory=list)`, `current_task: str = ''`, `suggested_response: str = ''`, `candidates: list[dict] = field(default_factory=list)`


**class `Observer`** (L147)
: 대화를 관찰하여 구조화된 관찰 로그를 생성

- `__init__(soulstream_client: SoulstreamClient, model: str)` (L150)
- `async observe(existing_observations: list[dict] | None, messages: list[dict]) -> ObserverResult | None` (L154): 대화를 관찰하여 새 관찰 로그를 생성합니다.

- `parse_observer_output(text: str, existing_items: list[dict] | None) -> ObserverResult` (L35): Observer 응답 JSON을 파싱합니다.

### `src/seosoyoung_plugins/memory/plugin.py`
> Memory plugin.

**class `MemoryPlugin`(Plugin)** (L21)
: Observational Memory plugin.

- `async on_load(config: dict[str, Any]) -> None` (L36)
- `async on_unload() -> None` (L79)
- `register_hooks() -> dict` (L83)
- `on_compact_flag(thread_ts: str) -> None` (L175): PreCompact 훅에서 OM inject 플래그 설정.

### `src/seosoyoung_plugins/memory/promoter.py`
> Promoter / Compactor 모듈

**class `PromoterResult`** (L26)
: Promoter 출력 결과

- Fields: `promoted: list[dict] = field(default_factory=list)`, `rejected: list[dict] = field(default_factory=list)`, `promoted_count: int = 0`, `rejected_count: int = 0`, `priority_counts: dict = None`


**class `CompactorResult`** (L41)
: Compactor 출력 결과

- Fields: `compacted: list[dict] = field(default_factory=list)`, `token_count: int = 0`


**class `Promoter`** (L185)
: 장기 기억 후보를 검토하여 승격

- `__init__(soulstream_client: SoulstreamClient, model: str)` (L188)
- `async promote(candidates: list[dict], existing_persistent: list[dict]) -> PromoterResult` (L192): 후보 항목들을 검토하여 장기 기억 승격 여부를 판단합니다.
- `merge_promoted(existing: list[dict], promoted: list[dict]) -> list[dict]` (L219): 승격된 항목을 기존 장기 기억에 머지합니다. ID 기반 중복 제거.

**class `Compactor`** (L238)
: 장기 기억을 압축

- `__init__(soulstream_client: SoulstreamClient, model: str)` (L241)
- `async compact(persistent: list[dict], target_tokens: int) -> CompactorResult` (L246): 장기 기억을 압축합니다.

- `parse_promoter_output(text: str, existing_items: list[dict] | None) -> PromoterResult` (L123): Promoter 응답 JSON에서 promoted와 rejected를 파싱합니다.
- `parse_compactor_output(text: str, existing_items: list[dict] | None) -> list[dict]` (L163): Compactor 응답에서 JSON 배열을 파싱합니다.

### `src/seosoyoung_plugins/memory/prompt_loader.py`
> 프롬프트 파일 로더

- `load_prompt(filename: str) -> str` (L20): 프롬프트 파일을 로드합니다.
- @lru_cache(maxsize=32) `load_prompt_cached(filename: str) -> str` (L39): 프롬프트 파일을 캐시하여 로드합니다.

### `src/seosoyoung_plugins/memory/prompts.py`
> Observer/Reflector 프롬프트

- `build_observer_system_prompt() -> str` (L19): Observer 시스템 프롬프트를 반환합니다.
- `build_observer_user_prompt(existing_observations: list[dict] | None, messages: list[dict], current_time: datetime | None) -> str` (L24): Observer 사용자 프롬프트를 구성합니다.
- `build_reflector_system_prompt() -> str` (L67): Reflector 시스템 프롬프트를 반환합니다.
- `build_reflector_retry_prompt(token_count: int, target: int) -> str` (L72): Reflector 재시도 프롬프트를 반환합니다.
- `build_promoter_prompt(existing_persistent: list[dict], candidate_entries: list[dict]) -> str` (L79): Promoter 프롬프트를 구성합니다.
- `build_compactor_prompt(persistent_memory: list[dict], target_tokens: int) -> str` (L100): Compactor 프롬프트를 구성합니다.

### `src/seosoyoung_plugins/memory/reflector.py`
> Reflector 모듈

**class `ReflectorResult`** (L27)
: Reflector 출력 결과

- Fields: `observations: list[dict] = field(default_factory=list)`, `token_count: int = 0`


**class `Reflector`** (L95)
: 관찰 로그를 압축하고 재구조화

- `__init__(soulstream_client: SoulstreamClient, model: str)` (L98)
- `async reflect(observations: list[dict], target_tokens: int) -> ReflectorResult | None` (L103): 관찰 로그를 압축합니다.

### `src/seosoyoung_plugins/memory/store.py`
> 관찰 로그 저장소

**class `ObservationItem`** (L40)
: 세션 관찰 항목

- Fields: `id: str`, `priority: str`, `content: str`, `session_date: str`, `created_at: str`, `source: str = 'observer'`

- `to_dict() -> dict` (L50)
- `from_dict(d: dict) -> 'ObservationItem'` (L61)

**class `PersistentItem`** (L73)
: 장기 기억 항목

- Fields: `id: str`, `priority: str`, `content: str`, `promoted_at: str`, `source_obs_ids: list[str] = field(default_factory=list)`

- `to_dict() -> dict` (L82)
- `from_dict(d: dict) -> 'PersistentItem'` (L94)

**class `MemoryRecord`** (L238)
: 세션별 관찰 로그 레코드

- Fields: `thread_ts: str`, `user_id: str = ''`, `username: str = ''`, `observations: list[dict] = field(default_factory=list)`, `observation_tokens: int = 0`, `last_observed_at: datetime | None = None`, `total_sessions_observed: int = 0`, `reflection_count: int = 0`, `anchor_ts: str = ''`, `created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))`

- `to_meta_dict() -> dict` (L255): 메타데이터를 직렬화 가능한 dict로 변환
- `from_meta_dict(data: dict, observations: list[dict] | None) -> 'MemoryRecord'` (L274): dict에서 MemoryRecord를 복원

**class `MemoryStore`** (L300)
: 파일 기반 관찰 로그 저장소

- `__init__(base_dir: str | Path)` (L306)
- `get_record(thread_ts: str) -> MemoryRecord | None` (L338): 세션의 관찰 레코드를 로드합니다. 없으면 None.
- `save_record(record: MemoryRecord) -> None` (L369): 관찰 레코드를 저장합니다.
- `append_pending_messages(thread_ts: str, messages: list[dict]) -> None` (L393): 미관찰 대화를 세션별 버퍼에 누적합니다.
- `load_pending_messages(thread_ts: str) -> list[dict]` (L403): 미관찰 대화 버퍼를 로드합니다. 없으면 빈 리스트.
- `clear_pending_messages(thread_ts: str) -> None` (L419): 관찰 완료 후 미관찰 대화 버퍼를 비웁니다.
- `save_new_observations(thread_ts: str, content: list[dict]) -> None` (L434): 이번 턴에서 새로 추가된 관찰만 별도 저장합니다.
- `get_new_observations(thread_ts: str) -> list[dict]` (L442): 저장된 새 관찰을 반환합니다. 없으면 빈 리스트.
- `clear_new_observations(thread_ts: str) -> None` (L456): 주입 완료된 새 관찰을 클리어합니다.
- `set_inject_flag(thread_ts: str) -> None` (L469): 다음 요청에 OM을 주입하도록 플래그를 설정합니다.
- `check_and_clear_inject_flag(thread_ts: str) -> bool` (L474): inject 플래그를 확인하고 있으면 제거합니다.
- `save_conversation(thread_ts: str, messages: list[dict]) -> None` (L486): 세션 대화 로그를 JSONL로 저장합니다.
- `load_conversation(thread_ts: str) -> list[dict] | None` (L495): 세션 대화 로그를 로드합니다. 없으면 None.
- `append_candidates(thread_ts: str, entries: list[dict]) -> None` (L517): 후보 항목을 세션별 파일에 누적합니다.
- `load_candidates(thread_ts: str) -> list[dict]` (L527): 세션별 후보를 로드합니다. 없으면 빈 리스트.
- `load_all_candidates() -> list[dict]` (L543): 전체 세션의 후보를 수집합니다.
- `count_all_candidate_tokens() -> int` (L557): 전체 후보의 content 필드 토큰 합산.
- `clear_all_candidates() -> None` (L571): 모든 후보 파일을 삭제합니다.
- `get_persistent() -> dict | None` (L599): 장기 기억을 로드합니다. 없으면 None.
- `save_persistent(content: list[dict], meta: dict) -> None` (L633): 장기 기억을 저장합니다.
- `archive_persistent() -> Path | None` (L648): 기존 장기 기억을 archive/에 백업합니다.

- `generate_obs_id(existing_items: list[dict], date_str: str | None) -> str` (L123): 관찰 항목 ID를 생성합니다.
- `generate_ltm_id(existing_items: list[dict], date_str: str | None) -> str` (L132): 장기 기억 항목 ID를 생성합니다.
- `parse_md_observations(md_text: str) -> list[dict]` (L144): 마크다운 관찰 로그를 항목 리스트로 파싱합니다.
- `parse_md_persistent(md_text: str) -> list[dict]` (L192): 마크다운 장기 기억을 항목 리스트로 파싱합니다.

### `src/seosoyoung_plugins/memory/token_counter.py`
> 토큰 카운터

**class `TokenCounter`** (L9)
: o200k_base 인코딩 기반 토큰 카운터

- `__init__()` (L14)
- `count_string(text: str) -> int` (L17): 텍스트의 토큰 수를 반환합니다.
- `count_messages(messages: list[dict]) -> int` (L23): 메시지 목록의 총 토큰 수를 반환합니다.

### `src/seosoyoung_plugins/soulstream_client.py`
> 소울스트림 LLM 프록시 클라이언트

**class `SoulstreamResult`** (L16)
: LLM 프록시 응답 결과

- Fields: `content: str`, `input_tokens: int`, `output_tokens: int`, `session_id: str`


**class `SoulstreamClient`** (L68)
: 비동기 LLM 프록시 클라이언트

- `__init__(base_url: str, bearer_token: str)` (L74)
- `async complete(provider: str, model: str, messages: list[dict], max_tokens: int, temperature: float | None, client_id: str | None) -> SoulstreamResult` (L81): LLM completions 요청을 프록시 서버로 전송합니다.
- `async close()` (L110): 클라이언트를 닫습니다.

**class `SoulstreamSyncClient`** (L121)
: 동기 LLM 프록시 클라이언트

- `__init__(base_url: str, bearer_token: str)` (L127)
- `complete(provider: str, model: str, messages: list[dict], max_tokens: int, temperature: float | None, client_id: str | None) -> SoulstreamResult` (L134): LLM completions 요청을 프록시 서버로 전송합니다.
- `close()` (L163): 클라이언트를 닫습니다.

### `src/seosoyoung_plugins/translate/detector.py`
> 언어 감지 모듈

**class `Language`(Enum)** (L9)


- `is_korean_char(char: str) -> bool` (L14): 한글 문자인지 확인 (한글 자모, 음절 모두 포함)
- `detect_language(text: str, threshold: float) -> Language` (L27): 텍스트의 언어를 감지

### `src/seosoyoung_plugins/translate/glossary.py`
> 용어집 로더 모듈

**class `GlossaryMatchResult`** (L176)
: 용어 매칭 결과

- Fields: `matched_terms: list[tuple[str, str]]`, `extracted_words: list[str] = field(default_factory=list)`, `debug_info: dict = field(default_factory=dict)`


- `get_glossary_entries(glossary_path: str) -> tuple[tuple[str, str], ...]` (L276): 용어집 항목들을 (한국어, 영어) 쌍으로 반환 (캐싱)
- `find_relevant_terms(text: str, source_lang: str, fuzzy_threshold: int) -> list[tuple[str, str]]` (L481): 텍스트에서 관련 용어 추출 (하위 호환성 유지)
- `find_relevant_terms_v2(text: str, source_lang: str, fuzzy_threshold: int) -> GlossaryMatchResult` (L503): 텍스트에서 관련 용어 추출 (개선된 버전, 디버그 정보 포함)
- `clear_cache() -> None` (L635): 캐시 초기화 (테스트 또는 용어집 갱신 시 사용)

### `src/seosoyoung_plugins/translate/plugin.py`
> Translate plugin.

**class `TranslatePlugin`(Plugin)** (L25)
: 자동 번역 플러그인.

- `async on_load(config: dict[str, Any]) -> None` (L37)
- `async on_unload() -> None` (L59)
- `register_hooks() -> dict` (L62)
- `translate_text(text: str) -> tuple[str, float, list[tuple[str, str]], Language]` (L79): 텍스트를 번역합니다 (플러그인 설정 사용).

### `src/seosoyoung_plugins/translate/slack_escape.py`
> Slack 마크업 이스케이프/언이스케이프 모듈

- `escape_slack_markup(text: str) -> tuple[str, dict[str, str]]` (L40): 슬랙 마크업을 플레이스홀더로 치환합니다.
- `unescape_slack_markup(text: str, replacements: dict[str, str]) -> str` (L95): 플레이스홀더를 원본 슬랙 마크업으로 복원합니다.

### `src/seosoyoung_plugins/translate/translator.py`
> 번역 모듈

- `translate(text: str, source_lang: Language) -> tuple[str, float, list[tuple[str, str]], GlossaryMatchResult | None]` (L217): 텍스트를 번역

### `src/seosoyoung_plugins/trello/client.py`
> Trello API 클라이언트

**class `TrelloCard`** (L17)
: 트렐로 카드 정보

- Fields: `id: str`, `name: str`, `desc: str`, `url: str`, `list_id: str`, `list_name: str = ''`, `due_complete: bool = False`, `labels: list = field(default_factory=list)`


**class `TrelloClient`** (L29)
: Trello API 클라이언트

- `__init__()` (L36)
- `get_cards_in_list(list_id: str) -> list[TrelloCard]` (L56): 특정 리스트의 카드 목록 조회
- `get_card(card_id: str) -> Optional[TrelloCard]` (L75): 카드 상세 조회
- `update_card_name(card_id: str, name: str) -> bool` (L90): 카드 제목 변경
- `move_card(card_id: str, list_id: str) -> bool` (L95): 카드를 다른 리스트로 이동
- `get_card_checklists(card_id: str) -> list[dict]` (L100): 카드의 체크리스트 목록 조회
- `get_card_comments(card_id: str, limit: int) -> list[dict]` (L122): 카드의 코멘트 목록 조회
- `get_lists() -> list[dict]` (L143): 보드의 리스트 목록 조회
- `remove_label_from_card(card_id: str, label_id: str) -> bool` (L154): 카드에서 레이블 제거
- `is_configured() -> bool` (L159): API 설정 여부 확인

### `src/seosoyoung_plugins/trello/formatting.py`
> 트렐로 카드 포맷팅 유틸리티

- `format_checklists(checklists: list[dict]) -> str` (L7): 체크리스트를 프롬프트용 문자열로 포맷
- `format_comments(comments: list[dict]) -> str` (L28): 코멘트를 프롬프트용 문자열로 포맷

### `src/seosoyoung_plugins/trello/list_runner.py`
> ListRunner - 리스트 정주행 기능

**class `ListNotFoundError`(Exception)** (L20)
: 리스트를 찾을 수 없을 때 발생하는 예외


**class `EmptyListError`(Exception)** (L25)
: 리스트에 카드가 없을 때 발생하는 예외


**class `ValidationStatus`(Enum)** (L30)
: 검증 결과 상태


**class `SessionStatus`(Enum)** (L37)
: 리스트 정주행 세션 상태


**class `CardExecutionResult`** (L48)
: 카드 실행 결과

- Fields: `success: bool`, `card_id: str`, `output: str = ''`, `error: Optional[str] = None`, `session_id: Optional[str] = None`


**class `ValidationResult`** (L58)
: 검증 결과

- Fields: `status: ValidationStatus`, `card_id: str`, `output: str = ''`, `session_id: Optional[str] = None`


**class `CardRunResult`** (L67)
: 카드 실행 및 검증 전체 결과

- Fields: `card_id: str`, `execution_success: bool`, `validation_status: ValidationStatus`, `execution_output: str = ''`, `validation_output: str = ''`, `error: Optional[str] = None`


**class `ListRunSession`** (L78)
: 리스트 정주행 세션 정보

- Fields: `session_id: str`, `list_id: str`, `list_name: str`, `card_ids: list[str]`, `status: SessionStatus`, `created_at: str`, `current_index: int = 0`, `verify_session_id: Optional[str] = None`, `processed_cards: dict[str, str] = field(default_factory=dict)`, `error_message: Optional[str] = None`

- `to_dict() -> dict` (L91): 딕셔너리로 변환 (저장용)
- `from_dict(data: dict) -> 'ListRunSession'` (L107): 딕셔너리에서 생성 (로드용)

**class `ListRunner`** (L123)
: 리스트 정주행 관리자

- `__init__(data_dir: Optional[Path])` (L137): Args:
- `save_sessions()` (L164): 세션 목록 저장
- `create_session(list_id: str, list_name: str, card_ids: list[str]) -> ListRunSession` (L179): 새 정주행 세션 생성
- `get_session(session_id: str) -> Optional[ListRunSession]` (L215): 세션 조회
- `update_session_status(session_id: str, status: SessionStatus, error_message: Optional[str]) -> bool` (L226): 세션 상태 업데이트
- `get_active_sessions() -> list[ListRunSession]` (L256): 활성 세션 목록 조회
- `get_paused_sessions() -> list[ListRunSession]` (L339): 중단된 세션 목록 조회
- `find_session_by_list_name(list_name: str) -> Optional[ListRunSession]` (L352): 리스트 이름으로 활성 세션 검색
- `pause_run(session_id: str, reason: str) -> bool` (L373): 정주행 세션 중단
- `resume_run(session_id: str) -> bool` (L404): 중단된 정주행 세션 재개
- `mark_card_processed(session_id: str, card_id: str, result: str) -> bool` (L433): 카드 처리 완료 표시
- `get_next_card_id(session_id: str) -> Optional[str]` (L459): 다음 처리할 카드 ID 조회
- `async start_run_by_name(list_name: str, trello_client) -> ListRunSession` (L477): 리스트 이름으로 정주행 세션 시작
- `async process_next_card(session_id: str, trello_client) -> Optional[dict]` (L550): 다음 처리할 카드 정보 조회
- `async execute_card(session_id: str, card_info: dict, claude_runner) -> CardExecutionResult` (L571): 카드 실행
- `async validate_completion(session_id: str, card_info: dict, execution_output: str, claude_runner) -> ValidationResult` (L625): 카드 완료 검증
- `async run_next_card(session_id: str, trello_client, claude_runner, auto_pause_on_fail: bool) -> Optional[CardRunResult]` (L692): 다음 카드 실행 및 검증

### `src/seosoyoung_plugins/trello/plugin.py`
> Trello plugin.

**class `TrelloPlugin`(Plugin)** (L51)
: Trello watcher and card management plugin.

- `async on_load(config: dict[str, Any]) -> None` (L64)
- `async on_unload() -> None` (L91)
- `register_hooks() -> dict` (L96)

### `src/seosoyoung_plugins/trello/prompt_builder.py`
> 트렐로 카드 프롬프트 빌더

**class `PromptBuilder`** (L10)
: 트렐로 카드용 프롬프트 빌더

- `__init__(trello: TrelloClient)` (L17): Args:
- `build_card_context(card_id: str, desc: str) -> str` (L27): 카드의 체크리스트, 코멘트, 리스트 ID 컨텍스트를 조합
- `build_to_go_request(card: TrelloCard, has_execute: bool) -> tuple[str, list[dict]]` (L48): To Go 카드용 (prompt, context_items) 반환
- `build_reaction_execute_request(info) -> tuple[str, list[dict]]` (L111): 리액션 기반 실행용 (prompt, context_items) 반환
- `build_list_run_request(card: TrelloCard, session_id: str, current: int, total: int) -> tuple[str, list[dict]]` (L153): 리스트 정주행용 (prompt, context_items) 반환

### `src/seosoyoung_plugins/trello/watcher.py`
> Trello 워처 - To Go 리스트 감시 및 처리

**class `TrackedCard`** (L31)
: 추적 중인 카드 정보 (To Go 리스트 감시용)

- Fields: `card_id: str`, `card_name: str`, `card_url: str`, `list_id: str`, `list_key: str`, `thread_ts: str`, `channel_id: str`, `detected_at: str`, `session_id: Optional[str] = None`, `has_execute: bool = False`, `dm_thread_ts: Optional[str] = None`


**class `ThreadCardInfo`** (L47)
: 스레드 ↔ 카드 매핑 정보 (리액션 처리용)

- Fields: `thread_ts: str`, `channel_id: str`, `card_id: str`, `card_name: str`, `card_url: str`, `session_id: Optional[str] = None`, `has_execute: bool = False`, `created_at: str = ''`


**class `TrelloWatcher`** (L59)
: Trello 리스트 감시자

- `__init__()` (L71): Args:
- `update_thread_card_session_id(thread_ts: str, session_id: str) -> bool` (L249): ThreadCardInfo의 session_id 업데이트
- `get_tracked_by_thread_ts(thread_ts: str) -> Optional[ThreadCardInfo]` (L258): thread_ts로 ThreadCardInfo 조회
- `update_tracked_session_id(card_id: str, session_id: str) -> bool` (L262): TrackedCard의 session_id 업데이트
- `start()` (L273): 워처 시작
- `stop()` (L295): 워처 중지
- `pause()` (L305): 워처 일시 중단
- `resume()` (L311): 워처 재개
- `is_paused() -> bool` (L318)
- `build_reaction_execute_request(info: ThreadCardInfo) -> tuple[str, list[dict]]` (L666): PromptBuilder에 위임 — (prompt, context_items) 반환
- `resume_list_run_session(session, notify_channel: str, reason: str) -> None` (L916): 중단된 정주행 세션을 재개한다.

### `src/seosoyoung_plugins/utils/async_runner.py`
> 스레드 안전한 async 실행 헬퍼.

**class `AsyncRunner`** (L17)
: 스레드 안전한 async 코루틴 실행기.

- `__init__() -> None` (L24)
- `loop() -> Optional[asyncio.AbstractEventLoop]` (L29): 내부 이벤트 루프 참조 (읽기 전용).
- `start() -> None` (L33): 전용 데몬 스레드에서 이벤트 루프를 시작한다.
- `run(coro: Coroutine[Any, Any, T]) -> T` (L48): 코루틴을 제출하고 결과를 동기적으로 대기한다.
- `stop() -> None` (L69): 이벤트 루프를 중지하고 스레드를 join한다.

### `src/seosoyoung_plugins/utils/message_formatter.py`
> 슬랙 메시지 -> 프롬프트 주입 포맷터

- `format_slack_message(msg: dict, channel: str, include_meta: bool) -> str` (L11): 슬랙 메시지를 프롬프트 주입용 텍스트로 포맷합니다.

### `src/seosoyoung_plugins/utils/prompt_loader.py`
> 프롬프트 파일 로더

- `load_prompt(filename: str) -> str` (L20): 프롬프트 파일을 로드합니다.
- @lru_cache(maxsize=32) `load_prompt_cached(filename: str) -> str` (L39): 프롬프트 파일을 캐시하여 로드합니다.

### `src/seosoyoung_plugins/utils/token_counter.py`
> 토큰 카운터

**class `TokenCounter`** (L9)
: o200k_base 인코딩 기반 토큰 카운터

- `__init__()` (L14)
- `count_string(text: str) -> int` (L17): 텍스트의 토큰 수를 반환합니다.
- `count_messages(messages: list[dict]) -> int` (L23): 메시지 목록의 총 토큰 수를 반환합니다.
