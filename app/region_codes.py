"""참가제한지역코드(prtcptLmtRgnCd) 매핑 — Phase 4.3.

12번 용역 검색조건 조회(`getBidPblancListInfoServcPPSSrch`)의 요청 파라미터
`prtcptLmtRgnCd`(참가제한지역코드) 코드↔이름 매핑과 `/config` select 옵션 순서를 제공한다.

코드표 근거: `documents/openapi/endpoints/12-getBidPblancListInfoServcPPSSrch.md`.

의미(중요):
- `""`(빈값)  : 전체 — 필터하지 않음(지역제한 무관, 기존 동작).
- `"00"`       : 전국 — 지역제한을 설정하지 않은 공고만(신규 설치 기본값).
- `"11"`~`"99"`: 특정 지역으로 제한된 공고.

`""`는 코드가 아니므로 REGION_CODES 에 넣지 않는다(저장 검증은 REGION_CODES 키 ∪ {""}).
"""

from __future__ import annotations

# 코드 → 지역명. 빈값("")은 "전체"의 의미라 코드 집합에 포함하지 않는다.
REGION_CODES: dict[str, str] = {
    "00": "전국",
    "11": "서울특별시",
    "26": "부산광역시",
    "27": "대구광역시",
    "28": "인천광역시",
    "29": "광주광역시",
    "30": "대전광역시",
    "31": "울산광역시",
    "36": "세종특별자치시",
    "41": "경기도",
    "42": "강원도",
    "43": "충청북도",
    "44": "충청남도",
    "45": "전라북도",
    "46": "전라남도",
    "47": "경상북도",
    "48": "경상남도",
    "50": "제주도",
    "51": "강원특별자치도",
    "52": "전북특별자치도",
    "99": "기타",
}

# /config <select> 표시 순서: 전체("") → 전국("00") → 특정 지역(코드 오름차순).
# 각 원소는 (코드, 표시 라벨).
REGION_OPTIONS: list[tuple[str, str]] = (
    [
        ("", "전체 (지역제한 무관)"),
        ("00", "전국 (지역제한 없는 공고만)"),
    ]
    + [
        (code, REGION_CODES[code])
        for code in sorted(REGION_CODES)
        if code != "00"
    ]
)

# 저장 시 허용하는 값 집합: 코드 전체 ∪ {""}(전체=필터 안 함).
ALLOWED_REGION_VALUES: frozenset[str] = frozenset(REGION_CODES) | {""}


def region_name(code: str | None) -> str:
    """코드 → 지역명. 빈값/None 은 '전체', 미정의 코드는 코드 그대로."""
    if code is None or code == "":
        return "전체"
    return REGION_CODES.get(code, code)


def is_valid_region(code: str | None) -> bool:
    """저장 허용 여부 — 빈값("") 또는 정의된 코드면 True."""
    return (code or "") in ALLOWED_REGION_VALUES
