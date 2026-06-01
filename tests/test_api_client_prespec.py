"""Phase 5.0 — 사전규격 op15 api_client 단위테스트 (네트워크 없음, 결정적).

검증 범위:
- op15(getPublicPrcureThngInfoServcPPSSrch)가 ENDPOINTS 에 등록되고 사전규격 베이스 URL을 가진다.
- base_url_for: op15=사전규격 베이스, 기존 입찰 op=입찰 베이스.
- build_params(op15): 허용 파라미터(공통 − bidNtceNo ∪ PRESPEC_SEARCH_PARAMS)만 통과시키고
  입찰 전용 파라미터(indstrytyCd/prtcptLmtRgnCd/presmptPrce*/bidNtceNo)는 제거한다.
- 기존 입찰 op는 회귀 없음(여전히 입찰 베이스, indstrytyCd 등 통과).

실호출은 두지 않는다(별도 스크립트로 1회 수동 검증).
"""

from __future__ import annotations

import pytest

from app import api_client


PRESPEC_OP = "getPublicPrcureThngInfoServcPPSSrch"
BID_SEARCH_OP = "getBidPblancListInfoServcPPSSrch"  # 12 · 용역 검색조건(입찰)


# --- op 등록 / 베이스 URL ------------------------------------------------
def test_op15_registered_in_endpoints():
    spec = api_client.ENDPOINTS_BY_OP.get(PRESPEC_OP)
    assert spec is not None, "op15 가 ENDPOINTS 에 등록되어야 한다"
    assert spec.no == 15
    assert spec.kind == "prespec"
    # 사전규격 전용 검색 파라미터 세트를 가진다.
    assert spec.extra_params is api_client.PRESPEC_SEARCH_PARAMS


def test_base_url_for_prespec_is_prespec_base():
    assert api_client.base_url_for(PRESPEC_OP) == api_client.PRESPEC_BASE_URL
    # 사전규격 베이스 경로(/ao/HrcspSsstndrdInfoService)를 가리킨다.
    assert "/ao/HrcspSsstndrdInfoService" in api_client.base_url_for(PRESPEC_OP)


def test_base_url_for_bid_ops_use_bid_base():
    # 기존 입찰 엔드포인트는 base_url 메타가 없어 입찰 BASE_URL 로 폴백(회귀 없음).
    for op in (
        BID_SEARCH_OP,
        "getBidPblancListInfoThngPPSSrch",
        "getBidPblancListInfoServc",
    ):
        assert api_client.base_url_for(op) == api_client.BASE_URL

    # 두 서비스 베이스는 서로 다른 경로여야 한다(입찰 ad vs 사전규격 ao).
    assert api_client.base_url_for(BID_SEARCH_OP) != api_client.base_url_for(PRESPEC_OP)


def test_base_url_for_unknown_op_falls_back_to_bid_base():
    assert api_client.base_url_for("nonexistentOp") == api_client.BASE_URL


# --- build_params: op15 허용목록 ----------------------------------------
def test_build_params_op15_keeps_allowed_params():
    raw = {
        "inqryDiv": "1",
        "inqryBgnDt": "202605010000",
        "inqryEndDt": "202605312359",
        "pageNo": "1",
        "numOfRows": "10",
        "bfSpecRgstNo": "347539",
        "refNo": "운영지원과",
        "ntceInsttCd": "3180000",
        "ntceInsttNm": "영등포구",
        "dminsttCd": "3180000",
        "dminsttNm": "영등포구",
        "prdctClsfcNoNm": "용역",
        "swBizObjYn": "Y",
        "dtilPrdctClsfcNo": "4110350201",
    }
    built = api_client.build_params(PRESPEC_OP, raw, "json")
    for key in raw:
        assert key in built, f"허용 파라미터 {key} 는 통과해야 한다"
    assert built["inqryDiv"] == "1"
    assert built["bfSpecRgstNo"] == "347539"
    assert built["type"] == "json"


def test_build_params_op15_drops_bid_only_params():
    """입찰 전용 파라미터는 op15 허용목록에 없으므로 제거되어야 한다."""
    raw = {
        "inqryDiv": "1",
        "numOfRows": "10",
        # 아래는 op15 명세에 없는 입찰 전용 파라미터 → 제거 대상.
        "indstrytyCd": "1468",
        "prtcptLmtRgnCd": "00",
        "presmptPrceBgn": "1000000",
        "presmptPrceEnd": "9000000",
        "bidNtceNo": "20260500001",  # omit_common 으로 제거
    }
    built = api_client.build_params(PRESPEC_OP, raw, "json")
    for forbidden in (
        "indstrytyCd",
        "prtcptLmtRgnCd",
        "presmptPrceBgn",
        "presmptPrceEnd",
        "bidNtceNo",
    ):
        assert forbidden not in built, f"{forbidden} 는 op15 에서 제거되어야 한다"
    # 허용 파라미터는 그대로 남는다.
    assert built["inqryDiv"] == "1"
    assert built["numOfRows"] == "10"


def test_build_params_op15_validates_datetime():
    """잘못된 날짜 형식은 op15 에서도 검증 에러를 던진다(기존 로직 재사용)."""
    with pytest.raises(api_client.ApiClientError):
        api_client.build_params(
            PRESPEC_OP,
            {"inqryDiv": "1", "inqryBgnDt": "2026-05-01"},
            "json",
        )


def test_build_params_op15_drops_empty_values():
    raw = {"inqryDiv": "1", "refNo": "", "bfSpecRgstNo": "   ", "swBizObjYn": "N"}
    built = api_client.build_params(PRESPEC_OP, raw, "json")
    assert "refNo" not in built
    assert "bfSpecRgstNo" not in built
    assert built["swBizObjYn"] == "N"


# --- 회귀: 입찰 op build_params 동작 불변 -------------------------------
def test_build_params_bid_op_still_accepts_bid_params():
    """입찰 검색조건 op(12)는 indstrytyCd/prtcptLmtRgnCd 등을 여전히 통과시킨다(회귀 없음)."""
    raw = {
        "inqryDiv": "1",
        "indstrytyCd": "1468",
        "prtcptLmtRgnCd": "00",
        "presmptPrceBgn": "1000000",
        "bidNtceNo": "20260500001",
    }
    built = api_client.build_params(BID_SEARCH_OP, raw, "json")
    assert built["indstrytyCd"] == "1468"
    assert built["prtcptLmtRgnCd"] == "00"
    assert built["presmptPrceBgn"] == "1000000"
    assert built["bidNtceNo"] == "20260500001"
    # 사전규격 전용 파라미터는 입찰 op 허용목록에 없으므로 제거된다.
    built2 = api_client.build_params(
        BID_SEARCH_OP, {"inqryDiv": "1", "bfSpecRgstNo": "347539"}, "json"
    )
    assert "bfSpecRgstNo" not in built2
