"""나라장터 입찰공고정보서비스(BidPublicInfoService) API 호출 모듈.

Phase 1: 각 엔드포인트를 실제 호출해 응답 형태를 확인하기 위한 독립 모듈.
FastAPI/스케줄러에 의존하지 않으며, 2·3단계에서 그대로 재사용한다.

- 서비스 키 등 시크릿은 코드에 두지 않고 .env(PROCUREMENT_SERVICE_KEY)에서만 로드한다.
- HTTP GET 호출 → XML(기본) 또는 JSON(type=json) 응답을 반환.
- resultCode를 확인해 정상/데이터없음/에러를 구분한다.
"""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import xmltodict
from dotenv import load_dotenv
import os

# .env 로드 (프로젝트 루트의 .env)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

SERVICE_KEY = os.getenv("PROCUREMENT_SERVICE_KEY", "").strip()
BASE_URL = os.getenv(
    "PROCUREMENT_BASE_URL",
    "http://apis.data.go.kr/1230000/ad/BidPublicInfoService",
).strip().rstrip("/")
DEFAULT_RESPONSE_TYPE = os.getenv("PROCUREMENT_RESPONSE_TYPE", "json").strip() or "json"


# --- 에러코드 (00-common.md 참고) ---------------------------------------
ERROR_CODES: dict[str, str] = {
    "00": "정상",
    "01": "Application Error (제공기관 서비스 상태 불량)",
    "02": "DB Error (제공기관 서비스 상태 불량)",
    "03": "No Data (데이터 없음)",
    "04": "HTTP Error (제공기관 서비스 상태 불량)",
    "05": "service time out (제공기관 서비스 상태 불량)",
    "06": "날짜 Format 에러 (YYYYMMDDHHMM 확인)",
    "07": "입력범위값 초과",
    "08": "필수값 입력 에러",
    "10": "잘못된 요청 파라미터 (ServiceKey 누락)",
    "11": "필수 요청 파라미터 없음",
    "12": "서비스 없음/폐기",
    "20": "서비스 접근 거부 (활용승인 안 됨)",
    "22": "요청 제한 횟수 초과 (일일 한도)",
    "30": "등록되지 않은 서비스 키 (키 오류 또는 URL 인코딩 문제)",
    "31": "기한 만료된 서비스 키",
    "32": "등록되지 않은 도메인/IP",
}


# --- 파라미터 명세 -------------------------------------------------------
@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    placeholder: str = ""
    help: str = ""


# 공통 파라미터 (모든 엔드포인트 공유). serviceKey/type 은 화면에서 직접 받지 않고
# 모듈이 자동 주입/선택한다.
COMMON_PARAMS: list[ParamSpec] = [
    ParamSpec("inqryDiv", "조회구분", "1", "검색계열: 1=공고게시일시, 2=개찰일시 / 목록계열: 1=등록일시,2=공고번호,3=변경일시"),
    ParamSpec("inqryBgnDt", "조회시작일시", "202505010000", "YYYYMMDDHHMM (일시 기준일 때 필수)"),
    ParamSpec("inqryEndDt", "조회종료일시", "202505012359", "YYYYMMDDHHMM (일시 기준일 때 필수)"),
    ParamSpec("pageNo", "페이지번호", "1"),
    ParamSpec("numOfRows", "페이지당건수", "10"),
    ParamSpec("bidNtceNo", "입찰공고번호", "", "inqryDiv=2(공고번호 조회)일 때 필수"),
]

# 검색조건 조회(11~14)의 추가 검색 파라미터. 비우면 전송하지 않는다.
SEARCH_PARAMS: list[ParamSpec] = [
    ParamSpec("bidNtceNm", "입찰공고명", "", "일부 입력 가능"),
    ParamSpec("ntceInsttNm", "공고기관명", "", "일부 입력 가능"),
    ParamSpec("dminsttNm", "수요기관명", "", "일부 입력 가능"),
    ParamSpec("indstrytyCd", "업종코드", "", "예: 6202"),
    ParamSpec("indstrytyNm", "업종명", "", "일부 입력 가능"),
    ParamSpec("prtcptLmtRgnCd", "참가제한지역코드", "", "예: 28=인천, 00=전국"),
    ParamSpec("presmptPrceBgn", "추정가격시작", "", "이상(원)"),
    ParamSpec("presmptPrceEnd", "추정가격종료", "", "이하(원)"),
    ParamSpec("intrntnlDivCd", "국제입찰구분", "", "국내:1, 국제:2"),
    # 입찰마감제외(bidClseExcpYn)는 화면의 조회일시 영역에서 체크박스로 별도 렌더링한다.
    ParamSpec("bidClseExcpYn", "입찰마감제외", "", "Y=입찰마감 건 제외"),
]


@dataclass(frozen=True)
class EndpointSpec:
    no: int
    operation: str
    label: str            # 화면 표시용
    kind: str             # "search" (검색조건) | "list" (기본 목록)
    extra_params: list[ParamSpec] = field(default_factory=list)


# 화면에 등록할 엔드포인트: 검색조건 조회 11~14 (핵심) + 기본 목록조회 1~4 (보조)
ENDPOINTS: list[EndpointSpec] = [
    EndpointSpec(11, "getBidPblancListInfoCnstwkPPSSrch", "11 · 공사 (검색조건)", "search", SEARCH_PARAMS),
    EndpointSpec(12, "getBidPblancListInfoServcPPSSrch", "12 · 용역 (검색조건)", "search", SEARCH_PARAMS),
    EndpointSpec(13, "getBidPblancListInfoFrgcptPPSSrch", "13 · 외자 (검색조건)", "search", SEARCH_PARAMS),
    EndpointSpec(14, "getBidPblancListInfoThngPPSSrch", "14 · 물품 (검색조건)", "search", SEARCH_PARAMS),
    EndpointSpec(1, "getBidPblancListInfoCnstwk", "1 · 공사 (기본 목록)", "list"),
    EndpointSpec(2, "getBidPblancListInfoServc", "2 · 용역 (기본 목록)", "list"),
    EndpointSpec(3, "getBidPblancListInfoFrgcpt", "3 · 외자 (기본 목록)", "list"),
    EndpointSpec(4, "getBidPblancListInfoThng", "4 · 물품 (기본 목록)", "list"),
]

ENDPOINTS_BY_OP: dict[str, EndpointSpec] = {e.operation: e for e in ENDPOINTS}


# --- 검증 ----------------------------------------------------------------
_DATETIME_RE = re.compile(r"^\d{12}$")


class ApiClientError(Exception):
    """호출 전 검증 실패 등 클라이언트 측 오류."""


def validate_datetime(value: str, field_name: str) -> None:
    """YYYYMMDDHHMM 형식 검증 (12자리 숫자)."""
    if not _DATETIME_RE.match(value):
        raise ApiClientError(f"{field_name}는 YYYYMMDDHHMM(12자리 숫자) 형식이어야 합니다: '{value}'")


# --- 호출 결과 -----------------------------------------------------------
@dataclass
class ApiResult:
    operation: str
    request_url: str
    sent_params: dict[str, str]        # serviceKey 제외 (화면 표시용)
    response_type: str                 # "json" | "xml"
    status_code: int
    raw_text: str                      # 응답 원문
    parsed: Any | None                 # dict (정상 파싱 시) / None
    result_code: str | None
    result_msg: str | None
    items: list[dict] = field(default_factory=list)
    total_count: str | None = None
    error: str | None = None           # 처리 중 발생한 메시지

    @property
    def result_code_desc(self) -> str:
        if self.result_code is None:
            return ""
        return ERROR_CODES.get(self.result_code, "알 수 없는 코드")

    @property
    def is_ok(self) -> bool:
        return self.result_code == "00"


def _extract_from_parsed(parsed: Any) -> tuple[str | None, str | None, list[dict], str | None]:
    """파싱된 dict(JSON/XML 공통 구조)에서 헤더/아이템/전체건수 추출.

    구조: response -> {header:{resultCode,resultMsg}, body:{items:..., totalCount}}
    items 는 응답에 따라 dict 1건, list, 또는 {item:[...]} 형태일 수 있다.
    """
    if not isinstance(parsed, dict):
        return None, None, [], None

    response = parsed.get("response", parsed)
    if not isinstance(response, dict):
        return None, None, [], None

    header = response.get("header") or {}
    body = response.get("body") or {}

    result_code = header.get("resultCode")
    result_msg = header.get("resultMsg")
    total_count = body.get("totalCount") if isinstance(body, dict) else None

    items_node: Any = body.get("items") if isinstance(body, dict) else None
    items: list[dict] = []
    if isinstance(items_node, dict):
        # XML 변환 시 <items><item>..</item></items> → {"item": ...}
        inner = items_node.get("item", items_node)
        if isinstance(inner, list):
            items = [i for i in inner if isinstance(i, dict)]
        elif isinstance(inner, dict):
            items = [inner]
    elif isinstance(items_node, list):
        items = [i for i in items_node if isinstance(i, dict)]

    return (
        str(result_code) if result_code is not None else None,
        str(result_msg) if result_msg is not None else None,
        items,
        str(total_count) if total_count is not None else None,
    )


def build_params(operation: str, raw: dict[str, str], response_type: str) -> dict[str, str]:
    """화면 입력(raw)에서 빈 값을 제거하고 호출 파라미터를 조립.

    serviceKey 는 여기서 넣지 않고 call_endpoint 에서 주입한다(로그/표시 분리).
    """
    spec = ENDPOINTS_BY_OP.get(operation)
    if spec is None:
        raise ApiClientError(f"알 수 없는 엔드포인트: {operation}")

    # 이 엔드포인트에서 허용되는 파라미터만 통과 (예: 검색 파라미터를 목록조회로 보내지 않음)
    allowed = {p.name for p in COMMON_PARAMS} | {p.name for p in spec.extra_params}

    params: dict[str, str] = {}
    for key, value in raw.items():
        if key not in allowed:
            continue
        if value is None:
            continue
        v = str(value).strip()
        if v == "":
            continue
        params[key] = v

    # 날짜 형식 검증 (입력된 경우에만)
    for dt_field in ("inqryBgnDt", "inqryEndDt"):
        if dt_field in params:
            validate_datetime(params[dt_field], dt_field)

    if response_type == "json":
        params["type"] = "json"
    return params


def call_endpoint(
    operation: str,
    raw_params: dict[str, str],
    response_type: str | None = None,
    timeout: float = 20.0,
) -> ApiResult:
    """엔드포인트를 호출하고 결과를 ApiResult 로 반환.

    operation: ENDPOINTS 의 operation 문자열
    raw_params: 화면에서 받은 파라미터 dict (빈 값 포함 가능)
    response_type: "json" | "xml" (None이면 .env 기본값)
    """
    if not SERVICE_KEY:
        raise ApiClientError(
            "PROCUREMENT_SERVICE_KEY 가 비어 있습니다. .env 에 서비스 키를 입력하세요."
        )

    spec = ENDPOINTS_BY_OP.get(operation)
    if spec is None:
        raise ApiClientError(f"알 수 없는 엔드포인트: {operation}")

    rtype = (response_type or DEFAULT_RESPONSE_TYPE).lower()
    if rtype not in ("json", "xml"):
        rtype = "json"

    sent_params = build_params(operation, raw_params, rtype)
    url = f"{BASE_URL}/{operation}"

    # serviceKey 는 표시/로그에 포함하지 않기 위해 별도로 합친다.
    request_params = {"serviceKey": SERVICE_KEY, **sent_params}

    try:
        resp = httpx.get(url, params=request_params, timeout=timeout)
    except httpx.HTTPError as exc:
        return ApiResult(
            operation=operation,
            request_url=url,
            sent_params=sent_params,
            response_type=rtype,
            status_code=-1,
            raw_text="",
            parsed=None,
            result_code=None,
            result_msg=None,
            error=f"HTTP 요청 실패: {exc}",
        )

    raw_text = resp.text
    parsed: Any | None = None
    parse_error: str | None = None

    try:
        if rtype == "json":
            parsed = resp.json()
        else:
            parsed = xmltodict.parse(raw_text)
    except (_json.JSONDecodeError, ValueError, Exception) as exc:  # noqa: BLE001
        # data.go.kr 은 키 오류 등에서 JSON 요청에도 XML 에러를 돌려주는 경우가 있어
        # 반대 포맷으로 한 번 더 시도한다.
        try:
            parsed = xmltodict.parse(raw_text) if rtype == "json" else resp.json()
        except Exception:  # noqa: BLE001
            parse_error = f"응답 파싱 실패: {exc}"

    result_code, result_msg, items, total_count = _extract_from_parsed(parsed)

    return ApiResult(
        operation=operation,
        request_url=url,
        sent_params=sent_params,
        response_type=rtype,
        status_code=resp.status_code,
        raw_text=raw_text,
        parsed=parsed,
        result_code=result_code,
        result_msg=result_msg,
        items=items,
        total_count=total_count,
        error=parse_error,
    )


def pretty_raw(result: ApiResult) -> str:
    """응답 원문을 보기 좋게 정렬해 반환 (표시용)."""
    if result.parsed is not None and result.response_type == "json":
        try:
            return _json.dumps(result.parsed, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            pass
    return result.raw_text
