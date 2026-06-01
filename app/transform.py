"""API 응답 item(dict) → ORM 컬럼 값 변환 — Phase 3.2 / Phase 5.2.

- 입찰: 12번 `getBidPblancListInfoServcPPSSrch` 응답 item 1건 → `BidNotice` 컬럼 값(3.2).
- 사전규격: op15 `getPublicPrcureThngInfoServcPPSSrch` 응답 item 1건 → `PreSpec` 컬럼 값(5.2).

수집/저장(upsert)·페이징·스케줄러는 이 단계에서 다루지 않는다(순수 변환 + 단위테스트).

설계 메모:
- 컬럼 ↔ API 필드 매핑은 `COLUMN_TO_API`/`PRE_SPEC_COLUMN_TO_API`에 명시적으로 둔다
  (models.py 컬럼 주석과 일치).
- 타입별 변환기는 ORM 컬럼 타입을 introspect 해서 고른다(타입 목록 중복 정의 회피).
  - DateTime → parse_datetime / Numeric → parse_decimal / Integer → parse_int /
    그 외(String·Text) → clean_str.
  - 리스트성 Text(원문 보존: purchsObjPrdctList·prdctDtlList·bidNtceNoList)는
    `_preserve_list_or_str` 로 별도 처리.
- 수집 메타데이터(matched_indstryty_cds·collected_at·updated_at)는 호출자가 부여한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import DateTime, Integer, Numeric

from app.models import BidNotice, PreSpec

# 호출자(3.3 collector/repository)가 채우는 수집 메타데이터 — 여기서는 채우지 않는다.
META_COLUMNS: frozenset[str] = frozenset(
    {"matched_indstryty_cds", "collected_at", "updated_at"}
)

# 사전규격(pre_spec) 수집 메타데이터 — 호출자(5.3 collector)가 부여(여기서 스킵).
PRE_SPEC_META_COLUMNS: frozenset[str] = frozenset({"collected_at", "updated_at"})

# 구매대상물품목록: 가변(0..n) → 원문 보존(특수 처리). Text 컬럼이라 일반 타입 분기로는
# clean_str 로 가지만, list 도 들어올 수 있어 별도로 직렬화한다.
PURCHS_OBJ_COLUMN = "purchs_obj_prdct_list"

# 사전규격에서 원문 보존(특수 처리)하는 리스트성 Text 컬럼:
#   prdct_dtl_list(물품상세목록 "[순번^…],…") / bid_ntce_no_list(입찰공고번호 CSV)
PRE_SPEC_LIST_COLUMNS: frozenset[str] = frozenset(
    {"prdct_dtl_list", "bid_ntce_no_list"}
)

# raw_json 은 컬럼 매핑이 아니라 원본 item 전체를 직렬화해 채운다.
RAW_JSON_COLUMN = "raw_json"

# 허용하는 날짜 형식: 초 있음 / 초 없음(실제 응답에 둘 다 존재).
_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


# --- 컬럼 ↔ API 필드 매핑 -------------------------------------------------
# models.py 의 컬럼 주석과 일치. 불규칙 명칭: VAT→vat, ntceInsttOfclEmailAdrs→ntce_instt_ofcl_email.
COLUMN_TO_API: dict[str, str] = {
    # 식별
    "bid_ntce_no": "bidNtceNo",
    "bid_ntce_ord": "bidNtceOrd",
    "unty_ntce_no": "untyNtceNo",
    # 분류·상태
    "bid_ntce_nm": "bidNtceNm",
    "ntce_kind_nm": "ntceKindNm",
    "re_ntce_yn": "reNtceYn",
    "srvce_div_nm": "srvceDivNm",
    "info_biz_yn": "infoBizYn",
    "intrbid_yn": "intrbidYn",
    "bid_methd_nm": "bidMethdNm",
    "cntrct_cncls_mthd_nm": "cntrctCnclsMthdNm",
    "sucsfbid_mthd_nm": "sucsfbidMthdNm",
    "indstryty_lmt_yn": "indstrytyLmtYn",
    "chg_ntce_rsn": "chgNtceRsn",
    # 기관·담당자
    "ntce_instt_cd": "ntceInsttCd",
    "ntce_instt_nm": "ntceInsttNm",
    "dminstt_cd": "dminsttCd",
    "dminstt_nm": "dminsttNm",
    "ntce_instt_ofcl_nm": "ntceInsttOfclNm",
    "ntce_instt_ofcl_tel_no": "ntceInsttOfclTelNo",
    "ntce_instt_ofcl_email": "ntceInsttOfclEmailAdrs",
    # 일정
    "bid_ntce_dt": "bidNtceDt",
    "rgst_dt": "rgstDt",
    "bid_qlfct_rgst_dt": "bidQlfctRgstDt",
    "bid_begin_dt": "bidBeginDt",
    "bid_clse_dt": "bidClseDt",
    "openg_dt": "opengDt",
    "chg_dt": "chgDt",
    # 금액·평가
    "presmpt_prce": "presmptPrce",
    "asign_bdgt_amt": "asignBdgtAmt",
    "vat": "VAT",
    "sucsfbid_lwlt_rate": "sucsfbidLwltRate",
    "tech_ablt_evl_rt": "techAbltEvlRt",
    "bid_prce_evl_rt": "bidPrceEvlRt",
    # 분류·링크
    "pub_prcrmnt_lrgclsfc_nm": "pubPrcrmntLrgclsfcNm",
    "pub_prcrmnt_midclsfc_nm": "pubPrcrmntMidclsfcNm",
    "pub_prcrmnt_clsfc_no": "pubPrcrmntClsfcNo",
    "bid_ntce_url": "bidNtceUrl",
    "bid_ntce_dtl_url": "bidNtceDtlUrl",
    "std_ntce_doc_url": "stdNtceDocUrl",
    # 첨부 규격서 1~10
    **{f"ntce_spec_doc_url{i}": f"ntceSpecDocUrl{i}" for i in range(1, 11)},
    **{f"ntce_spec_file_nm{i}": f"ntceSpecFileNm{i}" for i in range(1, 11)},
    # 구매대상물품목록(특수 처리)
    "purchs_obj_prdct_list": "purchsObjPrdctList",
}


# --- 사전규격 컬럼 ↔ API 필드 매핑 (Phase 5.2) ---------------------------
# models.py 의 PreSpec 컬럼 주석(# apiField)과 1:1. op15 응답 필드 기준.
PRE_SPEC_COLUMN_TO_API: dict[str, str] = {
    # 식별(PK)
    "bf_spec_rgst_no": "bfSpecRgstNo",
    # 분류·기관
    "bsns_div_nm": "bsnsDivNm",
    "ref_no": "refNo",
    "prdct_clsfc_no_nm": "prdctClsfcNoNm",
    "order_instt_nm": "orderInsttNm",
    "rl_dminstt_nm": "rlDminsttNm",
    # 금액
    "asign_bdgt_amt": "asignBdgtAmt",
    # 일정
    "rcpt_dt": "rcptDt",
    "opnin_rgst_clse_dt": "opninRgstClseDt",
    # 담당자
    "ofcl_nm": "ofclNm",
    "ofcl_tel_no": "ofclTelNo",
    # 분류 필터
    "sw_biz_obj_yn": "swBizObjYn",
    # 납품
    "dlvr_tmlmt_dt": "dlvrTmlmtDt",
    "dlvr_daynum": "dlvrDaynum",
    # 첨부 규격서 1~5
    **{f"spec_doc_file_url{i}": f"specDocFileUrl{i}" for i in range(1, 6)},
    # 물품상세·연계(특수 처리)
    "prdct_dtl_list": "prdctDtlList",
    "bid_ntce_no_list": "bidNtceNoList",
    # 일정(등록·변경)
    "rgst_dt": "rgstDt",
    "chg_dt": "chgDt",
}


# --- 값 변환기 -----------------------------------------------------------
def clean_str(value: Any) -> str | None:
    """좌우 공백 제거. 빈 문자열·공백만·None → None."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def parse_datetime(value: Any) -> datetime | None:
    """ "YYYY-MM-DD HH:MM:SS" / "YYYY-MM-DD HH:MM" → datetime.

    빈값·형식 불일치 → None(예외를 던지지 않는다).
    """
    s = clean_str(value)
    if s is None:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_decimal(value: Any) -> Decimal | None:
    """숫자 문자열 → Decimal. 콤마 제거. 빈값·변환 실패 → None."""
    s = clean_str(value)
    if s is None:
        return None
    s = s.replace(",", "")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def parse_int(value: Any) -> int | None:
    """정수 문자열 → int. 콤마 제거. 빈값·변환 실패 → None(예외를 던지지 않는다).

    pre_spec 의 dlvr_daynum(Integer) 대응.
    """
    s = clean_str(value)
    if s is None:
        return None
    s = s.replace(",", "")
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _preserve_list_or_str(value: Any) -> str | None:
    """리스트성 Text 컬럼(원문 보존) → Text 저장값.

    값이 list/dict 면 JSON 직렬화, 문자열이면 그대로(공백만이면 None).
    입찰의 purchsObjPrdctList·사전규격의 prdctDtlList/bidNtceNoList 공용.
    """
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return clean_str(value)


# 기존 이름 호환(입찰 변환 경로 동작 불변) — 범용 헬퍼의 별칭.
_convert_purchs_obj = _preserve_list_or_str


# 컬럼 타입 → 변환기 선택(introspection). 한 곳에서만 관리.
# SQLAlchemy 에서 Integer 와 Numeric 은 별개 타입이라 둘 다 분기.
# 순서: DateTime / Numeric / Integer / 그 외(String·Text → clean_str).
def _converter_for_column(column) -> Any:
    col_type = column.type
    if isinstance(col_type, DateTime):
        return parse_datetime
    if isinstance(col_type, Numeric):
        return parse_decimal
    if isinstance(col_type, Integer):
        return parse_int
    return clean_str


# --- 공개 함수 -----------------------------------------------------------
def item_to_bid_notice_values(item: dict) -> dict:
    """API 응답 item 1건 → {bid_notice 컬럼명: 변환된 값} dict 반환.

    - API에서 유래한 컬럼만 채운다. 수집 메타데이터는 호출자가 부여한다.
    - raw_json 은 원본 item 전체를 json.dumps(ensure_ascii=False)로 직렬화해 저장.
    - bid_ntce_no(PK)가 비어 있으면 ValueError(상위에서 스킵/로깅).
    """
    if not isinstance(item, dict):
        raise ValueError(f"item 은 dict 여야 합니다: {type(item).__name__}")

    values: dict[str, Any] = {}

    for column in BidNotice.__table__.columns:
        name = column.name
        if name in META_COLUMNS or name == RAW_JSON_COLUMN:
            continue

        api_field = COLUMN_TO_API.get(name)
        if api_field is None:
            # 매핑에 없는 API 비유래 컬럼(혹시 모를 누락)은 건너뛴다.
            continue

        raw = item.get(api_field)  # 없는 키는 None — KeyError 로 죽지 않게

        if name == PURCHS_OBJ_COLUMN:
            values[name] = _convert_purchs_obj(raw)
        else:
            values[name] = _converter_for_column(column)(raw)

    # PK 누락 검증
    if values.get("bid_ntce_no") is None:
        raise ValueError("bidNtceNo(PK)가 비어 있어 변환할 수 없습니다.")

    # 원본 item 전체 보존
    values[RAW_JSON_COLUMN] = json.dumps(item, ensure_ascii=False)

    return values


def item_to_pre_spec_values(item: dict) -> dict:
    """사전규격 op15 응답 item 1건 → {pre_spec 컬럼명: 변환된 값} dict 반환.

    `item_to_bid_notice_values` 와 동일 구조(순수 변환).
    - API에서 유래한 컬럼만 채운다. 수집 메타데이터(collected_at·updated_at)는
      호출자(5.3 collector)가 부여한다.
    - prdct_dtl_list·bid_ntce_no_list 는 list/dict 면 JSON 직렬화, 문자열이면 원문 보존.
    - raw_json 은 원본 item 전체를 json.dumps(ensure_ascii=False)로 직렬화해 저장.
    - bf_spec_rgst_no(PK)가 비어 있으면 ValueError(상위에서 스킵/로깅).
    """
    if not isinstance(item, dict):
        raise ValueError(f"item 은 dict 여야 합니다: {type(item).__name__}")

    values: dict[str, Any] = {}

    for column in PreSpec.__table__.columns:
        name = column.name
        if name in PRE_SPEC_META_COLUMNS or name == RAW_JSON_COLUMN:
            continue

        api_field = PRE_SPEC_COLUMN_TO_API.get(name)
        if api_field is None:
            # 매핑에 없는 API 비유래 컬럼(혹시 모를 누락)은 건너뛴다.
            continue

        raw = item.get(api_field)  # 없는 키는 None — KeyError 로 죽지 않게

        if name in PRE_SPEC_LIST_COLUMNS:
            values[name] = _preserve_list_or_str(raw)
        else:
            values[name] = _converter_for_column(column)(raw)

    # PK 누락 검증
    if values.get("bf_spec_rgst_no") is None:
        raise ValueError("bfSpecRgstNo(PK)가 비어 있어 변환할 수 없습니다.")

    # 원본 item 전체 보존
    values[RAW_JSON_COLUMN] = json.dumps(item, ensure_ascii=False)

    return values
