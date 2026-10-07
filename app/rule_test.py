"""규칙 테스트 초안 검증·반영 계획·무쓰기 미리보기(Phase 13.4b)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any

from app import classifier, repository


MAX_RULES_PER_KIND = 300
MAX_DRAFT_CHARS = 20_000
STALE_MESSAGE = "다른 곳에서 규칙(또는 매칭 업종코드)이 바뀌어 현재 규칙을 다시 불러왔습니다. 초안을 다시 입력한 뒤 미리보기하세요."
FORM_MESSAGE = "요청 형식이 올바르지 않아 현재 규칙을 다시 불러왔습니다."


class FormStructureError(ValueError):
    """입력칸 누락·중복 또는 비문자열 폼 값."""


class DraftValidationError(ValueError):
    """사용자가 수정할 수 있는 초안·기간 입력 오류."""


class StaleRulesError(Exception):
    """렌더 시점 규칙·매칭 업종코드와 현재 상태의 불일치."""


def rules_fingerprint(rows: list[dict], match_codes: frozenset[str]) -> str:
    """꺼진 규칙·추가·삭제와 매칭 업종코드를 포함한 전체 집합 지문."""
    references = [(row["id"], repository.list_rule_fingerprint(row))
                  for row in sorted(rows, key=lambda row: row["id"])]
    return hashlib.sha256(json.dumps([references, sorted(match_codes)]).encode("utf-8")).hexdigest()


def validate_form_structure(data: Mapping[str, Any]) -> dict[str, str]:
    """다중값을 보존한 상태에서 구조를 확인하고 초안 문자열을 반환한다."""
    getlist = getattr(data, "getlist", None)
    inputs = {}
    for kind, title, _ in repository.RULE_FIELDS:
        items = getlist(kind) if callable(getlist) else ([data[kind]] if kind in data else [])
        if len(items) != 1 or not isinstance(items[0], str):
            raise FormStructureError(f"{title}: 초안 입력칸을 각각 정확히 한 번 제출해야 합니다.")
        inputs[kind] = items[0]
    for field in ("dt_from", "dt_to", "search"):
        items = getlist(field) if callable(getlist) else ([data[field]] if field in data else [])
        if any(not isinstance(item, str) for item in items):
            raise FormStructureError(FORM_MESSAGE)
    return inputs


def draft_values(data: Mapping[str, Any]) -> dict[str, list[str]]:
    """필드 제출 횟수·총 크기를 먼저 검사하고 빈 줄을 뺀 규칙을 검증한다."""
    inputs = validate_form_structure(data)
    if sum(len(value) for value in inputs.values()) > MAX_DRAFT_CHARS:
        raise DraftValidationError(f"세 초안 입력 합계는 {MAX_DRAFT_CHARS:,}자 이하여야 합니다.")
    result = {}
    for kind, title, _ in repository.RULE_FIELDS:
        values, seen = [], set()
        for raw in inputs[kind].splitlines():
            if not repository.normalize_rule_value(raw):
                continue
            if len(values) >= MAX_RULES_PER_KIND:
                raise DraftValidationError(f"{title}: 규칙은 종류별 {MAX_RULES_PER_KIND}개 이하여야 합니다.")
            try:
                value = repository.validate_list_rule_value(kind, raw)
            except ValueError as exc:
                raise DraftValidationError(str(exc)) from exc
            key = repository.list_rule_value_key(kind, value)
            if key in seen:
                raise DraftValidationError(f"{title}: 중복된 규칙입니다('{value}').")
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
                raise StaleRulesError


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
        raise DraftValidationError("공고일 시작·끝 날짜를 확인하세요.") from None


def preview(session, plan: list[dict], match_codes, data: dict[str, str]) -> dict:
    """기간 내 전체를 집계하되 화면용 행은 상한까지만 보관한다."""
    start, end = date_bounds(data)
    stats = {"total": 0, "current": 0, "draft": 0, "added": 0, "removed": 0,
             "current_basis": dict.fromkeys(("업종", "분류", "키워드"), 0),
             "draft_basis": dict.fromkeys(("업종", "분류", "키워드"), 0),
             "added_rows": [], "removed_rows": [], "search_rows": [], "search_total": 0}
    search = data.get("search", "").strip().casefold()
    with session.no_autoflush:
        for notice, result in classifier.iter_classifications(
            session, rules=classifier.rule_set_from_rows(plan), match_codes=match_codes,
            bid_ntce_dt_from=start, bid_ntce_dt_to=end,
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
