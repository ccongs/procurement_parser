"""목록 표시 분류 — 순수 판별·규칙 조회·명시적 재분류(Phase 13.2)."""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BidLicenseLimit, BidNotice, ListRule
from app.repository import get_config


# 설정·규칙 변경과 수집 저장을 재분류 커밋까지 직렬화한다(프로세스 내 공용).
CLASSIFY_WRITE_LOCK = threading.RLock()


DEFAULT_LIST_RULES: tuple[tuple[str, str], ...] = (
    ("clsfc", "정보시스템개발서비스"),
    ("clsfc", "패키지소프트웨어개발및도입서비스"),
    ("clsfc", "정보시스템유지관리서비스"),
    ("clsfc", "소프트웨어유지및지원서비스"),
    ("clsfc", "정보인프라구축서비스"),
    ("clsfc", "정보시스템감리서비스"),
    ("clsfc", "정보화전략계획서비스"),
    ("clsfc", "정보통신연구조사서비스"),
    ("clsfc", "정보통신감리용역"),
    ("clsfc", "정보통신설계용역"),
    ("clsfc", "데이터서비스"),
    ("clsfc", "공간정보DB구축서비스"),
    ("clsfc", "인터넷지원개발서비스"),
    ("clsfc", "클라우드서비스"),
    ("clsfc", "클라우드융합서비스"),
    ("clsfc", "클라우드지원서비스"),
    ("keyword", "홈페이지"),
    ("keyword", "누리집"),
    ("keyword", "웹사이트"),
    ("keyword", "소프트웨어"),
    ("keyword", "SW"),
    ("keyword", "정보화"),
    ("keyword", "정보시스템"),
    ("keyword", "플랫폼"),
    ("keyword", "앱"),
    ("keyword", "모바일"),
    ("keyword", "DB"),
    ("keyword", "데이터베이스"),
    ("keyword", "클라우드"),
    ("keyword", "전산"),
    ("keyword", "솔루션"),
    ("keyword", "포털"),
    ("keyword", "프로그램 개발"),
    ("keyword", "시스템 구축"),
    ("keyword", "시스템 고도화"),
    ("keyword", "시스템 개발"),
    ("keyword", "LMS"),
    ("keyword", "ERP"),
    ("keyword", "그룹웨어"),
    ("negative", "전산화단층"),
)


@dataclass(frozen=True)
class RuleSet:
    clsfc: tuple[str, ...]
    keywords: tuple[str, ...]
    negatives: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    list_visible: bool
    match_basis: str | None
    match_detail: str | None
    matched_indstryty_cds: str | None


@lru_cache(maxsize=128)
def _compile_terms(terms: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """규칙 값 튜플별 캐시로 전 공고 순회 중 반복 컴파일을 피한다."""
    patterns = []
    normalized = dict.fromkeys(" ".join(term.split()) for term in terms)
    for term in normalized:
        parts = term.split()
        if not parts:
            continue
        pattern = r"\s*".join(re.escape(part) for part in parts)
        if re.fullmatch(r"[A-Za-z0-9]", parts[0][0]):
            pattern = r"(?<![A-Za-z0-9])" + pattern
        if re.fullmatch(r"[A-Za-z0-9]", parts[-1][-1]):
            pattern += r"(?![A-Za-z0-9])"
        patterns.append((term, re.compile(pattern, re.IGNORECASE)))
    return tuple(patterns)


def match_terms(title: str | None, terms: Sequence[str]) -> list[str]:
    """공고명에 일치하는 값을 규칙 순서대로 중복 없이 반환한다."""
    if not title:
        return []
    return [term for term, pattern in _compile_terms(tuple(terms)) if pattern.search(title)]


def classify_notice(
    *,
    indstryty_lmt_yn: str | None,
    pub_prcrmnt_clsfc_nm: str | None,
    bid_ntce_nm: str | None,
    license_codes: Collection[str],
    match_codes: Collection[str],
    rules: RuleSet,
) -> Classification:
    """업종 → 분류 → 키워드 우선순위로 첫 표시 근거만 기록한다."""
    matched = set(license_codes).intersection(match_codes)
    if matched:
        return Classification(True, "업종", None, ",".join(sorted(matched)))
    if indstryty_lmt_yn != "Y":
        clsfc = (pub_prcrmnt_clsfc_nm or "").strip()
        if clsfc and clsfc in rules.clsfc:
            return Classification(True, "분류", clsfc, None)
        if not match_terms(bid_ntce_nm, rules.negatives):
            keywords = match_terms(bid_ntce_nm, rules.keywords)
            if keywords:
                return Classification(True, "키워드", ", ".join(keywords)[:200], None)
    return Classification(False, None, None, None)


def _split_codes(value: str | None) -> set[str]:
    return {code.strip() for code in (value or "").split(",") if code.strip()}


def normalize_order(value: str) -> int | str:
    """차수 표기 차이를 흡수한다(정수 우선, 아니면 3자리 0채움)."""
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value.zfill(3)


def rule_set_from_rows(rows: Iterable[Mapping[str, Any] | ListRule]) -> RuleSet:
    """켜진 규칙을 id 순·종류별로 묶고 id 없는 새 규칙은 뒤에 붙인다."""
    def field(row, name):
        return row.get(name) if isinstance(row, Mapping) else getattr(row, name)

    values: dict[str, list[str]] = {"clsfc": [], "keyword": [], "negative": []}
    for row in sorted(rows, key=lambda row: (field(row, "id") is None, field(row, "id") or 0)):
        kind = field(row, "rule_type")
        if field(row, "enabled") and kind in values:
            values[kind].append(field(row, "value"))
    return RuleSet(tuple(values["clsfc"]), tuple(values["keyword"]), tuple(values["negative"]))


def load_rule_set(session: Session) -> RuleSet:
    """DB 규칙을 공용 구성 함수로 읽는다."""
    return rule_set_from_rows(session.scalars(select(ListRule)))


def load_match_codes(session: Session) -> frozenset[str]:
    """설정의 매칭 업종코드를 공백·빈값·중복 없이 읽는다."""
    return frozenset(_split_codes(get_config(session).indstryty_cds))


def iter_classifications(
    session: Session,
    *,
    rules: RuleSet,
    match_codes: Collection[str],
    bid_ntce_nos: Iterable[str] | None = None,
    batch_size: int = 500,
) -> Iterator[tuple[BidNotice, Classification]]:
    """공고·면허를 배치 조회해 판정만 내준다. 미리보기용으로 쓰기·커밋은 없다.

    flush 된 행만 대상이며, 호출자가 필요 시 flush 한다.
    """
    if batch_size <= 0:
        raise ValueError("batch_size 는 양수여야 합니다.")
    # 호출자의 autoflush 설정과 무관하게 미리보기 중 DB 쓰기를 막는다.
    with session.no_autoflush:
        targets = (
            tuple(sorted({no.strip() for no in bid_ntce_nos if no.strip()}))
            if bid_ntce_nos is not None else None
        )
        if targets == ():
            return
        codes = set(match_codes)
        last_no = None
        target_offset = 0
        while True:
            query = select(BidNotice).order_by(BidNotice.bid_ntce_no)
            if targets is not None:
                batch = targets[target_offset:target_offset + batch_size]
                if not batch:
                    break
                target_offset += batch_size
                query = query.where(BidNotice.bid_ntce_no.in_(batch))
            else:
                query = query.limit(batch_size)
                if last_no is not None:
                    query = query.where(BidNotice.bid_ntce_no > last_no)
            notices = session.scalars(query).all()
            if not notices:
                if targets is None:
                    break
                continue
            licenses: dict[str, list[BidLicenseLimit]] = defaultdict(list)
            for row in session.scalars(select(BidLicenseLimit).where(
                BidLicenseLimit.bid_ntce_no.in_([notice.bid_ntce_no for notice in notices])
            )):
                licenses[row.bid_ntce_no].append(row)
            for notice in notices:
                rows = licenses[notice.bid_ntce_no]
                legacy = not rows and bool(notice.matched_indstryty_cds)
                license_codes = set()
                if rows:
                    current_ord = (notice.bid_ntce_ord or "").strip()
                    if current_ord:
                        # 현재 차수가 없으면 이전 차수로 참가 가능 여부를 추정하지 않는다.
                        selected_ord = normalize_order(current_ord)
                    else:
                        try:
                            selected_ord = max(int(row.bid_ntce_ord) for row in rows)
                        except ValueError:
                            selected_ord = normalize_order(max(
                                row.bid_ntce_ord.strip().zfill(3) for row in rows
                            ))
                    for row in rows:
                        if normalize_order(row.bid_ntce_ord) == selected_ord:
                            license_codes.update(_split_codes(row.lcns_cd))
                            license_codes.update(_split_codes(row.permsn_cds))
                elif legacy:
                    license_codes = _split_codes(notice.matched_indstryty_cds)
                result = classify_notice(
                    indstryty_lmt_yn=notice.indstryty_lmt_yn,
                    pub_prcrmnt_clsfc_nm=notice.pub_prcrmnt_clsfc_nm,
                    bid_ntce_nm=notice.bid_ntce_nm,
                    license_codes=license_codes,
                    match_codes=codes,
                    rules=rules,
                )
                if legacy:
                    # 면허가 없는 기존 공고의 유일한 업종 근거는 숨겨져도 보존한다.
                    result = replace(result, matched_indstryty_cds=notice.matched_indstryty_cds)
                yield notice, result
            if targets is None:
                last_no = notices[-1].bid_ntce_no


def reclassify_notices(
    session: Session, *, bid_ntce_nos: Iterable[str] | None = None,
    rules: RuleSet | None = None, match_codes: Collection[str] | None = None,
    commit: bool = True,
) -> dict:
    """초안이 있으면 쓰기를 커밋까지 미루고, 없으면 flush 후 저장된 기준으로 판정한다."""
    if rules is None and match_codes is None:
        session.flush()
    with session.no_autoflush:
        if rules is None:
            rules = load_rule_set(session)
        if match_codes is None:
            match_codes = load_match_codes(session)
    stats = {"total": 0, "changed": 0, "visible": 0, "hidden": 0,
             "by_basis": {"업종": 0, "분류": 0, "키워드": 0}}
    columns = ("list_visible", "match_basis", "match_detail", "matched_indstryty_cds")
    for notice, result in iter_classifications(
        session, rules=rules, match_codes=match_codes, bid_ntce_nos=bid_ntce_nos,
    ):
        stats["total"] += 1
        if result.list_visible:
            stats["visible"] += 1
            stats["by_basis"][result.match_basis] += 1
        else:
            stats["hidden"] += 1
        if any(getattr(notice, column) != getattr(result, column) for column in columns):
            for column in columns:
                setattr(notice, column, getattr(result, column))
            stats["changed"] += 1
    if commit:
        session.commit()
    return stats
