"""규칙 테스트 초안 검증·반영 계획·무쓰기 미리보기(Phase 13.4b)."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from app import classifier, repository
from app.models import BidNotice


RULE_FIELDS = (
    ("clsfc", "IT 공공조달분류", "정식 분류명과 정확히 같을 때"),
    ("keyword", "SW 키워드", "공고명 부분 일치, 영문·숫자 약어는 앞뒤가 영문·숫자가 아닐 때, 공백은 있어도 없어도 일치"),
    ("negative", "판별 제외어", "공고명에 일치하면 키워드에 의한 표시에서 제외합니다."),
)
STALE_MESSAGE = "규칙 목록이 바뀌었습니다. 새로고침 후 다시 시도하세요."


def rules_fingerprint(rows: list[dict]) -> str:
    """꺼진 규칙·추가·삭제까지 포함한 전체 집합 지문."""
    references = [(row["id"], repository.list_rule_fingerprint(row))
                  for row in sorted(rows, key=lambda row: row["id"])]
    return hashlib.sha256(json.dumps(references).encode("utf-8")).hexdigest()


def draft_values(data: dict[str, str]) -> dict[str, list[str]]:
    """빈 줄을 무시하고 종류별 길이·중복을 검증한다."""
    result = {}
    for kind, title, _ in RULE_FIELDS:
        values, seen = [], set()
        for raw in data.get(kind, "").splitlines():
            if not repository.normalize_rule_value(raw):
                continue
            value = repository.validate_list_rule_value(kind, raw)
            key = repository.list_rule_value_key(kind, value)
            if key in seen:
                raise ValueError(f"{title}: 중복된 규칙입니다('{value}').")
            seen.add(key)
            values.append(value)
        result[kind] = values
    return result


def rule_plan(rows: list[dict], values: dict[str, list[str]]) -> list[dict]:
    """기존 값·id 순서를 보존해 미리보기와 적용의 판정 상세도 일치시킨다."""
    wanted = {kind: {repository.list_rule_value_key(kind, value) for value in items}
              for kind, items in values.items()}
    plan = [dict(row, enabled=repository.list_rule_value_key(row["rule_type"], row["value"])
                 in wanted[row["rule_type"]]) for row in rows]
    existing = {(row["rule_type"], repository.list_rule_value_key(row["rule_type"], row["value"]))
                for row in rows}
    for kind, items in values.items():
        for value in items:
            if (kind, repository.list_rule_value_key(kind, value)) not in existing:
                plan.append({"rule_type": kind, "value": value, "enabled": True})
    return plan


def apply_plan(session, plan: list[dict]) -> None:
    """삭제 없이 기존 행의 사용 여부와 새 규칙만 미커밋으로 반영한다."""
    repository.set_list_rules_enabled(
        session, [row["id"] for row in plan if "id" in row],
        [row["id"] for row in plan if "id" in row and row["enabled"]], commit=False,
    )
    for row in plan:
        if "id" not in row:
            if not repository.add_list_rule(session, row["rule_type"], row["value"], commit=False):
                raise ValueError(STALE_MESSAGE)


def date_bounds(data: dict[str, str]) -> tuple[datetime | None, datetime | None]:
    """공고일 시작·끝을 포함한다. 빈 경계는 제한하지 않는다."""
    try:
        start = date.fromisoformat(data["dt_from"]) if data.get("dt_from") else None
        end = date.fromisoformat(data["dt_to"]) if data.get("dt_to") else None
        if start and end and start > end:
            raise ValueError
        return (datetime.combine(start, time.min) if start else None,
                datetime.combine(end + timedelta(days=1), time.min) if end else None)
    except (ValueError, OverflowError):
        raise ValueError("공고일 시작·끝 날짜를 확인하세요.") from None


def preview(session, plan: list[dict], match_codes, data: dict[str, str]) -> dict:
    """기간 내 전체를 집계하되 화면용 행은 상한까지만 보관한다."""
    start, end = date_bounds(data)
    query = select(BidNotice.bid_ntce_no)
    if start:
        query = query.where(BidNotice.bid_ntce_dt >= start)
    if end:
        query = query.where(BidNotice.bid_ntce_dt < end)
    stats = {"total": 0, "current": 0, "draft": 0, "added": 0, "removed": 0,
             "current_basis": dict.fromkeys(("업종", "분류", "키워드"), 0),
             "draft_basis": dict.fromkeys(("업종", "분류", "키워드"), 0),
             "added_rows": [], "removed_rows": [], "search_rows": [], "search_total": 0}
    search = data.get("search", "").strip().casefold()
    with session.no_autoflush:
        for notice, result in classifier.iter_classifications(
            session, rules=classifier.rule_set_from_rows(plan), match_codes=match_codes,
            bid_ntce_nos=session.scalars(query),
        ):
            stats["total"] += 1
            for label, visible, basis in (("current", notice.list_visible, notice.match_basis),
                                          ("draft", result.list_visible, result.match_basis)):
                if visible:
                    stats[label] += 1
                    if basis in stats[label + "_basis"]:
                        stats[label + "_basis"][basis] += 1
            row = {"no": notice.bid_ntce_no, "title": notice.bid_ntce_nm,
                   "date": notice.bid_ntce_dt,
                   "current": (notice.list_visible, notice.match_basis, notice.match_detail,
                               notice.matched_indstryty_cds),
                   "draft": (result.list_visible, result.match_basis, result.match_detail,
                             result.matched_indstryty_cds)}
            change = ("added" if result.list_visible else "removed")
            if bool(notice.list_visible) != result.list_visible:
                stats[change] += 1
                if len(stats[change + "_rows"]) < 200:
                    stats[change + "_rows"].append(row)
            if search and search in (notice.bid_ntce_nm or "").casefold():
                stats["search_total"] += 1
                if len(stats["search_rows"]) < 100:
                    stats["search_rows"].append(row)
    return stats
