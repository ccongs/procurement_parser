"""transform.item_to_pre_spec_values 단위테스트 — Phase 5.2.

사전규격 op15 `getPublicPrcureThngInfoServcPPSSrch` 응답 item(camelCase dict) →
`PreSpec` 컬럼 값 변환을 검증한다. 순수 변환(DB·네트워크 미접근), 결정적(고정 입력값만).
지시문 4장의 대표 케이스(날짜 2형식·금액·정수·리스트성·빈값→None·PK검증·raw_json)를 모두 포함.
실행: `pytest tests/test_transform_prespec.py`
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest

from app.transform import (
    PRE_SPEC_COLUMN_TO_API,
    PRE_SPEC_META_COLUMNS,
    item_to_pre_spec_values,
    parse_int,
)


def _base_item(**overrides) -> dict:
    """PK 가 있는 최소 item. 개별 테스트에서 필드를 덮어쓴다."""
    item = {"bfSpecRgstNo": "347539"}
    item.update(overrides)
    return item


# --- 명세 §응답 예제 기반 종합 변환 -------------------------------------
def test_spec_example_item_full():
    """명세 응답 예제 item → 주요 컬럼이 정확히 변환되는지 종합 검증."""
    item = {
        "bsnsDivNm": "용역",
        "refNo": "운영지원과",
        "prdctClsfcNoNm": "경기수원항공산업전 농촌진흥청 전시홍보관 설치 및 운영",
        "orderInsttNm": "농촌진흥청",
        "rlDminsttNm": "농촌진흥청",
        "asignBdgtAmt": "44000000",
        "rcptDt": "2016-04-01 09:25:03",
        "opninRgstClseDt": "2016-04-06 23:59:00",
        "ofclTelNo": "063-238-0302",
        "ofclNm": "이찬희",
        "swBizObjYn": "N",
        "dlvrTmlmtDt": "2016-05-10 00:00:00",
        "dlvrDaynum": "0",
        "bfSpecRgstNo": "347539",
        "rgstDt": "2016-04-01 09:25:03",
        "chgDt": "",
        "bidNtceNoList": "20160400091,20160413548",
    }
    values = item_to_pre_spec_values(item)

    assert values["bf_spec_rgst_no"] == "347539"
    assert values["bsns_div_nm"] == "용역"
    assert values["ref_no"] == "운영지원과"
    assert values["prdct_clsfc_no_nm"] == "경기수원항공산업전 농촌진흥청 전시홍보관 설치 및 운영"
    assert values["order_instt_nm"] == "농촌진흥청"
    assert values["rl_dminstt_nm"] == "농촌진흥청"
    assert values["asign_bdgt_amt"] == Decimal("44000000")
    assert values["rcpt_dt"] == datetime(2016, 4, 1, 9, 25, 3)
    assert values["opnin_rgst_clse_dt"] == datetime(2016, 4, 6, 23, 59, 0)
    assert values["ofcl_tel_no"] == "063-238-0302"
    assert values["ofcl_nm"] == "이찬희"
    assert values["sw_biz_obj_yn"] == "N"
    assert values["dlvr_tmlmt_dt"] == datetime(2016, 5, 10, 0, 0, 0)
    assert values["dlvr_daynum"] == 0
    assert values["rgst_dt"] == datetime(2016, 4, 1, 9, 25, 3)
    assert values["chg_dt"] is None  # 빈 문자열 → None
    assert values["bid_ntce_no_list"] == "20160400091,20160413548"


def test_sw_biz_obj_yn_y():
    """SW사업대상여부 Y 보존."""
    item = _base_item(swBizObjYn="Y")
    values = item_to_pre_spec_values(item)
    assert values["sw_biz_obj_yn"] == "Y"


# --- 날짜 2형식 ---------------------------------------------------------
def test_datetime_with_seconds():
    """초 있는 날짜 → datetime (rcpt_dt·opnin_rgst_clse_dt·dlvr_tmlmt_dt·rgst_dt·chg_dt)."""
    item = _base_item(
        rcptDt="2016-05-01 08:59:00",
        opninRgstClseDt="2016-05-06 23:59:00",
        dlvrTmlmtDt="2016-12-30 00:00:00",
        rgstDt="2016-05-01 08:59:00",
        chgDt="2016-05-02 10:30:15",
    )
    values = item_to_pre_spec_values(item)
    assert values["rcpt_dt"] == datetime(2016, 5, 1, 8, 59, 0)
    assert values["opnin_rgst_clse_dt"] == datetime(2016, 5, 6, 23, 59, 0)
    assert values["dlvr_tmlmt_dt"] == datetime(2016, 12, 30, 0, 0, 0)
    assert values["rgst_dt"] == datetime(2016, 5, 1, 8, 59, 0)
    assert values["chg_dt"] == datetime(2016, 5, 2, 10, 30, 15)


def test_datetime_without_seconds():
    """초 없는 날짜 → datetime."""
    item = _base_item(rcptDt="2016-05-01 08:59", opninRgstClseDt="2016-05-06 23:59")
    values = item_to_pre_spec_values(item)
    assert values["rcpt_dt"] == datetime(2016, 5, 1, 8, 59, 0)
    assert values["opnin_rgst_clse_dt"] == datetime(2016, 5, 6, 23, 59, 0)


def test_datetime_empty_blank_and_malformed_to_none():
    """빈값·공백·형식불일치 날짜 → None (예외 없이)."""
    item = _base_item(
        rcptDt="",
        opninRgstClseDt="   ",
        dlvrTmlmtDt="2016/05/10",  # 형식 불일치
        rgstDt=None,
    )
    values = item_to_pre_spec_values(item)
    assert values["rcpt_dt"] is None
    assert values["opnin_rgst_clse_dt"] is None
    assert values["dlvr_tmlmt_dt"] is None
    assert values["rgst_dt"] is None
    # chgDt 키 자체가 없어도 None
    assert values["chg_dt"] is None


# --- 금액 ---------------------------------------------------------------
def test_decimal_plain_comma_and_empty():
    """배정예산: 일반/콤마/빈값."""
    item = _base_item(asignBdgtAmt="143000000")
    assert item_to_pre_spec_values(item)["asign_bdgt_amt"] == Decimal("143000000")

    item = _base_item(asignBdgtAmt="143,000,000")
    assert item_to_pre_spec_values(item)["asign_bdgt_amt"] == Decimal("143000000")

    item = _base_item(asignBdgtAmt="")
    assert item_to_pre_spec_values(item)["asign_bdgt_amt"] is None

    item = _base_item()  # 키 없음
    assert item_to_pre_spec_values(item)["asign_bdgt_amt"] is None


# --- 정수(dlvrDaynum) ----------------------------------------------------
def test_parse_int_unit():
    """parse_int: 숫자/0/콤마 → int, 빈값/비숫자 → None."""
    assert parse_int("90") == 90
    assert parse_int("0") == 0
    assert parse_int("1,000") == 1000
    assert parse_int("  365  ") == 365
    assert parse_int("") is None
    assert parse_int("   ") is None
    assert parse_int(None) is None
    assert parse_int("abc") is None
    assert parse_int("9.5") is None  # 소수점은 int 변환 실패 → None


def test_dlvr_daynum_integer_column():
    """dlvr_daynum(Integer): "90"/"0"/콤마 → int, 빈값/비숫자 → None."""
    assert item_to_pre_spec_values(_base_item(dlvrDaynum="90"))["dlvr_daynum"] == 90
    assert item_to_pre_spec_values(_base_item(dlvrDaynum="0"))["dlvr_daynum"] == 0
    assert item_to_pre_spec_values(_base_item(dlvrDaynum="1,000"))["dlvr_daynum"] == 1000
    assert item_to_pre_spec_values(_base_item(dlvrDaynum=""))["dlvr_daynum"] is None
    assert item_to_pre_spec_values(_base_item(dlvrDaynum="N/A"))["dlvr_daynum"] is None
    assert item_to_pre_spec_values(_base_item())["dlvr_daynum"] is None


# --- 빈값 → None (여러 문자열 컬럼) -------------------------------------
def test_blank_string_fields_to_none():
    item = _base_item(
        bsnsDivNm="   ",
        refNo="",
        orderInsttNm="  농촌진흥청  ",  # 트림 검증
        ofclNm="",
    )
    values = item_to_pre_spec_values(item)
    assert values["bsns_div_nm"] is None
    assert values["ref_no"] is None
    assert values["order_instt_nm"] == "농촌진흥청"
    assert values["ofcl_nm"] is None


def test_missing_keys_become_none():
    """PK 외 키가 없어도 KeyError 없이 None."""
    values = item_to_pre_spec_values(_base_item())
    assert values["bsns_div_nm"] is None
    assert values["rcpt_dt"] is None
    assert values["asign_bdgt_amt"] is None
    assert values["dlvr_daynum"] is None
    assert values["spec_doc_file_url1"] is None
    assert values["spec_doc_file_url5"] is None
    assert values["prdct_dtl_list"] is None
    assert values["bid_ntce_no_list"] is None


# --- 첨부 규격서 1~5 매핑 -----------------------------------------------
def test_spec_doc_file_url_mapping():
    item = _base_item(
        specDocFileUrl1="https://example/spec1",
        specDocFileUrl5="https://example/spec5",
    )
    values = item_to_pre_spec_values(item)
    assert values["spec_doc_file_url1"] == "https://example/spec1"
    assert values["spec_doc_file_url5"] == "https://example/spec5"


# --- 리스트성 컬럼 (문자열 보존 / list JSON 직렬화) ---------------------
def test_list_columns_string_kept():
    """prdctDtlList·bidNtceNoList 가 문자열이면 그대로 보존."""
    item = _base_item(
        prdctDtlList="[1^4321150102^컴퓨터서버],[2^4321150901^태블릿컴퓨터]",
        bidNtceNoList="20160530525,20160505996",
    )
    values = item_to_pre_spec_values(item)
    assert values["prdct_dtl_list"] == "[1^4321150102^컴퓨터서버],[2^4321150901^태블릿컴퓨터]"
    assert values["bid_ntce_no_list"] == "20160530525,20160505996"


def test_list_columns_list_serialized():
    """list 로 오면 JSON 직렬화(한글 보존)."""
    prdct = [
        {"seq": "1", "code": "4321150102", "name": "컴퓨터서버"},
        {"seq": "2", "code": "4321150901", "name": "태블릿컴퓨터"},
    ]
    bid_list = ["20160530525", "20160505996"]
    item = _base_item(prdctDtlList=prdct, bidNtceNoList=bid_list)
    values = item_to_pre_spec_values(item)
    assert isinstance(values["prdct_dtl_list"], str)
    assert json.loads(values["prdct_dtl_list"]) == prdct
    assert "컴퓨터서버" in values["prdct_dtl_list"]  # ensure_ascii=False
    assert json.loads(values["bid_ntce_no_list"]) == bid_list


def test_list_columns_blank_to_none():
    item = _base_item(prdctDtlList="   ", bidNtceNoList="")
    values = item_to_pre_spec_values(item)
    assert values["prdct_dtl_list"] is None
    assert values["bid_ntce_no_list"] is None


# --- raw_json 보존·복원 -------------------------------------------------
def test_raw_json_preserves_and_roundtrips():
    item = _base_item(
        prdctClsfcNoNm="소프트웨어 개발 사전규격",
        asignBdgtAmt="44000000",
        bidNtceNoList="20160400091,20160413548",
    )
    values = item_to_pre_spec_values(item)
    restored = json.loads(values["raw_json"])
    assert restored == item
    # 한글이 이스케이프되지 않고 보존되는지(ensure_ascii=False)
    assert "소프트웨어 개발 사전규격" in values["raw_json"]


# --- 수집 메타데이터는 반환 dict에 없어야 함 ----------------------------
def test_meta_columns_excluded():
    values = item_to_pre_spec_values(_base_item())
    for meta in PRE_SPEC_META_COLUMNS:
        assert meta not in values
    assert "collected_at" not in values
    assert "updated_at" not in values


# --- PK 누락 → ValueError -----------------------------------------------
def test_missing_pk_raises():
    with pytest.raises(ValueError):
        item_to_pre_spec_values({"bsnsDivNm": "용역"})  # PK 없음

    # 공백만 있어도 PK 누락으로 간주
    with pytest.raises(ValueError):
        item_to_pre_spec_values({"bfSpecRgstNo": "   "})


def test_non_dict_raises():
    with pytest.raises(ValueError):
        item_to_pre_spec_values(["not", "a", "dict"])  # type: ignore[arg-type]


# --- 매핑 무결성: 명세 23개 필드, 메타/raw_json 제외한 컬럼 전부 매핑 -----
def test_mapping_covers_all_non_meta_columns():
    from app.models import PreSpec
    from app.transform import RAW_JSON_COLUMN

    assert len(PRE_SPEC_COLUMN_TO_API) == 23
    skip = set(PRE_SPEC_META_COLUMNS) | {RAW_JSON_COLUMN}
    for column in PreSpec.__table__.columns:
        if column.name in skip:
            continue
        assert column.name in PRE_SPEC_COLUMN_TO_API


# --- 회귀: 13자 영숫자 실데이터 PK도 정상 변환 ---------------------------
def test_real_alphanumeric_pk():
    """5.0 실호출에서 확인된 13자 영숫자 PK(R26BD00222439) 정상 처리."""
    item = _base_item(bfSpecRgstNo="R26BD00222439")
    values = item_to_pre_spec_values(item)
    assert values["bf_spec_rgst_no"] == "R26BD00222439"
