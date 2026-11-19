"""토큰으로 보호하는 읽기 전용 공고 조회 API."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import defer

from app import repository
from app.db import SessionLocal
from app.models import BidLicenseLimit, BidNotice, PreSpec

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger(__name__)
OPEN_WITHOUT_CLOSE_DAYS = 30

BID_LIST_FIELDS = (
    "bid_ntce_no", "bid_ntce_ord", "unty_ntce_no", "bid_ntce_nm",
    "ntce_kind_nm", "re_ntce_yn", "chg_ntce_rsn", "srvce_div_nm", "info_biz_yn",
    "ntce_instt_cd", "ntce_instt_nm", "dminstt_cd", "dminstt_nm",
    "ntce_instt_ofcl_nm", "ntce_instt_ofcl_tel_no", "ntce_instt_ofcl_email",
    "bid_ntce_dt", "rgst_dt", "bid_qlfct_rgst_dt", "bid_begin_dt", "bid_clse_dt",
    "openg_dt", "chg_dt", "presmpt_prce", "asign_bdgt_amt", "vat",
    "bid_methd_nm", "cntrct_cncls_mthd_nm", "sucsfbid_mthd_nm", "indstryty_lmt_yn",
    "matched_indstryty_cds", "list_visible", "match_basis", "match_detail",
    "pub_prcrmnt_lrgclsfc_nm", "pub_prcrmnt_midclsfc_nm", "pub_prcrmnt_clsfc_no",
    "pub_prcrmnt_clsfc_nm", "bef_bid_bbanc_no", "bid_ntce_url", "bid_ntce_dtl_url",
    "collected_at", "updated_at",
)
BID_DETAIL_EXTRA_FIELDS = (
    "std_ntce_doc_url", "purchs_obj_prdct_list", "sucsfbid_lwlt_rate",
    "tech_ablt_evl_rt", "bid_prce_evl_rt",
)
PRE_SPEC_LIST_FIELDS = (
    "bf_spec_rgst_no", "ref_no", "bsns_div_nm", "prdct_clsfc_no_nm",
    "order_instt_nm", "rl_dminstt_nm", "ofcl_nm", "ofcl_tel_no", "asign_bdgt_amt",
    "rcpt_dt", "opnin_rgst_clse_dt", "dlvr_tmlmt_dt", "dlvr_daynum", "sw_biz_obj_yn",
    "bid_ntce_no_list", "rgst_dt", "chg_dt", "collected_at", "updated_at",
)
PRE_SPEC_DETAIL_EXTRA_FIELDS = ("prdct_dtl_list",)


def _now() -> datetime:
    return datetime.now()


def _response(body: dict, status: int = 200, *, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        body, status_code=status, headers={**(headers or {}), "Cache-Control": "no-store"},
    )


def _check_access(request: Request) -> JSONResponse | None:
    token = os.getenv("NOTICE_API_TOKEN", "").strip()
    if not token:
        return _response({"detail": "Not Found"}, 404)
    if len(token) < 16:
        logger.warning("NOTICE_API_TOKEN이 16자 미만이라 조회 API를 끕니다")
        return _response({"detail": "Not Found"}, 404)
    if any(not 0x21 <= ord(char) <= 0x7E for char in token):
        logger.warning("NOTICE_API_TOKEN에 ASCII 보이는 글자가 아닌 글자가 있어 조회 API를 끕니다")
        return _response({"detail": "Not Found"}, 404)
    scheme, separator, supplied = request.headers.get("Authorization", "").partition(" ")
    if (scheme.lower() != "bearer" or not separator
            or not hmac.compare_digest(supplied.encode("latin-1"), token.encode("ascii"))):
        return _response({"error": "unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"})
    return None


def _parse_params(request: Request, *, pre_spec: bool) -> dict:
    """알려진 매개변수만 검사하며 오류에 입력값을 담지 않는다."""
    special = "sw" if pre_spec else "visible"
    params = {}
    for name in ("limit", "cursor", "ntce_from", "ntce_to", "updated_since", "open", special):
        values = request.query_params.getlist(name)
        if len(values) > 1:
            raise ValueError(f"{name} 매개변수는 한 번만 지정할 수 있습니다.")
        if values:
            if not values[0]:
                raise ValueError(f"{name} 값은 비울 수 없습니다.")
            params[name] = values[0]

    limit = params.get("limit", "100")
    # 긴 숫자도 int 변환 전에 거부하여 예외에 입력값이 섞이지 않게 한다.
    digits = limit.lstrip("0") or "0"
    if not re.fullmatch(r"[0-9]+", limit) or len(digits) > 3 or not 1 <= int(digits) <= 500:
        raise ValueError("limit은 1~500 정수여야 합니다.")
    params["limit"] = int(digits)
    if "cursor" in params and len(params["cursor"]) > (30 if pre_spec else 40):
        raise ValueError(f"cursor는 1~{30 if pre_spec else 40}자여야 합니다.")
    for name in ("ntce_from", "ntce_to", "updated_since"):
        if name not in params:
            continue
        value = params[name]
        if not re.fullmatch(r"[0-9]{12}", value):
            raise ValueError(f"{name}은 YYYYMMDDHHMM 형식의 실제 시각이어야 합니다.")
        try:
            params[name] = datetime.strptime(value, "%Y%m%d%H%M")
        except ValueError:
            raise ValueError(f"{name}은 YYYYMMDDHHMM 형식의 실제 시각이어야 합니다.") from None
    if "ntce_from" in params and "ntce_to" in params and params["ntce_from"] > params["ntce_to"]:
        raise ValueError("ntce_from은 ntce_to보다 늦을 수 없습니다.")
    if params.get("open", "true") not in ("true", "false"):
        raise ValueError("open은 true 또는 false여야 합니다.")
    if special in params and params[special] != "all":
        raise ValueError(f"{special}은 all만 지정할 수 있습니다.")
    return params


def _iso(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def _csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _bid_numbers(value: str | None) -> list[str]:
    if not value or not value.strip():
        return []
    if value.strip().startswith("["):
        try:
            parts = json.loads(value)
            if not isinstance(parts, list):
                raise ValueError
        except ValueError:
            return [value]
        return [str(part).strip() for part in parts if str(part).strip()]
    return _csv(value)


def _value(name: str, value):
    if name == "bid_ntce_no_list":
        return _bid_numbers(value)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return _iso(value)
    if name in ("presmpt_prce", "asign_bdgt_amt", "vat", "dlvr_daynum"):
        return int(value)
    if name in ("sucsfbid_lwlt_rate", "tech_ablt_evl_rt", "bid_prce_evl_rt"):
        return float(value)
    if name == "list_visible":
        return bool(value)
    return value


def _license_number_key(value: str | None) -> tuple:
    value = (value or "").strip()
    return (0, int(value)) if value.isascii() and value.isdigit() else (1, value)


def _item(session, row, *, pre_spec: bool, detail: bool = False) -> dict:
    fields = PRE_SPEC_LIST_FIELDS if pre_spec else BID_LIST_FIELDS
    if detail:
        fields += PRE_SPEC_DETAIL_EXTRA_FIELDS if pre_spec else BID_DETAIL_EXTRA_FIELDS
    item = {name: _value(name, getattr(row, name)) for name in fields}
    files = (repository.get_pre_spec_files(session, row.bf_spec_rgst_no) if pre_spec
             else repository.get_notice_files(session, row.bid_ntce_no))
    item["files"] = [{"name": f["name"], "url": f["url"]} for f in files]
    if detail and not pre_spec:
        limits = session.scalars(select(BidLicenseLimit).where(
            BidLicenseLimit.bid_ntce_no == row.bid_ntce_no,
            BidLicenseLimit.superseded_at.is_(None),
        ).order_by(BidLicenseLimit.lmt_grp_no, BidLicenseLimit.lmt_sno)).all()
        limits.sort(key=lambda limit: (
            _license_number_key(limit.lmt_grp_no), _license_number_key(limit.lmt_sno),
        ))
        item["license_limits"] = [
            {**{name: _value(name, getattr(limit, name)) for name in (
                "lmt_grp_no", "lmt_sno", "lcns_cd", "lcns_nm",
            )}, "permsn_cds": _csv(limit.permsn_cds)}
            for limit in limits
        ]
    return item


def _list_response(request: Request, now: datetime, *, pre_spec: bool) -> JSONResponse:
    try:
        params = _parse_params(request, pre_spec=pre_spec)
    except ValueError as exc:
        return _response({"error": str(exc)}, 422)
    model = PreSpec if pre_spec else BidNotice
    number = model.bf_spec_rgst_no if pre_spec else model.bid_ntce_no
    notice_date = model.rcpt_dt if pre_spec else model.bid_ntce_dt
    close_date = model.opnin_rgst_clse_dt if pre_spec else model.bid_clse_dt
    stmt = select(model).options(defer(model.raw_json))
    if pre_spec and "sw" not in params:
        stmt = stmt.where(PreSpec.sw_biz_obj_yn == "Y")
    if not pre_spec and "visible" not in params:
        stmt = stmt.where(BidNotice.list_visible.is_(True))
    if params.get("open", "true") == "true":
        stmt = stmt.where(or_(
            close_date >= now,
            and_(close_date.is_(None), notice_date >= now - timedelta(days=OPEN_WITHOUT_CLOSE_DAYS)),
        ))
    if "cursor" in params:
        stmt = stmt.where(number < params["cursor"])
    if "ntce_from" in params:
        stmt = stmt.where(notice_date >= params["ntce_from"])
    if "ntce_to" in params:
        stmt = stmt.where(notice_date <= params["ntce_to"].replace(second=59, microsecond=999999))
    if "updated_since" in params:
        stmt = stmt.where(model.updated_at >= params["updated_since"])
    limit = params["limit"]
    with SessionLocal() as session:
        rows = session.scalars(stmt.order_by(number.desc()).limit(limit + 1)).all()
        page = rows[:limit]
        items = [_item(session, row, pre_spec=pre_spec) for row in page]
        cursor = getattr(page[-1], number.key) if len(rows) > limit else None
    return _response({
        "api_version": "1", "generated_at": _iso(now), "count": len(items),
        "next_cursor": cursor, "items": items,
    })


def _detail_response(number: str, now: datetime, *, pre_spec: bool) -> JSONResponse:
    with SessionLocal() as session:
        row = session.get(PreSpec if pre_spec else BidNotice, number)
        if row is None:
            return _response({"error": "not found"}, 404)
        item = _item(session, row, pre_spec=pre_spec, detail=True)
    return _response({"api_version": "1", "generated_at": _iso(now), "item": item})


@router.get("/bids")
def list_bids(request: Request):
    denied = _check_access(request)
    if denied is not None:
        return denied
    return _list_response(request, _now(), pre_spec=False)


@router.get("/bids/{bid_ntce_no}")
def get_bid(bid_ntce_no: str, request: Request):
    denied = _check_access(request)
    if denied is not None:
        return denied
    return _detail_response(bid_ntce_no, _now(), pre_spec=False)


@router.get("/pre-specs")
def list_pre_specs(request: Request):
    denied = _check_access(request)
    if denied is not None:
        return denied
    return _list_response(request, _now(), pre_spec=True)


@router.get("/pre-specs/{bf_spec_rgst_no}")
def get_pre_spec(bf_spec_rgst_no: str, request: Request):
    denied = _check_access(request)
    if denied is not None:
        return denied
    return _detail_response(bf_spec_rgst_no, _now(), pre_spec=True)
