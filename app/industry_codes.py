"""업종코드 → 한글명 매핑 — Phase 4.2.

`documents/openapi/industry-codes.md` 의 회사 대상 업종코드를 한글명으로 표기하기 위한
작은 조회 모듈. /list 의 매칭업종 컬럼(한글(코드), 세로 목록)에서 사용한다.

- 매핑에 없는 코드는 코드 그대로 노출(폴백).
- 표시는 단축명 `업무명 [코드]` 형태(예: `컴퓨터관련서비스사업 [1468]`).
  바깥 래퍼(`소프트웨어사업자(…)`)는 벗기고 안쪽 업무명 + ` [코드]` 만 노출(셀 폭 절약).
- 전체명(`industry_name`)은 tooltip 등에 쓰도록 유지한다.
"""

from __future__ import annotations

# 업종코드 → 한글 업종명(전체명, industry-codes.md 기준). tooltip 등 전체 표기용.
INDUSTRY_NAMES: dict[str, str] = {
    "1426": "소프트웨어사업자(패키지소프트웨어개발·공급사업)",
    "1468": "소프트웨어사업자(컴퓨터관련서비스사업)",
    "1469": "소프트웨어사업자(디지털콘텐츠개발서비스사업)",
    "1470": "소프트웨어사업자(데이터베이스제작및검색서비스사업)",
}

# 업종코드 → 단축 업무명(바깥 `소프트웨어사업자(…)` 래퍼를 벗긴 안쪽 업무명). 셀 표기용.
INDUSTRY_SHORT_NAMES: dict[str, str] = {
    "1426": "패키지소프트웨어개발·공급사업",
    "1468": "컴퓨터관련서비스사업",
    "1469": "디지털콘텐츠개발서비스사업",
    "1470": "데이터베이스제작및검색서비스사업",
}


def industry_name(code: str) -> str | None:
    """업종코드의 전체 한글명. 모르는 코드는 None."""
    return INDUSTRY_NAMES.get(code.strip())


def industry_label(code: str) -> str:
    """업종코드를 단축 라벨 `업무명 [코드]` 로. 모르는 코드는 코드 그대로."""
    code = code.strip()
    short = INDUSTRY_SHORT_NAMES.get(code)
    return f"{short} [{code}]" if short else code


def parse_matched_codes(csv: str | None) -> list[str]:
    """CSV(예: `1426,1468`)를 코드 리스트로. 공백/빈 항목 제거, 입력 순서 유지."""
    if not csv:
        return []
    return [c.strip() for c in csv.split(",") if c.strip()]


def matched_labels(csv: str | None) -> list[str]:
    """매칭업종 CSV → 단축 라벨 `업무명 [코드]` 리스트(세로 표기용)."""
    return [industry_label(c) for c in parse_matched_codes(csv)]


def industry_full_label(code: str) -> str:
    """tooltip 용 전체 라벨 `전체명 [코드]`. 모르는 코드는 코드 그대로."""
    code = code.strip()
    name = INDUSTRY_NAMES.get(code)
    return f"{name} [{code}]" if name else code


def matched_label_pairs(csv: str | None) -> list[tuple[str, str]]:
    """매칭업종 CSV → (단축 표시 라벨, tooltip 전체 라벨) 쌍 리스트(세로 표기용)."""
    return [
        (industry_label(c), industry_full_label(c)) for c in parse_matched_codes(csv)
    ]
