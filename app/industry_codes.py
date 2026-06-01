"""업종코드 → 한글명 매핑 — Phase 4.2.

`documents/openapi/industry-codes.md` 의 회사 대상 업종코드를 한글명으로 표기하기 위한
작은 조회 모듈. /list 의 매칭업종 컬럼(한글(코드), 세로 목록)에서 사용한다.

- 매핑에 없는 코드는 코드 그대로 노출(폴백).
- 표시는 `한글명(코드)` 형태(예: `소프트웨어사업자(컴퓨터관련서비스사업)(1468)`).
"""

from __future__ import annotations

# 업종코드 → 한글 업종명 (industry-codes.md 기준).
INDUSTRY_NAMES: dict[str, str] = {
    "1426": "소프트웨어사업자(패키지소프트웨어개발·공급사업)",
    "1468": "소프트웨어사업자(컴퓨터관련서비스사업)",
    "1469": "소프트웨어사업자(디지털콘텐츠개발서비스사업)",
    "1470": "소프트웨어사업자(데이터베이스제작및검색서비스사업)",
}


def industry_name(code: str) -> str | None:
    """업종코드의 한글명. 모르는 코드는 None."""
    return INDUSTRY_NAMES.get(code.strip())


def industry_label(code: str) -> str:
    """업종코드를 `한글명(코드)` 라벨로. 모르는 코드는 코드 그대로."""
    code = code.strip()
    name = INDUSTRY_NAMES.get(code)
    return f"{name}({code})" if name else code


def parse_matched_codes(csv: str | None) -> list[str]:
    """CSV(예: `1426,1468`)를 코드 리스트로. 공백/빈 항목 제거, 입력 순서 유지."""
    if not csv:
        return []
    return [c.strip() for c in csv.split(",") if c.strip()]


def matched_labels(csv: str | None) -> list[str]:
    """매칭업종 CSV → `한글명(코드)` 라벨 리스트(세로 표기용)."""
    return [industry_label(c) for c in parse_matched_codes(csv)]
