"""수집기 — 전체 용역 검색·면허 일괄·누락 보정·분류(Phase 13.3).

API 조회 중에는 공고·면허를 쓰지 않고, 단일 세션에서 upsert 후 분류와 함께 커밋한다.
윈도우 계산·게이트·수동 백필 진입점은 기존 정책을 유지한다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app import api_client, classifier, repository
from app.db import SessionLocal, init_db
from app.models import BidLicenseLimit, BidNotice, CollectionRun
from app.transform import item_to_bid_notice_values, parse_datetime

logger = logging.getLogger(__name__)

OPERATION = "getBidPblancListInfoServcPPSSrch"
LICENSE_OPERATION = "getBidPblancListInfoLicenseLimit"

_OK_CODES: frozenset[str] = frozenset({"00", "03"})
_RETRY_CODES: frozenset[str] = frozenset({"01", "02", "04", "05"})
_RETRY_BACKOFF_SECONDS = 2.0
_MAX_PAGES_SAFETY = 1000
_BULK_PAGE_SIZE = 999  # API 상한 999 — 1000 이상은 오류 없이 기본 10행 응답(2026-10-07 실측)


def classify_result_code(code: str | None) -> str:
    """resultCode → 'ok' | 'retry' | 'halt'.

    - 00/03 → ok(성공, 03은 No Data)
    - 01/02/04/05 → retry(일시 장애)
    - None(HTTP/파싱 실패) → retry 에 준함
    - 그 외(06/07/08/10/11/12/20/22/30/31/32 등) → halt(사람 확인 필요)
    """
    if code in _OK_CODES:
        return "ok"
    if code is None or code in _RETRY_CODES:
        return "retry"
    return "halt"


def total_pages(total_count: Any, num_of_rows: int) -> int:
    """전체 건수/페이지크기로 전체 페이지 수 계산. 0건·비정상 입력 → 0."""
    try:
        tc = int(total_count)
    except (TypeError, ValueError):
        tc = 0
    if tc <= 0 or num_of_rows <= 0:
        return 0
    return (tc + num_of_rows - 1) // num_of_rows


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _count_unique_items(operation: str, items: list[dict]) -> int:
    """응답 키로 중복을 세되, 키 필드가 없거나 비면 행마다 1건으로 센다."""
    fields = ("bidNtceNo", "bidNtceOrd")
    if operation == LICENSE_OPERATION:
        fields += ("lmtGrpNo", "lmtSno")
    keys = set()
    unkeyed = 0
    for item in items:
        parts = tuple(_text(item.get(field)) for field in fields) if isinstance(item, dict) else ()
        if not parts or not all(parts):
            # 변환에서 버리는 행도 수신 건수에는 포함하고, 저장 변환 규칙은 유지한다.
            unkeyed += 1
            continue
        keys.add((parts[0], classifier.normalize_order(parts[1]), *parts[2:]))
    return len(keys) + unkeyed


def dedup_notices(items: list[dict]) -> list[dict]:
    """같은 공고는 가장 높은 차수를 유지한다(정수 우선, 불가하면 3자리 문자열)."""
    deduped: dict[str, dict] = {}
    for item in items:
        no = _text(item.get("bidNtceNo")) if isinstance(item, dict) else ""
        if not no:
            continue
        previous = deduped.get(no)
        if previous is not None:
            new_ord = classifier.normalize_order(_text(item.get("bidNtceOrd")))
            old_ord = classifier.normalize_order(_text(previous.get("bidNtceOrd")))
            try:
                higher = new_ord > old_ord
            except TypeError:
                higher = str(new_ord).zfill(3) > str(old_ord).zfill(3)
            if not higher:
                continue
        deduped[no] = item
    return list(deduped.values())


def license_values(item: dict) -> dict[str, Any] | None:
    """면허 응답의 명/코드·허용업종 목록을 저장 컬럼으로 변환한다."""
    no, ord_ = _text(item.get("bidNtceNo")), _text(item.get("bidNtceOrd"))
    if not no or not ord_:
        return None
    group, sequence = _text(item.get("lmtGrpNo")), _text(item.get("lmtSno"))
    name = _text(item.get("lcnsLmtNm"))
    allowed_list = _text(item.get("permsnIndstrytyList"))
    if not group or not sequence:
        # 선택 키가 비어도 서로 다른 면허는 보존하고, 같은 내용은 재수신 시 갱신한다.
        content = "|".join((group, sequence, name, allowed_list))
        sequence = "h" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:9]
    if "/" in name:
        name, code = (part.strip() for part in name.rsplit("/", 1))
    else:
        code = None
    codes = []
    for entry in re.findall(r"\[([^\[\]]*)\]", allowed_list):
        if "/" in entry:
            allowed_code = entry.rsplit("/", 1)[1].strip()
            if allowed_code and allowed_code not in codes:
                codes.append(allowed_code)
    return {
        "bid_ntce_no": no, "bid_ntce_ord": ord_,
        "lmt_grp_no": group, "lmt_sno": sequence,
        "lcns_cd": code or None, "lcns_nm": name or None,
        "permsn_cds": ",".join(codes) or None, "rgst_dt": parse_datetime(item.get("rgstDt")),
    }


def _license_key(row: dict) -> tuple[str, str, str, str]:
    return tuple(row[name] for name in ("bid_ntce_no", "bid_ntce_ord", "lmt_grp_no", "lmt_sno"))


def _keep_licenses(session, items: list[dict], notice_nos: set[str]) -> dict[tuple, dict]:
    """이번 창 공고와 DB 기존 공고의 면허만 보존한다(후보 500개 이하 IN 조회)."""
    rows = [row for item in items if (row := license_values(item)) is not None]
    candidates = sorted({row["bid_ntce_no"] for row in rows} - notice_nos)
    keep = set(notice_nos)
    for start in range(0, len(candidates), 500):
        keep.update(session.scalars(select(BidNotice.bid_ntce_no).where(
            BidNotice.bid_ntce_no.in_(candidates[start:start + 500])
        )))
    return {_license_key(row): row for row in rows if row["bid_ntce_no"] in keep}


def _missing_license_targets(session, notices: list[dict], licenses: dict[tuple, dict]) -> list[dict]:
    """업종 제한 Y 공고의 현재 차수 면허가 일괄 응답·DB 모두에 없으면 보정한다."""
    candidates = [item for item in notices if _text(item.get("indstrytyLmtYn")) == "Y"
                  and _text(item.get("bidNtceOrd"))]
    pairs = {(row["bid_ntce_no"], classifier.normalize_order(row["bid_ntce_ord"]))
             for row in licenses.values()}
    nos = [_text(item["bidNtceNo"]) for item in candidates]
    for start in range(0, len(nos), 500):
        pairs.update((no, classifier.normalize_order(ord_)) for no, ord_ in session.execute(
            select(BidLicenseLimit.bid_ntce_no, BidLicenseLimit.bid_ntce_ord).where(
                BidLicenseLimit.bid_ntce_no.in_(nos[start:start + 500]),
                BidLicenseLimit.superseded_at.is_(None),
            ).distinct()
        ))
    return [item for item in candidates
            if (_text(item["bidNtceNo"]),
                classifier.normalize_order(_text(item["bidNtceOrd"]))) not in pairs]


def _verify_license_targets(session, licenses: dict[tuple, dict]) -> list[dict]:
    """새 PK 와 미수신 기존 유효 행이 같은 공고·차수에 있으면 전체 조회로 확인한다."""
    by_pair: dict[tuple, dict[tuple, dict]] = {}
    for pk, row in licenses.items():
        pair = (row["bid_ntce_no"], classifier.normalize_order(row["bid_ntce_ord"]))
        by_pair.setdefault(pair, {})[pk] = row
    existing: set[tuple] = set()
    active: dict[tuple, set[tuple]] = {}
    nos = sorted({no for no, _ in by_pair})
    for start in range(0, len(nos), 500):
        for no, ord_, group, sequence, superseded_at in session.execute(select(
            BidLicenseLimit.bid_ntce_no, BidLicenseLimit.bid_ntce_ord,
            BidLicenseLimit.lmt_grp_no, BidLicenseLimit.lmt_sno, BidLicenseLimit.superseded_at,
        ).where(BidLicenseLimit.bid_ntce_no.in_(nos[start:start + 500]))):
            pk = (no, ord_, group, sequence)
            existing.add(pk)
            if superseded_at is None:
                active.setdefault((no, classifier.normalize_order(ord_)), set()).add(pk)
    targets = []
    for pair, rows in by_pair.items():
        if rows.keys() - existing and active.get(pair, set()) - rows.keys():
            row = next(iter(rows.values()))
            targets.append({"bidNtceNo": row["bid_ntce_no"], "bidNtceOrd": row["bid_ntce_ord"]})
    return targets


def fmt_dt(dt: datetime) -> str:
    """datetime → 'YYYYMMDDHHMM'(12자리, 초 없음). API 조회일시 형식."""
    return dt.strftime("%Y%m%d%H%M")


@dataclass(frozen=True)
class _ConfigSnapshot:
    """한 실행에서 공통으로 사용할 조회 설정."""

    inqry_div: str
    intrntnl_div_cd: str | None
    prtcpt_lmt_rgn_cd: str | None
    num_of_rows: int
    max_retries: int


def _servc_params(window_bgn: datetime, window_end: datetime, config: _ConfigSnapshot) -> dict[str, str]:
    return {
        "inqryDiv": config.inqry_div,
        "intrntnlDivCd": config.intrntnl_div_cd or "",
        "prtcptLmtRgnCd": config.prtcpt_lmt_rgn_cd or "",
        "inqryBgnDt": fmt_dt(window_bgn), "inqryEndDt": fmt_dt(window_end),
    }


def _call_with_retry(
    operation: str, raw_params: dict[str, str], max_retries: int
) -> tuple[api_client.ApiResult, int, str]:
    """페이지 1건 호출 + 재시도. 반환: (마지막 ApiResult, 사용한 재시도 수, outcome).

    outcome ∈ {"ok","failed","halt"}. failed = 재시도 소진.
    """
    used_retries = 0
    while True:
        result = api_client.call_endpoint(operation, raw_params, response_type="json")
        cls = classify_result_code(result.result_code)
        if cls == "ok":
            return result, used_retries, "ok"
        if cls == "halt":
            return result, used_retries, "halt"
        # retry
        if used_retries >= max_retries:
            return result, used_retries, "failed"
        used_retries += 1
        time.sleep(_RETRY_BACKOFF_SECONDS * used_retries)  # 선형 백오프


def _fetch_pages(operation: str, base_params: dict[str, str], config: _ConfigSnapshot) -> dict[str, Any]:
    """operation 을 끝까지 페이징한다. 조회 결과만 모으고 DB 는 쓰지 않는다."""
    page_size = (_BULK_PAGE_SIZE
                 if operation == OPERATION or (
                     operation == LICENSE_OPERATION and base_params.get("inqryDiv") == "1")
                 else min(config.num_of_rows, _BULK_PAGE_SIZE))
    base_params = {**base_params, "numOfRows": str(page_size)}
    items: list[dict] = []
    retry_count = 0
    pages_fetched = 0
    last_code: str | None = None
    total_count: int | None = None
    max_page: int | None = None
    page = 1

    try:
        while True:
            params = {**base_params, "pageNo": str(page)}
            result, used_retries, outcome = _call_with_retry(operation, params, config.max_retries)
            retry_count += used_retries
            last_code = result.result_code

            if outcome == "halt":
                halt_code = result.result_code
                return {
                    "items": items,
                    "unique_items": _count_unique_items(operation, items),
                    "pages": pages_fetched,
                    "last_code": last_code,
                    "retry_count": retry_count,
                    "total_count": total_count,
                    "outcome": "halt",
                    "halt_code": halt_code,
                    "error_msg": f"resultCode={halt_code} "
                    f"({api_client.ERROR_CODES.get(halt_code or '', '알 수 없는 코드')})",
                }
            if outcome == "failed":
                return {
                    "items": items,
                    "unique_items": _count_unique_items(operation, items),
                    "pages": pages_fetched,
                    "last_code": last_code,
                    "retry_count": retry_count,
                    "total_count": total_count,
                    "outcome": "failed",
                    "halt_code": None,
                    "error_msg": result.error
                    or f"일시 장애 재시도 소진(resultCode={last_code})",
                }

            # outcome == "ok"
            pages_fetched += 1
            if result.result_code == "03":  # No Data → 종료
                if total_count is None:
                    total_count = 0
                break
            items.extend(result.items)

            try:
                response_total_count = int(_text(result.total_count))
                if response_total_count < 0:
                    raise ValueError
            except ValueError:
                return {
                    "items": items, "pages": pages_fetched, "last_code": last_code,
                    "unique_items": _count_unique_items(operation, items),
                    "retry_count": retry_count, "total_count": total_count,
                    "outcome": "failed", "halt_code": None,
                    "error_msg": "totalCount 누락/오류",
                }
            if max_page is None:
                total_count = response_total_count
                max_page = total_pages(total_count, page_size)

            if page >= max_page:
                break
            if page >= _MAX_PAGES_SAFETY:
                logger.warning(
                    "operation %s: 안전 상한(%d페이지) 도달 — 페이징 중단(total_count=%s)",
                    operation,
                    _MAX_PAGES_SAFETY,
                    result.total_count,
                )
                return {
                    "items": items, "pages": pages_fetched, "last_code": last_code,
                    "unique_items": _count_unique_items(operation, items),
                    "retry_count": retry_count, "outcome": "failed", "halt_code": None,
                    "total_count": total_count,
                    "truncated": True,
                    "error_msg": f"안전 상한({_MAX_PAGES_SAFETY}페이지) 도달 — 잘린 수집",
                }
            page += 1
    except api_client.ApiClientError as exc:
        # 파라미터/키 등 클라이언트 측 오류 — 일시 장애로 보지 않고 실패 처리.
        return {
            "items": items,
            "unique_items": _count_unique_items(operation, items),
            "pages": pages_fetched,
            "last_code": last_code,
            "retry_count": retry_count,
            "total_count": total_count,
            "outcome": "failed",
            "halt_code": None,
            "error_msg": f"ApiClientError: {exc}",
        }

    unique_items = _count_unique_items(operation, items)
    shortfall = unique_items < total_count
    duplicates = len(items) - unique_items
    error_msg = f"건수 미달: 고유 {unique_items} / 전체 {total_count}" if shortfall else None
    if shortfall and duplicates:
        error_msg += f" (중복 {duplicates})"
    return {
        "items": items,
        "unique_items": unique_items,
        "pages": pages_fetched,
        "last_code": last_code,
        "retry_count": retry_count,
        "total_count": total_count,
        "outcome": "failed" if shortfall else "ok",
        "halt_code": None,
        "error_msg": error_msg,
    }


def _detach_run(session, run: CollectionRun) -> CollectionRun:
    """commit 으로 만료된 run 속성을 로드하고 세션에서 분리한다.

    collect_window 는 내부 세션을 finally 에서 닫으므로, 반환된 run 을 호출자가
    그대로 읽으면 DetachedInstanceError 가 난다. 닫기 전에 refresh 로 속성을 채우고
    expunge 로 분리해 두면 세션 종료 후에도 읽기 전용으로 안전하게 접근할 수 있다.
    """
    session.refresh(run)
    session.expunge(run)
    return run


def _build_detail_json(by_call: dict, ord_changes: list[dict], classify: dict | None = None) -> str:
    """단계별 조회 결과·분류 통계·차수 변경을 직렬화한다."""
    return json.dumps({
        "by_call": by_call, "classify": classify,
        "ord_changes": ord_changes, "ord_changed_count": len(ord_changes),
    }, ensure_ascii=False)


def _call_detail(result: dict, **counts) -> dict:
    return {"outcome": result["outcome"], "pages": result["pages"],
            "last_result_code": result["last_code"], "items": len(result["items"]),
            "unique_items": result["unique_items"],
            "total_count": result["total_count"], "error_msg": result["error_msg"],
            **({"truncated": True} if result.get("truncated") else {}),
            **counts, "retry_count": result["retry_count"]}


def collect_window(
    window_bgn: datetime, window_end: datetime, trigger: str = "manual",
) -> CollectionRun | None:
    """전체 용역·면허 일괄·누락 보정 후 저장·분류하고 최종 실행 이력을 반환한다."""
    session = SessionLocal()
    try:
        config = repository.get_config(session)
        if config.auto_halted:
            logger.warning("auto_halted=True → 수집 건너뜀 (halt_code=%s, reason=%s)",
                           config.halt_code, config.halt_reason)
            return None
        if not any(code.strip() for code in (config.indstryty_cds or "").split(",")):
            logger.warning("매칭 업종코드 없음 — 업종 근거 표시 없음. 수집은 계속합니다.")
        snapshot = _ConfigSnapshot(
            inqry_div=config.inqry_div, intrntnl_div_cd=config.intrntnl_div_cd,
            prtcpt_lmt_rgn_cd=config.prtcpt_lmt_rgn_cd,
            num_of_rows=config.num_of_rows, max_retries=config.max_retries,
        )
        meta_ts = datetime.now()
        run = repository.create_run(session, trigger, window_bgn, window_end)
        logger.info("수집 시작: trigger=%s window=%s~%s",
                    trigger, fmt_dt(window_bgn), fmt_dt(window_end))
        by_call = {
            "servc": {"outcome": "skipped", "pages": 0, "last_result_code": None,
                      "items": 0, "unique_items": 0, "notices": 0, "retry_count": 0,
                      "page_size": _BULK_PAGE_SIZE, "total_count": None},
            "license": {"outcome": "skipped", "pages": 0, "last_result_code": None,
                        "items": 0, "unique_items": 0, "kept": 0, "retry_count": 0,
                        "page_size": _BULK_PAGE_SIZE, "total_count": None},
            "license_fix": {"outcome": "skipped", "targets": 0, "called": 0,
                            "found": 0, "failed": [], "retry_count": 0, "total_count": None,
                            "unique_items": 0, "verify_targets": 0,
                            "superseded": 0, "restored": 0},
        }
        failed: list[tuple[str, dict]] = []

        def halt(stage: str, result: dict) -> CollectionRun:
            reason = f"{stage} 에서 비재시도 에러 {result['error_msg']} — 스케줄 자동 중단."
            logger.error("halt: %s", reason)
            repository.set_halt(session, result["halt_code"], reason)
            repository.finish_run(
                session, run, status="failed", total_fetched=0, total_new=0, total_updated=0,
                retry_count=sum(detail["retry_count"] for detail in by_call.values()),
                error_code=result["halt_code"], error_msg=reason,
                detail_json=_build_detail_json(by_call, []),
            )
            logger.info("수집 종료: status=failed 공고=%d 면허보존=%d 보정=%d 분류표시=0",
                        by_call["servc"]["notices"], by_call["license"]["kept"],
                        by_call["license_fix"]["found"])
            return _detach_run(session, run)

        # 조회 단계는 공고·면허에 쓰지 않는다. halt 면 이후 호출도 중단한다.
        servc = _fetch_pages(OPERATION, _servc_params(window_bgn, window_end, snapshot), snapshot)
        notices = dedup_notices(servc["items"])
        notice_nos = {_text(item["bidNtceNo"]) for item in notices}
        by_call["servc"] = _call_detail(servc, notices=len(notices), page_size=_BULK_PAGE_SIZE)
        if servc["outcome"] == "halt":
            return halt("servc", servc)
        if servc["outcome"] == "failed":
            failed.append(("servc", servc))

        license_result = _fetch_pages(LICENSE_OPERATION, {
            "inqryDiv": "1", "inqryBgnDt": fmt_dt(window_bgn), "inqryEndDt": fmt_dt(window_end),
        }, snapshot)
        licenses = _keep_licenses(session, license_result["items"], notice_nos)
        by_call["license"] = _call_detail(
            license_result, kept=len(licenses), page_size=_BULK_PAGE_SIZE,
        )
        if license_result["outcome"] == "halt":
            return halt("license", license_result)
        if license_result["outcome"] == "failed":
            failed.append(("license", license_result))

        fix = by_call["license_fix"]
        full_license_keys: list[tuple[str, str, set[tuple]]] = []
        if servc["outcome"] == license_result["outcome"] == "ok":
            verify_targets = _verify_license_targets(session, licenses)
            targets_by_pair = {
                (_text(item["bidNtceNo"]), classifier.normalize_order(_text(item["bidNtceOrd"]))): item
                for item in _missing_license_targets(session, notices, licenses) + verify_targets
            }
            targets = list(targets_by_pair.values())
            if len(targets) >= 50:
                logger.warning("면허 누락 보정 대상 %d건 — 이번 창 공고 %d건",
                               len(targets), len(notices))
            fix.update(outcome="ok", targets=len(targets), verify_targets=len(verify_targets),
                       total_count=0)
            for item in targets:
                no = _text(item["bidNtceNo"])
                result = _fetch_pages(LICENSE_OPERATION, {
                    "inqryDiv": "2", "bidNtceNo": no, "bidNtceOrd": _text(item["bidNtceOrd"]),
                }, snapshot)
                fix["called"] += 1
                fix["retry_count"] += result["retry_count"]
                fix["unique_items"] += result["unique_items"]
                # 보정 단계의 전체 건수는 공고별 첫 정상 응답 건수의 합(불명이 있으면 None).
                fix["total_count"] = (fix["total_count"] + result["total_count"]
                                      if fix["total_count"] is not None
                                      and result["total_count"] is not None else None)
                rows = [row for raw in result["items"] if (row := license_values(raw)) is not None]
                licenses.update({_license_key(row): row for row in rows})
                if rows:
                    fix["found"] += 1
                if result["outcome"] == "halt":
                    fix["outcome"] = "failed"
                    fix["error_msg"] = result["error_msg"]
                    fix["failed"].append(no)
                    return halt("license_fix", result)
                if result["outcome"] == "failed":
                    fix["outcome"] = "failed"
                    fix["error_msg"] = "; ".join(filter(None, [fix.get("error_msg"), result["error_msg"]]))
                    if result.get("truncated"):
                        fix["truncated"] = True
                    fix["failed"].append(no)
                    failed.append(("license_fix", result))
                elif rows:
                    # 성공한 비어 있지 않은 전체 조회만 사용하고, 다른 공고·차수는 제외한다.
                    ord_ = _text(item["bidNtceOrd"])
                    keep_pks = {_license_key(row) for row in rows
                                if row["bid_ntce_no"] == no
                                and classifier.normalize_order(row["bid_ntce_ord"])
                                == classifier.normalize_order(ord_)}
                    if keep_pks:
                        full_license_keys.append((no, ord_, keep_pks))

        # 공고·면허·분류는 finish_run 의 실행 종료 이력과 함께 커밋한다.
        values_list = []
        for item in notices:
            try:
                values = item_to_bid_notice_values(item)
            except ValueError as exc:
                logger.warning("transform 건너뜀(PK 누락 등): %s", exc)
                continue
            values.update(collected_at=meta_ts, updated_at=meta_ts)
            values_list.append(values)
        with classifier.CLASSIFY_WRITE_LOCK:
            new_count, updated_count, ord_changes = repository.upsert_bid_notices(session, values_list)
            repository.upsert_license_limits(session, list(licenses.values()), meta_ts, stats=fix)
            for no, ord_, keep_pks in full_license_keys:
                fix["superseded"] += repository.mark_superseded_licenses(
                    session, no, ord_, keep_pks, meta_ts,
                )
            classify = classifier.reclassify_notices(
                session, bid_ntce_nos=notice_nos | {row["bid_ntce_no"] for row in licenses.values()}
                | {no for no, _, _ in full_license_keys},
                commit=False,
            )
            for change in ord_changes:
                logger.info("차수변경 %s: %s -> %s", change["no"], change["old"], change["new"])
            status = "partial" if failed else "success"
            error_code = next((result["last_code"] for _, result in failed if result["last_code"]), None)
            error_msg = "; ".join(f"{stage}: {result['error_msg']}" for stage, result in failed) or None
            total_retry = sum(detail["retry_count"] for detail in by_call.values())
            repository.finish_run(
                session, run, status=status, total_fetched=len(notices),
                total_new=new_count, total_updated=updated_count, retry_count=total_retry,
                error_code=error_code, error_msg=error_msg,
                detail_json=_build_detail_json(by_call, ord_changes, classify),
            )
            if status == "success":
                repository.update_last_success_dt(session, window_end)
        logger.info("수집 종료: status=%s 공고=%d 면허보존=%d 면허저장=%d 보정=%d/%d 분류표시=%d "
                    "new=%d updated=%d retry=%d ord_changed=%d",
                    status, len(notices), by_call["license"]["kept"], len(licenses),
                    fix["found"], fix["called"], classify["visible"],
                    new_count, updated_count, total_retry, len(ord_changes))
        return _detach_run(session, run)
    finally:
        session.close()


# --- 수동 백필 진입점 ----------------------------------------------------
def _run_backfill() -> None:
    """최근 1개월 백필 1회 — 검증용. 주기 실행은 3.4."""
    init_db()  # 테이블·시드 보장
    with SessionLocal() as session:
        config = repository.get_config(session)
        backfill_days = config.backfill_days

    window_end = datetime.now()
    window_bgn = window_end - timedelta(days=backfill_days)

    run = collect_window(window_bgn, window_end, trigger="backfill")
    if run is None:
        print("수집이 실행되지 않았습니다(auto_halted 등 — 로그 확인).")
        return

    summary = (
        f"[backfill] status={run.status} "
        f"fetched={run.total_fetched} new={run.total_new} updated={run.total_updated} "
        f"retry={run.retry_count}"
    )
    if run.error_code:
        summary += f" error_code={run.error_code}"
    print(summary)


if __name__ == "__main__":
    from app.logging_config import setup_logging  # noqa: PLC0415
    setup_logging()
    _run_backfill()
