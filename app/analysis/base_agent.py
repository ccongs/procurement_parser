"""Claude 기반 에이전트 추상 클래스"""

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import anthropic

logger = logging.getLogger("agent")

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"


class BaseAgent(ABC):
    """Claude 기반 에이전트 추상 클래스"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self.model = model or DEFAULT_CLAUDE_MODEL
        self.client = anthropic.Anthropic(api_key=self.api_key)

    @abstractmethod
    async def execute(
        self,
        input_data: Dict[str, Any],
        progress_callback: Optional[Callable] = None,
    ) -> Any:
        """에이전트 실행"""
        pass

    def _call_claude(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 4096,
    ) -> str:
        """
        Claude API 호출

        Args:
            system_prompt: 시스템 프롬프트
            user_message: 사용자 메시지
            max_tokens: 최대 토큰 수

        Returns:
            Claude 응답 텍스트
        """
        logger.debug(f"Claude API 호출 (model: {self.model})")

        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            return message.content[0].text
        except Exception as e:
            logger.error(f"Claude API 호출 실패: {e}")
            raise

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """
        텍스트에서 JSON 추출

        Args:
            text: JSON을 포함한 텍스트

        Returns:
            파싱된 JSON 딕셔너리
        """
        patterns = [
            r"```json\s*([\s\S]*?)\s*```",
            r"```\s*([\s\S]*?)\s*```",
            r"(\{[\s\S]*\})",
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                json_str = match.group(1)
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    continue

        logger.error("JSON 추출 실패")
        return {}

    def _truncate_text(self, text: str, max_chars: int = 30000) -> str:
        """텍스트 길이 제한"""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n\n... (텍스트가 잘렸습니다)"
