"""transform.item_to_bid_notice_values 단위테스트 — Phase 3.2.

순수 변환 검증(DB·네트워크 미접근). 지시문 3장의 대표 케이스를 모두 포함한다.
실행: `pytest tests/test_transform.py`
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest

from app.transform import (
    clean_str,
    item_to_bid_notice_values,
    parse_datetime,
    parse_decimal,
)


def _base_item(**overrides) -> dict:
    """PK 가 있는 최소 item. 개별 테스트에서 필드를 덮어쓴다."""
    item = {"bidNtceNo": "R25BK00933736"}
    item.update(overrides)
    return item


# --- 1·2·3. 날짜 변환 ---------------------------------------------------
def test_datetime_with_seconds():
    """초 있는 날짜 → 올바른 datetime."""
    item = _base_item(bidNtceDt="2025-07-01 09:28:14")
    values = item_to_bid_notice_values(item)
    assert values["bid_ntce_dt"] == datetime(2025, 7, 1, 9, 28, 14)


def test_datetime_without_seconds():
    """초 없는 날짜 → 올바른 datetime."""
    item = _base_item(bidQlfctRgstDt="2025-07-14 18:00")
    values = item_to_bid_notice_values(item)
    assert values["bid_qlfct_rgst_dt"] == datetime(2025, 7, 14, 18, 0, 0)


def test_datetime_empty_and_blank_to_none():
    """빈 날짜·공백 → None (예외 없이)."""
    assert parse_datetime("") is None
    assert parse_datetime("   ") is None
    assert parse_datetime(None) is None
    # 형식 불일치도 None
    assert parse_datetime("2025/07/01") is None

    item = _base_item(bidNtceDt="", opengDt="   ")
    values = item_to_bid_notice_values(item)
    assert values["bid_ntce_dt"] is None
    assert values["openg_dt"] is None


# --- 4·5. 금액·율 변환 --------------------------------------------------
def test_decimal_plain_comma_and_empty():
    """금액: 일반/콤마/빈값."""
    assert parse_decimal("46363636") == Decimal("46363636")
    assert parse_decimal("1,000") == Decimal("1000")
    assert parse_decimal("") is None
    assert parse_decimal("   ") is None
    assert parse_decimal(None) is None

    item = _base_item(presmptPrce="46363636", asignBdgtAmt="1,000", VAT="")
    values = item_to_bid_notice_values(item)
    assert values["presmpt_prce"] == Decimal("46363636")
    assert values["asign_bdgt_amt"] == Decimal("1000")
    assert values["vat"] is None


def test_decimal_rate():
    """율: "87.745" → Decimal("87.745")."""
    assert parse_decimal("87.745") == Decimal("87.745")
    item = _base_item(sucsfbidLwltRate="87.745")
    values = item_to_bid_notice_values(item)
    assert values["sucsfbid_lwlt_rate"] == Decimal("87.745")


def test_decimal_invalid_to_none():
    """숫자가 아니면 None (예외 없이)."""
    assert parse_decimal("abc") is None


# --- 6. 공백 문자열 → None ----------------------------------------------
def test_blank_string_field_to_none():
    assert clean_str("   ") is None
    assert clean_str("") is None
    assert clean_str(None) is None
    assert clean_str("  hello  ") == "hello"

    item = _base_item(bidNtceNm="   ", ntceInsttNm="  조달청  ")
    values = item_to_bid_notice_values(item)
    assert values["bid_ntce_nm"] is None
    assert values["ntce_instt_nm"] == "조달청"


# --- 7. VAT → vat 매핑 ---------------------------------------------------
def test_vat_mapping():
    item = _base_item(VAT="4636364")
    values = item_to_bid_notice_values(item)
    assert "vat" in values
    assert values["vat"] == Decimal("4636364")


def test_email_irregular_mapping():
    """ntceInsttOfclEmailAdrs → ntce_instt_ofcl_email 불규칙 매핑."""
    item = _base_item(ntceInsttOfclEmailAdrs="officer@example.go.kr")
    values = item_to_bid_notice_values(item)
    assert values["ntce_instt_ofcl_email"] == "officer@example.go.kr"


# --- 8. raw_json 보존·복원 ----------------------------------------------
def test_raw_json_preserves_and_roundtrips():
    item = _base_item(
        bidNtceNm="소프트웨어 개발 용역",
        presmptPrce="46363636",
        ntceInsttOfclEmailAdrs="a@b.go.kr",
    )
    values = item_to_bid_notice_values(item)
    restored = json.loads(values["raw_json"])
    assert restored == item
    # 한글이 이스케이프되지 않고 보존되는지(ensure_ascii=False)
    assert "소프트웨어 개발 용역" in values["raw_json"]


# --- 9. purchsObjPrdctList: list / 문자열 모두 Text ----------------------
def test_purchs_obj_list_serialized():
    payload = [
        {"input": "1", "code": "7611170100", "name": "건설현장청소용역"},
    ]
    item = _base_item(purchsObjPrdctList=payload)
    values = item_to_bid_notice_values(item)
    assert isinstance(values["purchs_obj_prdct_list"], str)
    assert json.loads(values["purchs_obj_prdct_list"]) == payload


def test_purchs_obj_string_kept():
    item = _base_item(purchsObjPrdctList="[1^7611170100^건설현장청소용역]")
    values = item_to_bid_notice_values(item)
    assert values["purchs_obj_prdct_list"] == "[1^7611170100^건설현장청소용역]"


def test_purchs_obj_blank_to_none():
    item = _base_item(purchsObjPrdctList="   ")
    values = item_to_bid_notice_values(item)
    assert values["purchs_obj_prdct_list"] is None


# --- 10. 수집 메타데이터는 반환 dict에 없어야 함 -------------------------
def test_meta_columns_excluded():
    item = _base_item()
    values = item_to_bid_notice_values(item)
    assert "matched_indstryty_cds" not in values
    assert "collected_at" not in values
    assert "updated_at" not in values


# --- 11. bid_ntce_no 누락 → ValueError ----------------------------------
def test_missing_pk_raises():
    with pytest.raises(ValueError):
        item_to_bid_notice_values({"bidNtceNm": "PK 없는 공고"})

    # 공백만 있어도 PK 누락으로 간주
    with pytest.raises(ValueError):
        item_to_bid_notice_values({"bidNtceNo": "   "})


# --- 보강: 없는 키는 None, KeyError 로 죽지 않음 --------------------------
def test_missing_keys_become_none():
    item = _base_item()  # PK 외 키 없음
    values = item_to_bid_notice_values(item)
    assert values["bid_ntce_nm"] is None
    assert values["openg_dt"] is None
    assert values["presmpt_prce"] is None
    # 첨부 1~10 도 None
    assert values["ntce_spec_doc_url1"] is None
    assert values["ntce_spec_file_nm10"] is None


def test_attachment_mapping():
    """첨부 1~10 컬럼이 ntceSpecDocUrl/FileNm 으로 매핑되는지."""
    item = _base_item(
        ntceSpecDocUrl1="https://example/doc1",
        ntceSpecFileNm1="과업지시서.hwp",
    )
    values = item_to_bid_notice_values(item)
    assert values["ntce_spec_doc_url1"] == "https://example/doc1"
    assert values["ntce_spec_file_nm1"] == "과업지시서.hwp"
