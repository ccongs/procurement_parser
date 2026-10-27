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
from app.models import BidLicenseLimit, BidNotice, CollectionRun, LicenseVerification
from app.ordering import is_higher_order, stored_order
from app.transform import item_to_bid_notice_values, parse_datetime
from app.window_split import collect_in_chunks

logger = logging.getLogger(__name__)

OPERATION = "getBidPblancListInfoServcPPSSrch"
LICENSE_OPERATION = "getBidPblancListInfoLicenseLimit"

_OK_CODES: frozenset[str] = frozenset({"00", "03"})
_RETRY_CODES: frozenset[str] = frozenset({"01", "02", "04", "05"})
_RETRY_BACKOFF_SECONDS = 2.0
_MAX_PAGES_SAFETY = 1000
_BULK_PAGE_SIZE = 999  # API 상한 999 — 1000 이상은 오류 없이 기본 10행 응답(2026-10-07 실측)
_VERIFY_MAX_ATTEMPTS = 24
_VERIFY_MAX_AGE = timedelta(days=7)
_VERIFY_QUERY_LIMIT = 200


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


def _halt_message(result: api_client.ApiResult) -> str:
    code = result.result_code
    message = f"resultCode={code} ({api_client.ERROR_CODES.get(code or '', '알 수 없는 코드')})"
    return message + (f" — API: {result.result_msg}" if result.result_msg else "")


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
            higher = is_higher_order(_text(item.get("bidNtceOrd")),
                                     _text(previous.get("bidNtceOrd")))
            if not higher:
                continue
        deduped[no] = item
    return list(deduped.values())


def license_values(item: dict) -> dict[str, Any] | None:
    """면허 응답의 명/코드·허용업종 목록을 저장 컬럼으로 변환한다.

    그룹·순번이 비면 내용 해시(`h` + sha1 앞 9자리)를 순번 키로 쓴다.
    같은 내용은 같은 키, 내용이 바뀌면 새 키(이전 행은 대체 표시 규칙으로 처리).
    """
    if not isinstance(item, dict):
        return None
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
        "bid_ntce_no": no, "bid_ntce_ord": stored_order(ord_),
        "lmt_grp_no": group, "lmt_sno": sequence,
        "lcns_cd": code or None, "lcns_nm": name or None,
        "permsn_cds": ",".join(codes) or None, "rgst_dt": parse_datetime(item.get("rgstDt")),
    }


def _warn_unkeyed_licenses(stage: str, items: list[dict]) -> None:
    rows = [item for item in items if isinstance(item, dict)
            and (not _text(item.get("lmtGrpNo")) or not _text(item.get("lmtSno")))]
    if rows:
        examples = list(dict.fromkeys(_text(item.get("bidNtceNo")) or "공고번호 없음"
                                     for item in rows))[:5]
        logger.warning("면허 %s 빈 그룹·순번 행 %d건: %s", stage, len(rows), ", ".join(examples))


def _regressed_notices(session, notices: list[dict]) -> list[dict]:
    incoming = {_text(item["bidNtceNo"]): _text(item.get("bidNtceOrd")) for item in notices}
    result = []
    nos = list(incoming)
    for start in range(0, len(nos), 500):
        for no, ord_ in session.execute(select(BidNotice.bid_ntce_no, BidNotice.bid_ntce_ord).where(
                BidNotice.bid_ntce_no.in_(nos[start:start + 500]))):
            if ord_ and incoming[no] and is_higher_order(ord_, incoming[no]):
                result.append({"bid_ntce_no": no, "stored_ord": ord_, "incoming_ord": incoming[no]})
    return result


def _failure_summary(failures: list[tuple[str, str]]) -> str | None:
    """같은 사유를 묶어 건수·공고 예시만 남긴다. 공고별 상세는 별도 목록에 보존한다."""
    grouped: dict[str, list[str]] = {}
    for label, reason in failures:
        grouped.setdefault(reason, []).append(label)
    return "; ".join(f"{reason} ({len(labels)}건: {', '.join(labels[:5])})"
                     for reason, labels in grouped.items()) or None


def _pending_after(session, fix):
    session.flush()
    fix["verify_pending_after"] = len(session.scalars(select(LicenseVerification).where(
        LicenseVerification.pending_since.is_not(None), LicenseVerification.gave_up_at.is_(None),
    )).all())


def _license_key(row: dict) -> tuple[str, str, str, str]:
    return (row["bid_ntce_no"], stored_order(row["bid_ntce_ord"]),
            row["lmt_grp_no"], row["lmt_sno"])


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
    """새 PK+미수신 유효 행 또는 대체 행 재등장 시 확인 대기 등록 대상을 산출한다.

    정상 키 행의 소실도 전체 조회 대상이 될 때만 처리한다. 사라지기만 하면 감지 불가.
    """
    by_pair: dict[tuple, dict[tuple, dict]] = {}
    for pk, row in licenses.items():
        pair = (row["bid_ntce_no"], classifier.normalize_order(row["bid_ntce_ord"]))
        by_pair.setdefault(pair, {})[pk] = row
    existing: set[tuple] = set()
    superseded: set[tuple] = set()
    active: dict[tuple, set[tuple]] = {}
    nos = sorted({no for no, _ in by_pair})
    for start in range(0, len(nos), 500):
        for no, ord_, group, sequence, superseded_at in session.execute(select(
            BidLicenseLimit.bid_ntce_no, BidLicenseLimit.bid_ntce_ord,
            BidLicenseLimit.lmt_grp_no, BidLicenseLimit.lmt_sno, BidLicenseLimit.superseded_at,
        ).where(BidLicenseLimit.bid_ntce_no.in_(nos[start:start + 500]))):
            pk = (no, stored_order(ord_), group, sequence)
            existing.add(pk)
            if superseded_at is None:
                active.setdefault((no, classifier.normalize_order(ord_)), set()).add(pk)
            else:
                superseded.add(pk)
    targets = []
    for pair, rows in by_pair.items():
        if (rows.keys() & superseded
                or (rows.keys() - existing and active.get(pair, set()) - rows.keys())):
            row = next(iter(rows.values()))
            targets.append({"bidNtceNo": row["bid_ntce_no"], "bidNtceOrd": row["bid_ntce_ord"]})
    return targets


def _give_up_license_verification(state, at, fix):
    label = f"{state.bid_ntce_no}-{state.bid_ntce_ord}"
    state.gave_up_at = at
    state.pending_since = None
    fix["verify_gave_up_count"] += 1
    if len(fix["verify_gave_up"]) < 50:
        fix["verify_gave_up"].append(label)
    logger.warning("면허 확인 포기 %s: 시도 %d회, %s",
                   label, state.attempts, state.last_error or "대기 7일 경과")


def _pending_license_verifications(session, at, fix):
    """만료 대기는 메모리에서만 제외해 API 조회 중 쓰기 잠금을 잡지 않는다."""
    query = select(LicenseVerification).where(
        LicenseVerification.pending_since.is_not(None), LicenseVerification.gave_up_at.is_(None),
    ).execution_options(populate_existing=True)
    states = session.scalars(query).all()
    # 실행 시작 때 대기 수: 이번 실행에서 포기될 것 포함, 이번 실행 새 등록 제외.
    fix["verify_pending"] = len(states)

    return {(state.bid_ntce_no, state.bid_ntce_ord): state.pending_since
            for state in states if not _license_verification_expired(state, at)}


def _license_verification_expired(state, at):
    return (state.attempts >= _VERIFY_MAX_ATTEMPTS
            or at - state.pending_since >= _VERIFY_MAX_AGE)


def _expire_license_verifications(session, at, fix):
    """조회 종료 후 공용 쓰기 잠금 안에서 만료를 재확인하고 기존 종료 커밋에 맡긴다."""
    states = session.scalars(select(LicenseVerification).where(
        LicenseVerification.pending_since.is_not(None), LicenseVerification.gave_up_at.is_(None),
    ).execution_options(populate_existing=True)).all()
    expired = set()
    for state in states:
        if _license_verification_expired(state, at):
            _give_up_license_verification(state, at, fix)
            expired.add((state.bid_ntce_no, state.bid_ntce_ord))
    session.flush()
    return expired


def _apply_license_verifications(session, registrations, responses, at, fix, *,
                                 bulk_received_at=None, bulk_pairs=(), bulk_keys=(), shortfall_pairs=(),
                                 rejected_shortfalls=None, gave_up_pairs=()):
    """공용 쓰기 잠금 안에서 상태를 재조회해 적용할 전체 조회와 오래된 공고·차수를 반환한다.

    SQLite 다중 프로세스에서 중간 커밋이 있으면 쓰기 시 잠금/스냅샷 오류로 실패할 수
    있다(기존 동작 유지). PostgreSQL 이전 시 확인 상태 행의 행 잠금이 필요하다.
    """
    nos = sorted({item["bidNtceNo"] for item in registrations}
                 | {no for no, _, _, _, _ in responses} | {no for no, _ in bulk_pairs})
    states = {}
    for start in range(0, len(nos), 500):
        states.update({(row.bid_ntce_no, row.bid_ntce_ord): row for row in session.scalars(
            select(LicenseVerification).where(
                LicenseVerification.bid_ntce_no.in_(nos[start:start + 500])
            ).execution_options(populate_existing=True)
        )})

    def state_for(pair):
        if pair not in states:
            states[pair] = LicenseVerification(
                bid_ntce_no=pair[0], bid_ntce_ord=pair[1], attempts=0,
            )
            session.add(states[pair])
        return states[pair]

    for item in registrations:
        state = state_for((item["bidNtceNo"], stored_order(item["bidNtceOrd"])))
        # 더 새 전체 조회가 이미 적용됐다면 오래된 일괄 결과로 재등록하지 않는다.
        if state.verified_at is not None and state.verified_at >= at:
            continue
        if state.pending_since is None or state.gave_up_at is not None:
            if (state.bid_ntce_no, state.bid_ntce_ord) in gave_up_pairs:
                label = f"{state.bid_ntce_no}-{state.bid_ntce_ord}"
                fix["verify_reregistered_count"] += 1
                if len(fix["verify_reregistered"]) < 50:
                    fix["verify_reregistered"].append(label)
                logger.info("면허 확인 재등록 %s: 같은 실행에서 포기 뒤 다시 누락 감지", label)
            state.pending_since = at
            state.gave_up_at = None
            state.attempts = 0

    applicable = []
    # 이번 실행의 대조를 적용하기 전 상태와 일괄 응답 수신 시각을 비교한다.
    # 시스템 시계 단조 증가 전제: 뒤로 조정되면 그 공고·차수의 일괄 행이 한 번
    # 제외될 수 있으며, 창 겹침으로 대부분 다시 수신한다.
    stale_pairs = {pair for pair in bulk_pairs if pair in states
                   and states[pair].verified_at is not None
                   and states[pair].verified_at > bulk_received_at}
    active_pks = {}
    shortfall_nos = sorted({no for no, _ in shortfall_pairs})
    for start in range(0, len(shortfall_nos), 500):
        for no, ord_, group, sequence in session.execute(select(
            BidLicenseLimit.bid_ntce_no, BidLicenseLimit.bid_ntce_ord,
            BidLicenseLimit.lmt_grp_no, BidLicenseLimit.lmt_sno,
        ).where(BidLicenseLimit.bid_ntce_no.in_(shortfall_nos[start:start + 500]),
                BidLicenseLimit.superseded_at.is_(None))):
            active_pks.setdefault((no, stored_order(ord_)), set()).add(
                (no, stored_order(ord_), group, sequence))
    # 이번 커밋에서 저장할 일괄 행도 미달 응답으로 대체하지 않는다.
    for pk in bulk_keys:
        if pk[:2] in shortfall_pairs and pk[:2] not in stale_pairs:
            active_pks.setdefault(pk[:2], set()).add(pk)
    for no, ord_, received_at, keep_pks, error in responses:
        if not keep_pks and (no, stored_order(ord_)) not in states:
            continue  # 누락 보정만 실패한 경우 확인 대기로 새로 등록하지 않는다.
        state = state_for((no, stored_order(ord_)))
        label = f"{no}-{stored_order(ord_)}"
        if state.verified_at is not None and received_at <= state.verified_at:
            stale_pairs.add((no, stored_order(ord_)))
            if len(fix["stale_skipped"]) < 50:
                fix["stale_skipped"].append(label)
            continue
        if (keep_pks and (no, stored_order(ord_)) in shortfall_pairs
                and active_pks.get((no, stored_order(ord_)), set()) - keep_pks):
            keep_pks = None
            error = "전체 조회 변환 후 고유 PK 미달: 기존 유효 행 또는 일괄 행 누락"
            if rejected_shortfalls is not None:
                rejected_shortfalls[(no, stored_order(ord_))] = error
        if keep_pks:
            applicable.append((no, ord_, keep_pks))
            state.verified_at = received_at
            state.pending_since = None
            state.attempts = 0
            state.last_error = None
            state.last_attempt_at = received_at
            fix["verified_count"] += 1
            if len(fix["verified"]) < 50:
                fix["verified"].append(label)
        elif state.pending_since is not None and state.gave_up_at is None:
            state.attempts += 1
            state.last_attempt_at = received_at
            state.last_error = (error or "전체 조회 검증 실패")[:200]
            fix["verify_failed_count"] += 1
            if len(fix["verify_failed"]) < 50:
                fix["verify_failed"].append(label)
            if (state.attempts >= _VERIFY_MAX_ATTEMPTS
                    or received_at - state.pending_since >= _VERIFY_MAX_AGE):
                _give_up_license_verification(state, received_at, fix)
    session.flush()
    return applicable, stale_pairs


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
    paging_flags: dict[str, bool] = {}
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
                    **paging_flags,
                    "outcome": "halt",
                    "halt_code": halt_code,
                    "error_msg": _halt_message(result),
                }
            if outcome == "failed":
                return {
                    "items": items,
                    "unique_items": _count_unique_items(operation, items),
                    "pages": pages_fetched,
                    "last_code": last_code,
                    "retry_count": retry_count,
                    "total_count": total_count,
                    **paging_flags,
                    "outcome": "failed",
                    "halt_code": None,
                    "error_msg": result.error
                    or f"일시 장애 재시도 소진(resultCode={last_code})",
                }

            # outcome == "ok"
            pages_fetched += 1
            if result.result_code == "03":  # No Data → 종료
                try:
                    response_total_count = int(_text(result.total_count))
                    if (total_count is not None and response_total_count >= 0
                            and response_total_count != total_count):
                        paging_flags["total_count_changed"] = True
                except ValueError:
                    pass  # 03은 totalCount 없이도 정상 종료하는 기존 규칙을 유지한다.
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
                    **paging_flags,
                    "outcome": "failed", "halt_code": None,
                    "error_msg": "totalCount 누락/오류",
                }
            if max_page is None:
                total_count = response_total_count
                max_page = total_pages(total_count, page_size)
            elif response_total_count != total_count:
                paging_flags["total_count_changed"] = True

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
                    **paging_flags,
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
            **paging_flags,
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
        **paging_flags,
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


def _build_detail_json(by_call: dict, ord_changes: list[dict], classify: dict | None = None,
                       ord_regress_skipped: list[dict] | None = None) -> str:
    """단계별 조회 결과·분류 통계·차수 변경을 직렬화한다."""
    return json.dumps({
        "by_call": by_call, "classify": classify,
        "ord_changes": ord_changes, "ord_changed_count": len(ord_changes),
        "ord_regress_skipped": (ord_regress_skipped or [])[:50],
        "ord_regress_skipped_count": len(ord_regress_skipped or []),
    }, ensure_ascii=False)


def _call_detail(result: dict, **counts) -> dict:
    return {"outcome": result["outcome"], "pages": result["pages"],
            "last_result_code": result["last_code"], "items": len(result["items"]),
            "unique_items": result["unique_items"],
            "total_count": result["total_count"], "error_msg": result["error_msg"],
            **({"truncated": True} if result.get("truncated") else {}),
            **({"total_count_changed": True} if result.get("total_count_changed") else {}),
            **counts, "retry_count": result["retry_count"]}


def collect_window(
    window_bgn: datetime, window_end: datetime, trigger: str = "manual",
) -> CollectionRun | None:
    """전체 용역·면허 일괄·누락 보정 후 저장·분류하고 최종 실행 이력을 반환한다."""
    session = SessionLocal()
    run_id = None
    by_call = {}
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
        run_id = run.id
        logger.info("수집 시작: trigger=%s window=%s~%s",
                    trigger, fmt_dt(window_bgn), fmt_dt(window_end))
        by_call = {
            "servc": {"outcome": "skipped", "pages": 0, "last_result_code": None,
                      "items": 0, "unique_items": 0, "notices": 0, "retry_count": 0,
                      "page_size": _BULK_PAGE_SIZE, "total_count": None, "error_msg": None},
            "license": {"outcome": "skipped", "pages": 0, "last_result_code": None,
                        "items": 0, "unique_items": 0, "kept": 0, "retry_count": 0,
                        "page_size": _BULK_PAGE_SIZE, "total_count": None, "error_msg": None},
            "license_fix": {"outcome": "skipped", "targets": 0, "called": 0,
                            "found": 0, "failed": [], "retry_count": 0, "total_count": None,
                            "unique_items": 0, "verify_targets": 0, "error_msg": None,
                            "failure_details": [], "verify_pending_after": 0,
                            "verify_reregistered": [], "verify_reregistered_count": 0,
                            "verify_pending": 0, "verified": [],
                            "verify_deferred": 0, "stale_bulk_skipped": 0,
                            "verify_failed": [], "verify_gave_up": [], "stale_skipped": [],
                            "verified_count": 0, "verify_failed_count": 0, "verify_gave_up_count": 0,
                            "anomalies": [], "pk_shortfall_skipped": [],
                            "superseded": 0, "restored": 0,
                            "superseded_notices": [], "superseded_notices_count": 0},
        }
        failed: list[tuple[str, dict]] = []
        fix_failures: list[tuple[str, str]] = []
        fix_unkeyed_items = []
        fix_failure_codes = []
        regress_skipped = []
        fix = by_call["license_fix"]
        pending_since = _pending_license_verifications(session, meta_ts, fix)
        pending_targets = [
            {"bidNtceNo": no, "bidNtceOrd": ord_} for no, ord_ in pending_since
        ]

        def halt(stage: str, result: dict) -> CollectionRun:
            reason = f"{stage} 에서 비재시도 에러 {result['error_msg']} — 스케줄 자동 중단."
            logger.error("halt: %s", reason)
            with classifier.CLASSIFY_WRITE_LOCK:
                repository.set_halt(session, result["halt_code"], reason)
                _expire_license_verifications(session, meta_ts, fix)
                _pending_after(session, fix)
                repository.finish_run(
                    session, run, status="failed", total_fetched=0, total_new=0, total_updated=0,
                    retry_count=sum(detail["retry_count"] for detail in by_call.values()),
                    error_code=result["halt_code"], error_msg=reason,
                    detail_json=_build_detail_json(by_call, [], ord_regress_skipped=regress_skipped),
                )
            logger.info("수집 종료: status=failed 공고=%d 면허보존=%d 보정=%d 분류표시=0 "
                        "verify_targets=%d superseded=%d restored=%d "
                        "verify_pending=%d verified=%d verify_failed=%d verify_gave_up=%d "
                        "verify_pending_after=%d verify_reregistered=%d",
                        by_call["servc"]["notices"], by_call["license"]["kept"],
                        by_call["license_fix"]["found"], by_call["license_fix"]["verify_targets"],
                        by_call["license_fix"]["superseded"], by_call["license_fix"]["restored"],
                        by_call["license_fix"]["verify_pending"], by_call["license_fix"]["verified_count"],
                        by_call["license_fix"]["verify_failed_count"], by_call["license_fix"]["verify_gave_up_count"],
                        fix["verify_pending_after"], fix["verify_reregistered_count"])
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

        regress_skipped = _regressed_notices(session, notices)
        regress_nos = {row["bid_ntce_no"] for row in regress_skipped}
        active_notices = [item for item in notices if _text(item["bidNtceNo"]) not in regress_nos]
        pending_targets = [item for item in pending_targets if item["bidNtceNo"] not in regress_nos]
        license_result = _fetch_pages(LICENSE_OPERATION, {
            "inqryDiv": "1", "inqryBgnDt": fmt_dt(window_bgn), "inqryEndDt": fmt_dt(window_end),
        }, snapshot)
        bulk_received_at = datetime.now()
        _warn_unkeyed_licenses("일괄", license_result["items"])
        licenses = _keep_licenses(session, [item for item in license_result["items"]
                                  if isinstance(item, dict)
                                  and _text(item.get("bidNtceNo")) not in regress_nos], notice_nos)
        bulk_keys = set(licenses)
        bulk_pairs = {pk[:2] for pk in bulk_keys}
        by_call["license"] = _call_detail(
            license_result, kept=len(licenses), page_size=_BULK_PAGE_SIZE,
        )
        if license_result["outcome"] == "halt":
            return halt("license", license_result)
        if license_result["outcome"] == "failed":
            failed.append(("license", license_result))

        verification_responses = []
        shortfall_pairs = set()
        full_rows = {}
        registrations = []
        missing_pairs = set()
        bulk_ok = servc["outcome"] == license_result["outcome"] == "ok"
        if bulk_ok or pending_targets:
            if bulk_ok:
                registrations = _verify_license_targets(session, licenses)
            verify_targets_by_pair = {
                (_text(item["bidNtceNo"]), classifier.normalize_order(_text(item["bidNtceOrd"]))): item
                for item in pending_targets + registrations
            }
            verify_pairs = set(verify_targets_by_pair)
            # 일괄 실패 시에도 영속 대기분은 재조회한다. 누락 보정·신규 등록은 모두 ok일 때만.
            missing_targets = _missing_license_targets(session, active_notices, licenses) if bulk_ok else []
            missing_pairs = {
                (_text(item["bidNtceNo"]), classifier.normalize_order(_text(item["bidNtceOrd"])))
                for item in missing_targets
            }
            # 누락 보정은 상한 없이 조회하고, 확인 전용은 대기 시작이 오래된 순이다.
            ordered_verify_pairs = sorted(verify_pairs - missing_pairs, key=lambda pair: (
                pending_since.get((pair[0], stored_order(str(pair[1]))), meta_ts),
                pair[0], stored_order(str(pair[1])),
            ))
            selected_verify_pairs = (set(ordered_verify_pairs[:_VERIFY_QUERY_LIMIT])
                                     | (verify_pairs & missing_pairs))
            fix["verify_deferred"] = len(verify_pairs - selected_verify_pairs)
            verify_targets = [verify_targets_by_pair[pair] for pair in ordered_verify_pairs[:_VERIFY_QUERY_LIMIT]]
            verify_targets.extend(verify_targets_by_pair[pair] for pair in sorted(
                verify_pairs & missing_pairs, key=lambda pair: (pair[0], stored_order(str(pair[1]))),
            ))
            targets_by_pair = {
                (_text(item["bidNtceNo"]), classifier.normalize_order(_text(item["bidNtceOrd"]))): item
                for item in missing_targets + verify_targets
            }
            targets = list(targets_by_pair.values())
            existing_license_pairs = set()
            active_license_pks = {}
            target_nos = sorted({no for no, _ in targets_by_pair})
            for start in range(0, len(target_nos), 500):
                for no, ord_, group, sequence, superseded_at in session.execute(
                        select(BidLicenseLimit.bid_ntce_no, BidLicenseLimit.bid_ntce_ord,
                               BidLicenseLimit.lmt_grp_no, BidLicenseLimit.lmt_sno,
                               BidLicenseLimit.superseded_at).where(
                            BidLicenseLimit.bid_ntce_no.in_(target_nos[start:start + 500])
                        )):
                    existing_pair = (no, classifier.normalize_order(ord_))
                    existing_license_pairs.add(existing_pair)
                    if superseded_at is None:
                        active_license_pks.setdefault(existing_pair, set()).add(
                            (no, stored_order(ord_), group, sequence))
            for pk in bulk_keys:
                bulk_pair = (pk[0], classifier.normalize_order(pk[1]))
                active_license_pks.setdefault(bulk_pair, set()).add(pk)
            if len(targets) >= 50:
                logger.warning("면허 보정·확인 대상 %d건(확인 %d건) — 이번 창 공고 %d건",
                               len(targets), len(verify_targets), len(notices))
            fix.update(outcome="ok", targets=len(targets), verify_targets=len(verify_targets),
                       total_count=0)
            for item in targets:
                no = _text(item["bidNtceNo"])
                ord_ = _text(item["bidNtceOrd"])
                pair = (no, classifier.normalize_order(ord_))
                result = _fetch_pages(LICENSE_OPERATION, {
                    "inqryDiv": "2", "bidNtceNo": no, "bidNtceOrd": _text(item["bidNtceOrd"]),
                }, snapshot)
                received_at = datetime.now()
                fix_unkeyed_items.extend(result["items"])
                fix["called"] += 1
                fix["retry_count"] += result["retry_count"]
                fix["unique_items"] += result["unique_items"]
                # 보정 단계의 전체 건수는 공고별 첫 정상 응답 건수의 합(불명이 있으면 None).
                fix["total_count"] = (fix["total_count"] + result["total_count"]
                                      if fix["total_count"] is not None
                                      and result["total_count"] is not None else None)
                rows = [row for raw in result["items"] if (row := license_values(raw)) is not None]
                if result.get("total_count_changed"):
                    fix["total_count_changed"] = True
                if result["outcome"] == "halt":
                    _warn_unkeyed_licenses("보정", fix_unkeyed_items)
                    fix["outcome"] = "failed"
                    fix["error_msg"] = result["error_msg"]
                    fix["failed"].append(no)
                    verification_responses.append((no, ord_, received_at, None, result["error_msg"]))
                    # halt 시 공고·면허 저장은 중단하되 확인 대기와 실패 시도는 보존한다.
                    with classifier.CLASSIFY_WRITE_LOCK:
                        gave_up_pairs = _expire_license_verifications(session, meta_ts, fix)
                        _apply_license_verifications(
                            session, registrations,
                            [response for response in verification_responses if not response[3]],
                            meta_ts, fix, gave_up_pairs=gave_up_pairs,
                        )
                        return halt("license_fix", result)
                validation_error = None
                if result["outcome"] == "ok":
                    valid_rows = [row for row in rows if
                                  (row["bid_ntce_no"], classifier.normalize_order(row["bid_ntce_ord"]))
                                  == pair]
                    anomaly_reasons = []
                    if len(rows) != len(result["items"]):
                        anomaly_reasons.append("전체 조회 변환 불가 행 포함")
                    if len(valid_rows) != len(rows):
                        anomaly_reasons.append("전체 조회 다른 공고·차수 행 포함")
                    # 일시적 미완료는 재조회하고, 지속적 이상은 정상 소속 행만 저장한다.
                    if result.get("total_count_changed"):
                        validation_error = "전체 조회 페이지 사이 totalCount 변화"
                    keep_pks = {_license_key(row) for row in valid_rows}
                    pk_shortfall = len(keep_pks) < result["total_count"]
                    # 한 페이지 미달은 DB 유효 행·이번 일괄 행이 모두 포함돼야 확인을 허용한다.
                    safe_shortfall = (pk_shortfall and result["pages"] == 1
                                      and not (active_license_pks.get(pair, set()) - keep_pks))
                    # 기존 유효·대체 행이 모두 없으면 해시 중복은 저장만 하고 대조는 생략한다.
                    if (validation_error is None and not anomaly_reasons and pk_shortfall
                            and not safe_shortfall
                            and (pair in existing_license_pairs or pair in verify_pairs)):
                        validation_error = (f"전체 조회 변환 후 고유 PK 미달: "
                                            f"고유 {len(keep_pks)} / 전체 {result['total_count']}")
                    if not validation_error and pair in verify_pairs:
                        if not anomaly_reasons and not keep_pks:
                            validation_error = "전체 조회 0행 응답"
                    if validation_error:
                        result = {**result, "outcome": "failed",
                                  "error_msg": f"{no}-{ord_}: {validation_error}"}
                    else:
                        if valid_rows:
                            fix["found"] += 1
                        if anomaly_reasons:
                            licenses.update({_license_key(row): row for row in valid_rows})
                            reason = "; ".join(anomaly_reasons)
                            anomalies = fix["anomalies"]
                            if len(anomalies) < 50:
                                anomalies.append({"notice": f"{no}-{stored_order(ord_)}", "reason": reason})
                            logger.warning("면허 전체 조회 이상 %s-%s: %s — 정상 소속 행만 저장, 대체·복원 생략",
                                           no, stored_order(ord_), reason)
                        elif pk_shortfall and not safe_shortfall:
                            licenses.update({_license_key(row): row for row in valid_rows})
                            skipped = fix["pk_shortfall_skipped"]
                            if len(skipped) < 50:
                                skipped.append(f"{no}-{stored_order(ord_)}")
                        elif keep_pks:
                            # 새 응답 적용 여부를 저장 잠금 안에서 판단한 뒤에만 전체 행을 저장한다.
                            full_rows[(no, stored_order(ord_))] = {_license_key(row): row for row in valid_rows}
                            if safe_shortfall:
                                shortfall_pairs.add((no, stored_order(ord_)))
                            verification_responses.append((no, ord_, received_at, keep_pks, None))
                    if anomaly_reasons and result["outcome"] == "ok":
                        verification_responses.append((no, ord_, received_at, None,
                                                       "; ".join(anomaly_reasons)))
                if result["outcome"] == "failed":
                    verification_responses.append((no, ord_, received_at, None, result["error_msg"]))
                    if (pair in verify_pairs and pair not in missing_pairs
                            or validation_error == "전체 조회 0행 응답"):
                        # 일괄 행은 정상 저장하고 실패는 대기 상태로 재시도한다(창 정상 전진).
                        if result.get("truncated"):
                            fix["truncated"] = True
                        continue
                    fix["outcome"] = "failed"
                    reason = f"검증 실패 — {validation_error}" if validation_error else result["error_msg"]
                    fix_failures.append((f"{no}-{stored_order(ord_)}", reason))
                    fix["failure_details"].append({"notice": f"{no}-{stored_order(ord_)}", "reason": reason})
                    if result.get("truncated"):
                        fix["truncated"] = True
                    fix["failed"].append(no)
                    fix_failure_codes.append(result["last_code"])

        _warn_unkeyed_licenses("보정", fix_unkeyed_items)

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
            # 기존 2인자 호출을 유지하고, 저장 잠금이 유지된 같은 트랜잭션에서 최종 차수를 읽는다.
            final_skipped = _regressed_notices(session, notices)
            regress_skipped = list({row["bid_ntce_no"]: row
                                    for row in regress_skipped + final_skipped}.values())
            regress_nos = {row["bid_ntce_no"] for row in regress_skipped}
            licenses = {pk: row for pk, row in licenses.items() if pk[0] not in regress_nos}
            registrations = [item for item in registrations if item["bidNtceNo"] not in regress_nos]
            verification_responses = [row for row in verification_responses if row[0] not in regress_nos]
            bulk_keys = {pk for pk in bulk_keys if pk[0] not in regress_nos}
            bulk_pairs = {pair for pair in bulk_pairs if pair[0] not in regress_nos}
            gave_up_pairs = _expire_license_verifications(session, meta_ts, fix)
            rejected_shortfalls = {}
            full_license_keys, stale_pairs = _apply_license_verifications(
                session, registrations, verification_responses, meta_ts, fix,
                bulk_received_at=bulk_received_at, bulk_pairs=bulk_pairs, bulk_keys=bulk_keys,
                shortfall_pairs=shortfall_pairs, rejected_shortfalls=rejected_shortfalls,
                gave_up_pairs=gave_up_pairs,
            )
            for (no, ord_), error in rejected_shortfalls.items():
                # 동시 수집 등으로 누락 보정 대상에 유효 행이 생긴 경우에도 partial을 유지한다.
                if (no, classifier.normalize_order(ord_)) in missing_pairs:
                    fix["outcome"] = "failed"
                    fix["failed"].append(no)
                    fix_failures.append((f"{no}-{stored_order(ord_)}", error))
                    fix["failure_details"].append({"notice": f"{no}-{stored_order(ord_)}", "reason": error})
            # 전체 조회를 하지 않은 공고·차수도 더 새 검증이 있으면 일괄 행을 제외한다.
            stale_keys = [pk for pk in licenses if pk[:2] in stale_pairs]
            fix["stale_bulk_skipped"] = len(bulk_keys.intersection(stale_keys))
            for pk in stale_keys:
                del licenses[pk]
            for no, ord_, _ in full_license_keys:
                licenses.update(full_rows[(no, stored_order(ord_))])
            repository.upsert_license_limits(session, list(licenses.values()), meta_ts)
            changes = repository.reconcile_license_limits(session, full_license_keys, meta_ts)
            fix.update(changes)
            classify = classifier.reclassify_notices(
                session, bid_ntce_nos=notice_nos | {row["bid_ntce_no"] for row in licenses.values()}
                | {no for no, _, _ in full_license_keys},
                commit=False,
            )
            for change in ord_changes:
                logger.info("차수변경 %s: %s -> %s", change["no"], change["old"], change["new"])
            _pending_after(session, fix)
            fix["error_msg"] = _failure_summary(fix_failures)
            if fix_failures:
                fix_code = next((code for code in fix_failure_codes
                                 if code and code not in _OK_CODES), None)
                failed.append(("license_fix", {"last_code": fix_code, "error_msg": fix["error_msg"]}))
            status = "partial" if failed else "success"
            error_code = next((result["last_code"] for _, result in failed
                               if result["last_code"] and result["last_code"] not in _OK_CODES), None)
            error_msg = "; ".join(f"{stage}: {result['error_msg']}" for stage, result in failed) or None
            total_retry = sum(detail["retry_count"] for detail in by_call.values())
            repository.finish_run(
                session, run, status=status, total_fetched=len(notices),
                total_new=new_count, total_updated=updated_count, retry_count=total_retry,
                error_code=error_code, error_msg=error_msg,
                detail_json=_build_detail_json(by_call, ord_changes, classify, regress_skipped),
            )
            if status == "success":
                repository.update_last_success_dt(session, window_end)
        logger.info("수집 종료: status=%s 공고=%d 면허보존=%d 면허저장=%d 보정=%d/%d 분류표시=%d "
                    "new=%d updated=%d retry=%d ord_changed=%d ord_regress_skipped=%d "
                    "verify_targets=%d superseded=%d restored=%d "
                    "verify_pending=%d verified=%d verify_failed=%d verify_gave_up=%d "
                    "verify_pending_after=%d verify_reregistered=%d",
                    status, len(notices), by_call["license"]["kept"], len(licenses),
                    fix["found"], fix["called"], classify["visible"],
                    new_count, updated_count, total_retry, len(ord_changes), len(regress_skipped),
                    fix["verify_targets"], fix["superseded"], fix["restored"],
                    fix["verify_pending"], fix["verified_count"],
                    fix["verify_failed_count"], fix["verify_gave_up_count"],
                    fix["verify_pending_after"], fix["verify_reregistered_count"])
        return _detach_run(session, run)
    except Exception as exc:
        repository.fail_running_run(
            session, run_id, exc,
            retry_count=sum(detail["retry_count"] for detail in by_call.values()), label="수집",
        )
        raise
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

    run = collect_in_chunks(collect_window, window_bgn, window_end, trigger="backfill")
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
