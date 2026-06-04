"""RFP 분석 에이전트"""

from datetime import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.analysis.provider import AnalysisProvider, create_provider
from app.analysis.rfp_schema import RFPAnalysis

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "rfp_analysis.txt"
_RFP_LOG_DIR = Path("rfp_analyzer_logs")


class RFPAnalyzer:
    """RFP 문서 분석 — 프로바이더 주입 방식."""

    def __init__(self, provider: Optional[AnalysisProvider] = None) -> None:
        self._provider = provider or create_provider()

    async def execute(
        self,
        input_data: Dict[str, Any],
        progress_callback: Optional[Callable] = None,
    ) -> RFPAnalysis:
        """
        RFP 문서를 분석하여 핵심 정보 추출

        Args:
            input_data: {
                "text": str  (파싱된 전체 텍스트)
                "tables": List[Dict]  (선택)
            }
            progress_callback: 진행 상황 콜백

        Returns:
            RFPAnalysis: 분석된 RFP 정보
        """
        input_text = input_data.get("text", "")
        logger.info("[RFP] 분석 시작: 텍스트 %d자", len(input_text))
        if progress_callback:
            progress_callback(
                {"step": 1, "total": 3, "message": "RFP 텍스트 준비 중..."}
            )

        # 프롬프트 로드
        system_prompt = self._load_prompt()
        if not system_prompt:
            system_prompt = self._get_default_system_prompt()

        # 입력 데이터 준비
        raw_text = self._truncate_text(input_text, 25000)
        tables_json = json.dumps(
            input_data.get("tables", [])[:10], ensure_ascii=False, indent=2
        )[:5000]

        user_message = f"""
다음 RFP(제안요청서) 문서를 분석해주세요.

## 문서 텍스트
{raw_text}

## 테이블 데이터
{tables_json}

위 내용을 분석하여 다음 JSON 형식으로 응답해주세요:

```json
{{
    "project_name": "프로젝트명",
    "client_name": "발주처명",
    "project_overview": "프로젝트 개요 (2-3문장)",
    "project_type": "marketing_pr / event / it_system / public / consulting / general 중 택1",
    "key_requirements": [
        {{"category": "기능/비기능/기술/관리", "requirement": "요구사항", "priority": "필수/선택"}}
    ],
    "technical_requirements": [
        {{"category": "기술", "requirement": "기술 요구사항", "priority": "필수/선택"}}
    ],
    "evaluation_criteria": [
        {{"category": "분야", "item": "평가 항목", "weight": 배점}}
    ],
    "deliverables": [
        {{"name": "산출물명", "phase": "단계", "description": "설명"}}
    ],
    "timeline": {{
        "total_duration": "전체 기간",
        "phases": [{{"name": "단계명", "duration": "기간"}}]
    }},
    "budget": {{
        "total_budget": "예산 (있는 경우)",
        "notes": "예산 관련 참고사항"
    }},
    "key_success_factors": ["핵심 성공 요인 1", "핵심 성공 요인 2"],
    "potential_risks": ["리스크 1", "리스크 2"],
    "winning_strategy": "수주를 위한 전략 제안",
    "differentiation_points": ["차별화 포인트 1", "차별화 포인트 2"],
    "pain_points": [
        "발주처 핵심 고민 1 (RFP 행간에서 추출)",
        "발주처 핵심 고민 2",
        "발주처 핵심 고민 3"
    ],
    "hidden_needs": [
        "RFP에 명시되지 않은 숨겨진 니즈 1",
        "RFP에 명시되지 않은 숨겨진 니즈 2"
    ],
    "evaluation_strategy": {{
        "high_weight_items": [
            {{"item": "배점 높은 평가 항목", "weight": 30, "proposal_emphasis": "이 항목에 대응하기 위해 제안서에서 강조할 내용"}}
        ],
        "emphasis_mapping": {{
            "Phase 2 (INSIGHT)": "이 Phase에서 강조할 평가 항목",
            "Phase 4 (ACTION)": "이 Phase에서 강조할 평가 항목",
            "Phase 6 (WHY US)": "이 Phase에서 강조할 평가 항목"
        }}
    }},
    "win_theme_candidates": [
        {{
            "name": "Win Theme 이름 (짧은 키워드)",
            "rationale": "이 Win Theme이 효과적인 이유",
            "rfp_alignment": "연결되는 RFP 요구사항/평가 기준"
        }},
        {{
            "name": "Win Theme 2",
            "rationale": "이유",
            "rfp_alignment": "연결 요구사항"
        }},
        {{
            "name": "Win Theme 3",
            "rationale": "이유",
            "rfp_alignment": "연결 요구사항"
        }}
    ],
    "competitive_landscape": "예상 경쟁 환경 분석 (어떤 유형의 회사가 경쟁할지, 차별화 가능 영역)"
}}
```
"""

        if progress_callback:
            progress_callback(
                {"step": 2, "total": 3, "message": "AI 분석 수행 중..."}
            )

        # 프로바이더 호출
        response = await self._provider.complete(system_prompt, user_message)

        if progress_callback:
            progress_callback(
                {"step": 3, "total": 3, "message": "분석 결과 정리 중..."}
            )

        # JSON 파싱
        analysis_data, json_parse_success = self._extract_json_with_status(response)
        self._write_analysis_log(
            system_prompt=system_prompt,
            user_message=user_message,
            response=response,
            analysis_data=analysis_data,
            input_text_length=len(input_text),
            json_parse_success=json_parse_success,
        )

        # 기본값 설정
        analysis_data.setdefault("project_name", "프로젝트명 미확인")
        analysis_data.setdefault("client_name", "발주처 미확인")
        analysis_data.setdefault("project_overview", "")

        logger.info("[RFP] 분석 완료: project_name=%s", analysis_data.get("project_name", "?"))

        return RFPAnalysis(**analysis_data)

    def _load_prompt(self) -> str:
        """프롬프트 파일 로드"""
        if not _PROMPT_PATH.exists():
            logger.warning(f"프롬프트 파일 없음: {_PROMPT_PATH}")
            return ""
        return _PROMPT_PATH.read_text(encoding="utf-8")

    def _get_default_system_prompt(self) -> str:
        """기본 시스템 프롬프트"""
        return """당신은 경쟁 입찰에서 승리하는 제안서를 위한 RFP 분석 전문가입니다.
단순 정보 추출을 넘어, 수주를 위한 전략적 분석을 수행합니다.

## 분석 영역

### 기본 정보 추출
1. 프로젝트 기본 정보 (이름, 발주처, 개요)
2. 요구사항 (기능, 비기능, 기술)
3. 평가 기준 및 배점
4. 산출물 목록
5. 일정 및 예산 정보

### 전략적 분석 (★핵심)
6. **프로젝트 유형 분류**: marketing_pr, event, it_system, public, consulting, general
7. **Pain Point 추출**: 발주처가 겪고 있는 핵심 고민 3~5개
8. **평가 기준 전략화**: 배점 높은 항목 → 제안서 강조 포인트 변환
9. **Win Theme 후보 도출**: RFP에 직접 대응하는 핵심 수주 전략 메시지 3개
10. **숨겨진 니즈**: RFP에 명시되지 않았지만 발주처가 원하는 것

## 분석 원칙
- 명시적 정보 추출 + 행간 해석 병행
- 불확실한 정보는 "미확인" 표시
- 모든 분석에 근거 제시

응답은 반드시 유효한 JSON 형식으로 제공해주세요."""

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """텍스트에서 JSON 추출"""
        data, _ = self._extract_json_with_status(text)
        return data

    def _extract_json_with_status(self, text: str) -> tuple[Dict[str, Any], bool]:
        """텍스트에서 JSON을 추출하고 성공 여부를 함께 반환."""
        stripped = text.strip()

        candidates = [stripped]

        for pattern in [
            r"```json\s*([\s\S]*?)\s*```",
            r"```\s*([\s\S]*?)\s*```",
        ]:
            for match in re.finditer(pattern, stripped):
                candidates.append(match.group(1).strip())

        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and start < end:
            candidates.append(stripped[start:end + 1])

        for json_str in candidates:
            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed, True

        logger.error("JSON 추출 실패")
        return {}, False

    def _truncate_text(self, text: str, max_chars: int = 30000) -> str:
        """텍스트 길이 제한"""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n\n... (텍스트가 잘렸습니다)"

    def _write_analysis_log(
        self,
        *,
        system_prompt: str,
        user_message: str,
        response: str,
        analysis_data: Dict[str, Any],
        input_text_length: int,
        json_parse_success: bool,
    ) -> None:
        """RFP 분석 request/response 진단 로그 저장."""
        if not _rfp_log_enabled():
            return

        try:
            _RFP_LOG_DIR.mkdir(parents=True, exist_ok=True)
            created_at = datetime.now()
            slug = _slugify_project_name(analysis_data.get("project_name"))
            path = _RFP_LOG_DIR / f"{created_at:%Y%m%d-%H%M%S-%f}_{slug}.md"
            provider = self._provider
            model = (
                getattr(provider, "model", None)
                or getattr(provider, "model_name", None)
                or ""
            )
            system_prompt_bytes = len(system_prompt.encode("utf-8"))
            user_message_bytes = len(user_message.encode("utf-8"))
            request_chars = len(system_prompt) + len(user_message)
            request_bytes = system_prompt_bytes + user_message_bytes
            response_bytes = len(response.encode("utf-8"))

            content = "\n".join(
                [
                    "# RFP Analyzer Log",
                    "",
                    "## Meta",
                    f"- 생성 시각: {created_at.isoformat(timespec='seconds')}",
                    f"- provider: {type(provider).__name__}",
                    f"- model: {model}",
                    f"- 입력 텍스트 길이: {input_text_length}",
                    f"- 응답 길이: {len(response)}",
                    f"- JSON 파싱 성공: {json_parse_success}",
                    "",
                    "## 데이터 크기",
                    f"- Request — system prompt: {len(system_prompt)}자 / {system_prompt_bytes} bytes",
                    f"- Request — user message: {len(user_message)}자 / {user_message_bytes} bytes",
                    f"- Request 합계: {request_chars}자 / {request_bytes} bytes",
                    f"- Response: {len(response)}자 / {response_bytes} bytes",
                    (
                        "- 토큰(근사·참고용, 매우 근사; 정확값 아님): "
                        f"요청 ≈ {request_bytes // 4}, 응답 ≈ {response_bytes // 4}"
                    ),
                    "",
                    "## Request — system prompt",
                    "````",
                    system_prompt,
                    "````",
                    "",
                    "## Request — user message",
                    "````",
                    user_message,
                    "````",
                    "",
                    "## Response (raw)",
                    "````",
                    response,
                    "````",
                    "",
                ]
            )
            path.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.warning("RFP 분석 로그 저장 실패: %s", e)


def _rfp_log_enabled() -> bool:
    """USER_RFP_ANALYZER_LOG env가 truthy일 때만 진단 로그 저장."""
    val = os.environ.get("USER_RFP_ANALYZER_LOG", "false").strip().lower()
    return val in ("1", "true", "yes", "on")


def _slugify_project_name(project_name: Any) -> str:
    """project_name을 파일명에 안전한 slug로 변환."""
    raw = str(project_name or "").strip()
    raw = re.sub(r"\s+", "_", raw)
    slug = re.sub(r"[^0-9A-Za-z가-힣._-]", "", raw).strip("._-")
    return (slug[:80] or "unparsed")
