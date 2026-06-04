"""분석 AI 프로바이더 추상화.

ANALYSIS_PROVIDER env로 claude | openai | gemini 선택.
ANALYSIS_MODEL env로 모델 오버라이드 (비워두면 provider 기본값).
"""
import asyncio
import os
from abc import ABC, abstractmethod


DEFAULT_MODELS: dict[str, str] = {
    "claude": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "gemini": "gemini-2.0-flash",
}


class AnalysisProvider(ABC):
    """RFP 분석용 LLM 프로바이더 인터페이스."""

    @abstractmethod
    async def complete(self, system_prompt: str, user_content: str) -> str:
        """system_prompt + user_content → LLM 응답 텍스트."""


class ClaudeProvider(AnalysisProvider):
    def __init__(self, api_key: str, model: str) -> None:
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "anthropic 패키지가 설치되지 않았습니다. "
                "`pip install anthropic` 를 실행해 주세요."
            ) from e
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def complete(self, system_prompt: str, user_content: str) -> str:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text


class OpenAIProvider(AnalysisProvider):
    def __init__(self, api_key: str, model: str) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ImportError(
                "openai 패키지가 설치되지 않았습니다. "
                "`pip install openai` 를 실행해 주세요."
            ) from e
        self._client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def complete(self, system_prompt: str, user_content: str) -> str:
        from openai import AsyncOpenAI  # 런타임 import (선택적)
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=4096,
        )
        return response.choices[0].message.content


class GeminiProvider(AnalysisProvider):
    def __init__(self, api_key: str, model: str) -> None:
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise ImportError(
                "google-generativeai 패키지가 설치되지 않았습니다. "
                "`pip install google-generativeai` 를 실행해 주세요."
            ) from e
        genai.configure(api_key=api_key)
        self._genai = genai
        self.model_name = model

    async def complete(self, system_prompt: str, user_content: str) -> str:
        model = self._genai.GenerativeModel(
            model_name=self.model_name,
            system_instruction=system_prompt,
        )
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, lambda: model.generate_content(user_content)
        )
        return response.text


def analysis_enabled() -> bool:
    """USE_ANALYSIS_PROVIDER env (기본 false). true일 때만 분석 UI(컬럼·버튼) 표시."""
    val = os.environ.get("USE_ANALYSIS_PROVIDER", "false").strip().lower()
    return val in ("1", "true", "yes", "on")


def active_provider_name() -> str:
    """현재 활성 분석 프로바이더 이름."""
    return os.environ.get("ANALYSIS_PROVIDER", "claude").lower()


def create_provider() -> AnalysisProvider:
    """ANALYSIS_PROVIDER env에 따라 프로바이더 인스턴스 생성."""
    name = active_provider_name()
    model = os.environ.get("ANALYSIS_MODEL") or DEFAULT_MODELS.get(name)

    if name == "claude":
        api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("CLAUDE_API_KEY 환경변수가 설정되지 않았습니다.")
        return ClaudeProvider(api_key=api_key, model=model)

    if name == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY 환경변수가 설정되지 않았습니다.")
        return OpenAIProvider(api_key=api_key, model=model)

    if name == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
        return GeminiProvider(api_key=api_key, model=model)

    raise ValueError(
        f"지원하지 않는 ANALYSIS_PROVIDER: '{name}'. claude | openai | gemini 중 선택."
    )
