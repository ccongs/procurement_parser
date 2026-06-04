"""Phase 6.4 — create_provider() factory 테스트 (실제 API 호출 없음).

openai / google-generativeai 패키지가 설치되지 않아도 green이어야 함.
미설치 환경에서는 sys.modules 에 가짜 모듈을 등록해 ImportError 를 우회한다.
"""
import os
import sys
import types
import pytest
from unittest.mock import MagicMock, patch

# 테스트 환경 기본 키 설정
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")


def _register_fake_openai():
    """openai 패키지가 없는 환경에서 sys.modules 에 stub 등록."""
    if "openai" not in sys.modules:
        fake = types.ModuleType("openai")
        fake.AsyncOpenAI = MagicMock()
        sys.modules["openai"] = fake


def _register_fake_genai():
    """google.generativeai 패키지가 없는 환경에서 stub 등록."""
    if "google" not in sys.modules:
        google_mod = types.ModuleType("google")
        sys.modules["google"] = google_mod
    if "google.generativeai" not in sys.modules:
        genai_mod = types.ModuleType("google.generativeai")
        genai_mod.configure = MagicMock()
        genai_mod.GenerativeModel = MagicMock()
        sys.modules["google.generativeai"] = genai_mod
        # google 패키지 속성으로도 연결
        sys.modules["google"].generativeai = genai_mod


# 테스트 모듈 임포트 전에 stub 등록
_register_fake_openai()
_register_fake_genai()

from app.analysis.provider import (  # noqa: E402
    ClaudeProvider,
    OpenAIProvider,
    GeminiProvider,
    create_provider,
)


def test_create_provider_claude(monkeypatch):
    monkeypatch.setenv("ANALYSIS_PROVIDER", "claude")
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch("anthropic.AsyncAnthropic"):
        provider = create_provider()
    assert isinstance(provider, ClaudeProvider)


def test_create_provider_openai(monkeypatch):
    monkeypatch.setenv("ANALYSIS_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # sys.modules 에 등록된 fake openai.AsyncOpenAI 를 mock
    with patch.object(sys.modules["openai"], "AsyncOpenAI", MagicMock()):
        provider = create_provider()
    assert isinstance(provider, OpenAIProvider)


def test_create_provider_gemini(monkeypatch):
    monkeypatch.setenv("ANALYSIS_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    with patch.object(sys.modules["google.generativeai"], "configure", MagicMock()):
        provider = create_provider()
    assert isinstance(provider, GeminiProvider)


def test_create_provider_unknown_raises(monkeypatch):
    monkeypatch.setenv("ANALYSIS_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="지원하지 않는"):
        create_provider()


def test_create_provider_no_key_raises(monkeypatch):
    monkeypatch.setenv("ANALYSIS_PROVIDER", "claude")
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="CLAUDE_API_KEY"):
        create_provider()


def test_claude_provider_fallback_anthropic_key(monkeypatch):
    """CLAUDE_API_KEY 없고 ANTHROPIC_API_KEY만 있을 때 fallback."""
    monkeypatch.setenv("ANALYSIS_PROVIDER", "claude")
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fallback")
    with patch("anthropic.AsyncAnthropic") as mock:
        create_provider()
    mock.assert_called_once_with(api_key="sk-ant-fallback")


def test_default_model_per_provider(monkeypatch):
    """ANALYSIS_MODEL 미설정 시 provider별 기본 모델 사용."""
    monkeypatch.setenv("ANALYSIS_PROVIDER", "claude")
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-test")
    monkeypatch.delenv("ANALYSIS_MODEL", raising=False)
    with patch("anthropic.AsyncAnthropic"):
        p = create_provider()
    assert p.model == "claude-sonnet-4-6"


def test_custom_model_override(monkeypatch):
    monkeypatch.setenv("ANALYSIS_PROVIDER", "claude")
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-test")
    monkeypatch.setenv("ANALYSIS_MODEL", "claude-opus-4-8")
    with patch("anthropic.AsyncAnthropic"):
        p = create_provider()
    assert p.model == "claude-opus-4-8"


def test_openai_no_key_raises(monkeypatch):
    monkeypatch.setenv("ANALYSIS_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        create_provider()


def test_gemini_no_key_raises(monkeypatch):
    monkeypatch.setenv("ANALYSIS_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        create_provider()


def test_openai_default_model(monkeypatch):
    """ANALYSIS_MODEL 미설정 시 OpenAI 기본 모델 gpt-4o."""
    monkeypatch.setenv("ANALYSIS_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("ANALYSIS_MODEL", raising=False)
    with patch.object(sys.modules["openai"], "AsyncOpenAI", MagicMock()):
        p = create_provider()
    assert p.model == "gpt-4o"


def test_gemini_default_model(monkeypatch):
    """ANALYSIS_MODEL 미설정 시 Gemini 기본 모델 gemini-2.0-flash."""
    monkeypatch.setenv("ANALYSIS_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    monkeypatch.delenv("ANALYSIS_MODEL", raising=False)
    with patch.object(sys.modules["google.generativeai"], "configure", MagicMock()):
        p = create_provider()
    assert p.model_name == "gemini-2.0-flash"
