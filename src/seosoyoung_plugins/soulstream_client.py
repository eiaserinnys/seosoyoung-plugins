"""소울스트림 LLM 프록시 클라이언트

소울스트림 서버의 /llm/completions 엔드포인트를 호출하여
OpenAI/Anthropic API를 프록시합니다.

동기(SoulstreamSyncClient)와 비동기(SoulstreamClient) 두 가지
클라이언트를 제공합니다.
"""

from dataclasses import dataclass

import httpx


@dataclass
class SoulstreamResult:
    """LLM 프록시 응답 결과"""

    content: str
    input_tokens: int
    output_tokens: int
    session_id: str


class SoulstreamClient:
    """비동기 LLM 프록시 클라이언트

    channel_observer, memory 플러그인처럼 비동기 컨텍스트에서 사용합니다.
    """

    def __init__(self, base_url: str, bearer_token: str):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=120.0,
        )

    async def complete(
        self,
        provider: str,
        model: str,
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float | None = None,
        client_id: str | None = None,
    ) -> SoulstreamResult:
        """LLM completions 요청을 프록시 서버로 전송합니다.

        Args:
            provider: "openai" 또는 "anthropic"
            model: 사용할 모델명
            messages: 메시지 리스트
            max_tokens: 최대 출력 토큰 수
            temperature: 온도 (None이면 서버 기본값)
            client_id: 클라이언트 식별자 (대시보드 추적용)

        Returns:
            SoulstreamResult
        """
        resp = await self._client.post(
            "/llm/completions",
            json={
                "provider": provider,
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "client_id": client_id,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return SoulstreamResult(
            content=data["content"],
            input_tokens=data["usage"]["input_tokens"],
            output_tokens=data["usage"]["output_tokens"],
            session_id=data["session_id"],
        )

    async def close(self):
        """클라이언트를 닫습니다."""
        await self._client.aclose()


class SoulstreamSyncClient:
    """동기 LLM 프록시 클라이언트

    translate 플러그인처럼 동기 컨텍스트에서 사용합니다.
    """

    def __init__(self, base_url: str, bearer_token: str):
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=120.0,
        )

    def complete(
        self,
        provider: str,
        model: str,
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float | None = None,
        client_id: str | None = None,
    ) -> SoulstreamResult:
        """LLM completions 요청을 프록시 서버로 전송합니다.

        Args:
            provider: "openai" 또는 "anthropic"
            model: 사용할 모델명
            messages: 메시지 리스트
            max_tokens: 최대 출력 토큰 수
            temperature: 온도 (None이면 서버 기본값)
            client_id: 클라이언트 식별자 (대시보드 추적용)

        Returns:
            SoulstreamResult
        """
        resp = self._client.post(
            "/llm/completions",
            json={
                "provider": provider,
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "client_id": client_id,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return SoulstreamResult(
            content=data["content"],
            input_tokens=data["usage"]["input_tokens"],
            output_tokens=data["usage"]["output_tokens"],
            session_id=data["session_id"],
        )

    def close(self):
        """클라이언트를 닫습니다."""
        self._client.close()
